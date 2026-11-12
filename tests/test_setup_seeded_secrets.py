"""The first-run setup page is public until an Admin account exists, so it
must never echo seeded secrets or the Admin's Telegram id."""
from __future__ import annotations

import re

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import auth_util
import webui

SECRET = b"0123456789abcdef0123456789abcdef"
BOT_TOKEN = "123456:SEEDED-bot-token-value"
SEERR_KEY = "seeded-seerr-key-value"
ADMIN_ID = 424242


async def _make_client(tmp_path, monkeypatch, *, seeded: bool):
    monkeypatch.delenv("USHER_WEBHOOK_SECRET", raising=False)
    path = tmp_path / "settings.json"
    store = webui.SettingsStore(path)
    if seeded:
        store.settings.telegram_bot_token = BOT_TOKEN
        store.settings.seerr_api_key = SEERR_KEY
        store.settings.admin_telegram_id = ADMIN_ID
        store.settings.seerr_url = "http://192.0.2.10:5056"
    app = web.Application()
    webui.attach_webui(
        app, settings_store=store, session_secret=SECRET, data_dir=tmp_path,
        settings_path=path, db_path=tmp_path / "m.sqlite", http_port=8765)
    c = TestClient(TestServer(app))
    await c.start_server()
    c.settings_store = store
    return c


async def _form(c, **over):
    r = await c.get("/admin/setup")
    html = await r.text()
    csrf = re.search(r'name="%s" value="([^"]+)"' % webui.CSRF_FORM_FIELD,
                     html).group(1)
    token = (c.server.app["data_dir"] / auth_util.SETUP_TOKEN_FILE).read_text().strip()
    data = {webui.CSRF_FORM_FIELD: csrf, "setup_token": token,
            "username": "admin", "password": "longenoughpw",
            "confirm": "longenoughpw", "telegram_bot_token": "",
            "admin_telegram_id": "", "seerr_url": "http://192.0.2.10:5056",
            "seerr_api_key": ""}
    data.update(over)
    return html, data


@pytest.fixture
async def seeded(tmp_path, monkeypatch):
    c = await _make_client(tmp_path, monkeypatch, seeded=True)
    yield c
    await c.close()


@pytest.fixture
async def empty(tmp_path, monkeypatch):
    c = await _make_client(tmp_path, monkeypatch, seeded=False)
    yield c
    await c.close()


async def test_get_hides_seeded_secrets_and_admin_id(seeded):
    html, _ = await _form(seeded)
    assert BOT_TOKEN not in html
    assert SEERR_KEY not in html
    assert str(ADMIN_ID) not in html
    assert "Already set. Leave blank to keep it." in html


async def test_post_blank_fields_keeps_seeded_values(seeded):
    _, data = await _form(seeded)
    r = await seeded.post("/admin/setup", data=data)
    assert r.status == 200
    s = seeded.settings_store.settings
    assert s.telegram_bot_token == BOT_TOKEN
    assert s.seerr_api_key == SEERR_KEY
    assert s.admin_telegram_id == ADMIN_ID
    assert s.admin.is_set()


async def test_post_blank_fields_with_nothing_stored_is_refused(empty):
    html, data = await _form(empty)
    assert "Already set" not in html
    r = await empty.post("/admin/setup", data=data)
    assert r.status == 400
    assert not empty.settings_store.settings.admin.is_set()


async def test_post_typed_values_replace_stored(seeded):
    _, data = await _form(seeded, telegram_bot_token="999:new",
                          admin_telegram_id="77", seerr_api_key="newkey")
    r = await seeded.post("/admin/setup", data=data)
    assert r.status == 200
    s = seeded.settings_store.settings
    assert (s.telegram_bot_token, s.admin_telegram_id, s.seerr_api_key) == (
        "999:new", 77, "newkey")


async def test_get_drops_required_only_for_stored_fields(seeded):
    html, _ = await _form(seeded)
    for name in ("telegram_bot_token", "admin_telegram_id", "seerr_api_key"):
        tag = re.search(r'<input[^>]*name="%s"[^>]*>' % name, html).group(0)
        assert "required" not in tag
    assert "http://192.0.2.10:5056" in html


async def test_partly_seeded_store_still_requires_missing_key(tmp_path, monkeypatch):
    c = await _make_client(tmp_path, monkeypatch, seeded=True)
    try:
        c.settings_store.settings.seerr_api_key = ""
        html, data = await _form(c)
        tag = re.search(r'<input[^>]*name="seerr_api_key"[^>]*>', html).group(0)
        assert "required" in tag
        r = await c.post("/admin/setup", data=data)
        body = await r.text()
        assert r.status == 400
        assert BOT_TOKEN not in body and str(ADMIN_ID) not in body
    finally:
        await c.close()
