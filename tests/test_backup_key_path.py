"""Backup and restore use the encryption key path the bot actually runs with
(ENCRYPTION_KEY_PATH), not a hard-coded data_dir/encryption.key."""
from __future__ import annotations

import io
import zipfile

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet

import webui

SECRET = b"0123456789abcdef0123456789abcdef"
_SESSION = webui._make_session_cookie(SECRET, "admin")
CSRF = webui.generate_csrf_token(SECRET, _SESSION.rsplit(".", 1)[-1])


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.delenv("USHER_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(webui, "schedule_clean_exit", lambda *a, **k: None)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    key_path = key_dir / "custom.key"
    key_path.write_bytes(Fernet.generate_key())
    settings_path = data_dir / "settings.json"
    store = webui.SettingsStore(settings_path)
    store.settings.admin.username = "admin"
    store.settings.admin.password_hash = "x"
    store.save()
    app = web.Application()
    webui.attach_webui(
        app, settings_store=store, session_secret=SECRET,
        data_dir=data_dir, settings_path=settings_path,
        db_path=data_dir / "mappings.sqlite", enc_key_path=key_path,
    )
    async with TestClient(TestServer(app)) as c:
        yield c, data_dir, key_path


def _cookies():
    return {webui.SESSION_COOKIE: _SESSION, "usher_csrf": CSRF}


async def test_backup_reads_key_from_custom_path(env):
    client, data_dir, key_path = env
    resp = await client.post("/admin/backup",
                             data={"csrf_token": CSRF, "passphrase": "",
                                   "unencrypted_ok": "1"},
                             cookies=_cookies())
    assert resp.status == 200
    with zipfile.ZipFile(io.BytesIO(await resp.read())) as zf:
        assert zf.read("encryption.key") == key_path.read_bytes()


async def test_restore_writes_key_to_custom_path_and_snapshots_it(env):
    client, data_dir, key_path = env
    old_key = key_path.read_bytes()
    new_key = Fernet.generate_key()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("settings.json", (data_dir / "settings.json").read_bytes())
        zf.writestr("encryption.key", new_key)
    form = {"csrf_token": CSRF, "passphrase": "", "backup": buf.getvalue()}
    resp = await client.post("/admin/restore", data=form, cookies=_cookies())
    assert resp.status == 200
    assert key_path.read_bytes() == new_key
    assert not (data_dir / "encryption.key").exists()
    snaps = list(data_dir.glob("pre-restore-*/custom.key"))
    assert len(snaps) == 1 and snaps[0].read_bytes() == old_key


async def test_a_save_after_a_restore_does_not_write_over_it(env):
    client, data_dir, key_path = env
    store = client.server.app["settings_store"]
    restored = (data_dir / "settings.json").read_text().replace(
        '"admin"', '"restored-admin"', 1)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("settings.json", restored)
    form = {"csrf_token": CSRF, "passphrase": "", "backup": buf.getvalue()}
    resp = await client.post("/admin/restore", data=form, cookies=_cookies())
    assert resp.status == 200
    # A save thread that was queued before the restore runs now, holding the
    # old in-memory settings: it must not rename over the restored file.
    await store.save_async()
    assert (data_dir / "settings.json").read_text() == restored


async def test_a_restore_failing_after_settings_was_written_restarts(env, monkeypatch):
    client, data_dir, key_path = env
    exits = []
    monkeypatch.setattr(webui, "schedule_clean_exit",
                        lambda *a, **k: exits.append(a))
    real = webui.atomic_write_bytes

    def fail_on_key(path, data, *a, **k):
        if path == key_path:
            raise OSError("disk full")
        return real(path, data, *a, **k)

    monkeypatch.setattr(webui, "atomic_write_bytes", fail_on_key)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("settings.json", (data_dir / "settings.json").read_bytes())
        zf.writestr("encryption.key", Fernet.generate_key())
    form = {"csrf_token": CSRF, "passphrase": "", "backup": buf.getvalue()}
    resp = await client.post("/admin/restore", data=form, cookies=_cookies())
    assert resp.status == 500
    text = await resp.text()
    assert "partway" in text and "restarting" in text
    assert exits == [(2.0,)]


async def test_a_restore_failing_before_any_write_does_not_restart_or_freeze(env, monkeypatch):
    client, data_dir, key_path = env
    store = client.server.app["settings_store"]
    exits = []
    monkeypatch.setattr(webui, "schedule_clean_exit",
                        lambda *a, **k: exits.append(a))

    def boom(data):
        raise OSError("read-only volume")

    monkeypatch.setattr(store, "write_restored", boom)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("settings.json", (data_dir / "settings.json").read_bytes())
    form = {"csrf_token": CSRF, "passphrase": "", "backup": buf.getvalue()}
    resp = await client.post("/admin/restore", data=form, cookies=_cookies())
    assert resp.status == 500
    assert "partway" not in await resp.text()
    assert exits == []
    store.settings.admin.username = "after"
    await store.save_async()
    assert '"after"' in (data_dir / "settings.json").read_text()
