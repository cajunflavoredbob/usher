"""The Webhook tab URL must not keep a trailing /admin from the configured
Usher Admin UI URL (the auth middleware answers /admin/webhook/seerr with a
302), and a bad URL error names the field by its label."""
from __future__ import annotations

import re

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import webui

SECRET = b"0123456789abcdef0123456789abcdef"
_SESSION = webui._make_session_cookie(SECRET, "admin")
CSRF = webui.generate_csrf_token(SECRET, _SESSION.rsplit(".", 1)[-1])
COOKIES = {webui.SESSION_COOKIE: _SESSION, "usher_csrf": CSRF}


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.delenv("USHER_WEBHOOK_SECRET", raising=False)
    path = tmp_path / "settings.json"
    store = webui.SettingsStore(path)
    store.settings.admin.username = "admin"
    store.settings.admin.password_hash = "x"
    store.settings.usher_public_url = "https://usher.example.com/admin"
    store.save()
    app = web.Application()
    webui.attach_webui(
        app, settings_store=store, session_secret=SECRET, data_dir=tmp_path,
        settings_path=path, db_path=tmp_path / "m.sqlite")
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_webhook_tab_url_drops_trailing_admin(client):
    r = await client.get("/admin", cookies=COOKIES)
    html = await r.text()
    assert "https://usher.example.com/webhook/seerr" in html
    assert "/admin/webhook/seerr" not in html


async def test_bad_public_url_error_uses_field_label(client):
    r = await client.post(
        "/admin/telegram", cookies=COOKIES,
        data={"csrf_token": CSRF, "telegram_bot_token": "123:abc",
              "admin_telegram_id": "1", "usher_public_url": "ftp://x"})
    html = await r.text()
    assert r.status == 400
    assert "Usher Admin UI URL:" in html
    assert "Usher Public URL" not in html
