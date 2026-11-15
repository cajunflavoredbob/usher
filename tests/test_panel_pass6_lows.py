"""Pass 6 Lows in the panel: a save skipped by the restore freeze is said and
not hot-reloaded, the PBKDF2 auto-upgrade hashes off the event loop, and the
change-password form throttles wrong current passwords per session."""
from __future__ import annotations

import logging
import threading

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import webui
from settings import hash_password

SECRET = b"0123456789abcdef0123456789abcdef"
_SESSION = webui._make_session_cookie(SECRET, "admin")
CSRF = webui.generate_csrf_token(SECRET, _SESSION.rsplit(".", 1)[-1])
REAL_PW = "right-password-1"


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.delenv("USHER_WEBHOOK_SECRET", raising=False)
    reloads = []

    async def reload_cb():
        reloads.append(1)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    settings_path = data_dir / "settings.json"
    store = webui.SettingsStore(settings_path)
    store.settings.admin.username = "admin"
    store.settings.admin.password_hash = hash_password(REAL_PW)
    store.save()
    app = web.Application()
    webui.attach_webui(
        app, settings_store=store, session_secret=SECRET,
        data_dir=data_dir, settings_path=settings_path,
        db_path=data_dir / "mappings.sqlite",
        enc_key_path=data_dir / "encryption.key",
        on_settings_changed=reload_cb,
    )
    async with TestClient(TestServer(app)) as c:
        yield c, store, reloads


def _cookies():
    return {webui.SESSION_COOKIE: _SESSION, "usher_csrf": CSRF}


async def test_a_tab_save_skipped_by_the_restore_freeze_is_not_called_saved(env):
    client, store, reloads = env
    # A restore froze saves (write_restored) but the restart is not yet marked.
    store.write_restored(store.path.read_bytes())
    resp = await client.post(
        "/admin/webhook",
        data={"csrf_token": CSRF, "webhook_secret": "changed-secret"},
        cookies=_cookies())
    text = await resp.text()
    assert resp.status == 503
    assert "not saved" in text and "Saved." not in text
    assert reloads == []  # unsaved values are not hot-reloaded


async def test_a_normal_tab_save_still_saves_and_reloads(env):
    client, store, reloads = env
    resp = await client.post(
        "/admin/webhook",
        data={"csrf_token": CSRF, "webhook_secret": "changed-secret"},
        cookies=_cookies())
    assert resp.status == 200
    assert "Saved." in await resp.text()
    assert reloads == [1]
    assert '"changed-secret"' in store.path.read_text()


async def test_a_password_change_skipped_by_the_freeze_says_so(env):
    client, store, _ = env
    old_hash = store.settings.admin.password_hash
    store.write_restored(store.path.read_bytes())
    resp = await client.post(
        "/admin/password",
        data={"csrf_token": CSRF, "current": REAL_PW,
              "new": "brand-new-password-1", "confirm": "brand-new-password-1"},
        cookies=_cookies(), allow_redirects=False)
    assert resp.status == 503
    assert "not saved" in await resp.text()
    assert "Set-Cookie" not in resp.headers  # no re-issued session
    assert '"password_version": 1' not in store.path.read_text()
    assert old_hash in store.path.read_text()


async def test_the_pbkdf2_rehash_runs_off_the_event_loop(env, monkeypatch):
    client, store, _ = env
    # A stored hash with a stale iteration count.
    import secrets
    from hashlib import pbkdf2_hmac
    salt = secrets.token_bytes(16)
    iters = 1000
    h = pbkdf2_hmac("sha256", b"hunter2-test", salt, iters)
    store.settings.admin.password_hash = f"pbkdf2_sha256${iters}${salt.hex()}${h.hex()}"
    store.save()
    main = threading.get_ident()
    seen = []
    real = webui.hash_password

    def spy(pw):
        seen.append(threading.get_ident())
        return real(pw)

    monkeypatch.setattr(webui, "hash_password", spy)
    get = await client.get("/admin/login")
    from http.cookies import SimpleCookie
    sc = SimpleCookie()
    for c in get.headers.getall("Set-Cookie", []):
        sc.load(c)
    csrf = sc["usher_csrf"].value
    resp = await client.post(
        "/admin/login",
        data={"username": "admin", "password": "hunter2-test", "csrf_token": csrf},
        cookies={"usher_csrf": csrf}, allow_redirects=False)
    assert resp.status == 302
    assert seen and all(t != main for t in seen)


async def test_wrong_current_passwords_are_throttled_per_session_and_audited(
        env, caplog):
    client, store, _ = env
    caplog.set_level(logging.WARNING, logger="usher.audit")
    form = {"csrf_token": CSRF, "current": "wrong",
            "new": "brand-new-password-1", "confirm": "brand-new-password-1"}
    for _ in range(5):
        resp = await client.post("/admin/password", data=form,
                                 cookies=_cookies())
        assert resp.status == 400
    resp = await client.post("/admin/password", data=form, cookies=_cookies())
    assert resp.status == 429
    assert "Retry-After" in resp.headers
    # Even the right password is refused while the session is locked.
    ok = dict(form, current=REAL_PW)
    resp = await client.post("/admin/password", data=ok, cookies=_cookies())
    assert resp.status == 429
    msgs = [r.getMessage() for r in caplog.records if r.name == "usher.audit"]
    assert sum("password_change_fail" in m for m in msgs) == 5


async def test_a_right_current_password_clears_the_count(env):
    client, store, _ = env
    bad = {"csrf_token": CSRF, "current": "wrong",
           "new": "brand-new-password-1", "confirm": "brand-new-password-1"}
    for _ in range(4):
        assert (await client.post("/admin/password", data=bad,
                                  cookies=_cookies())).status == 400
    good = dict(bad, current=REAL_PW)
    assert (await client.post("/admin/password", data=good,
                              cookies=_cookies())).status == 200
    # The four wrong answers no longer count: not even a bucket is left.
    assert _SESSION.rsplit(".", 1)[-1] not in webui._password_change_throttle._failures
