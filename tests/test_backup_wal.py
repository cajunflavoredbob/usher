"""Backup must capture uncheckpointed WAL data: the store runs
in WAL mode, so recent commits live in mappings.sqlite-wal until a checkpoint.
Zipping the bare .sqlite file silently dropped them; VACUUM INTO snapshots
the complete database."""
from __future__ import annotations

import io
import sqlite3
import zipfile

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import webui

SECRET = b"0123456789abcdef0123456789abcdef"
CSRF = "csrf-token-value"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_WEBHOOK_SECRET", raising=False)
    settings_path = tmp_path / "settings.json"
    store = webui.SettingsStore(settings_path)
    store.settings.admin.username = "admin"
    store.settings.admin.password_hash = "x"
    store.save()

    app = web.Application()
    webui.attach_webui(
        app,
        settings_store=store,
        session_secret=SECRET,
        data_dir=tmp_path,
        settings_path=settings_path,
        db_path=tmp_path / "mappings.sqlite",
    )
    async with TestClient(TestServer(app)) as c:
        yield c


def _cookies():
    return {
        webui.SESSION_COOKIE: webui._make_session_cookie(SECRET, "admin"),
        "hermes_csrf": CSRF,
    }


async def test_backup_includes_uncheckpointed_wal_data(client, tmp_path):
    db = tmp_path / "mappings.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('recent-commit')")
    conn.commit()
    # Keep the connection open: closing the last connection checkpoints the
    # WAL, which would hide exactly the condition being tested.
    try:
        assert (tmp_path / "mappings.sqlite-wal").exists()
        resp = await client.post("/admin/backup",
                                 data={"csrf_token": CSRF, "passphrase": "",
                                       "unencrypted_ok": "1"},
                                 cookies=_cookies())
        assert resp.status == 200
        blob = await resp.read()
    finally:
        conn.close()

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        snapshot_bytes = zf.read("mappings.sqlite")
    snap_path = tmp_path / "snapshot.sqlite"
    snap_path.write_bytes(snapshot_bytes)
    with sqlite3.connect(snap_path) as check:
        rows = check.execute("SELECT v FROM t").fetchall()
    assert rows == [("recent-commit",)]
