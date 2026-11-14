"""Tests for /admin/autofix POST: allow-all + unlimited flag parsing and the
retention of the underlying allowlist / daily-limit values."""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import webui

SECRET = b"0123456789abcdef0123456789abcdef"
# Signed AND session-bound since 0.13.0. Pin one admin session cookie so its
# signature is stable, and bind the CSRF token to it.
_SESSION = webui._make_session_cookie(SECRET, "admin")
CSRF = webui.generate_csrf_token(SECRET, _SESSION.rsplit(".", 1)[-1])


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.delenv("USHER_WEBHOOK_SECRET", raising=False)
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
        c.store = store  # type: ignore[attr-defined]
        yield c


def _cookies():
    return {
        webui.SESSION_COOKIE: _SESSION,
        "usher_csrf": CSRF,
    }


async def _post(c, fields):
    data = {"csrf_token": CSRF, "daily_autofix_limit": "3", **fields}
    return await c.post("/admin/autofix", data=data, cookies=_cookies())


async def test_allow_all_checked_sets_flag_and_retains_ids(client):
    r = await _post(client, {
        "allowed_autofix_telegram_ids": "111,222",
        "autofix_allow_all": "on",
    })
    assert r.status == 200
    s = client.store.settings
    assert s.autofix_allow_all is True
    assert s.allowed_autofix_telegram_ids == [111, 222]  # retained


async def test_allow_all_unchecked_clears_flag(client):
    client.store.settings.autofix_allow_all = True
    r = await _post(client, {"allowed_autofix_telegram_ids": "333"})
    assert r.status == 200
    s = client.store.settings
    assert s.autofix_allow_all is False
    assert s.allowed_autofix_telegram_ids == [333]


async def test_unlimited_checked_sets_flag_and_retains_limit(client):
    r = await _post(client, {
        "daily_autofix_limit": "9",
        "daily_autofix_unlimited": "on",
    })
    assert r.status == 200
    s = client.store.settings
    assert s.daily_autofix_unlimited is True
    assert s.daily_autofix_limit == 9  # retained


async def test_unlimited_with_blank_limit_does_not_error(client):
    client.store.settings.daily_autofix_limit = 5
    r = await _post(client, {
        "daily_autofix_limit": "",
        "daily_autofix_unlimited": "on",
    })
    assert r.status == 200
    s = client.store.settings
    assert s.daily_autofix_unlimited is True
    assert s.daily_autofix_limit == 5  # prior value kept, not an error


async def test_invalid_limit_without_unlimited_is_400(client):
    r = await _post(client, {"daily_autofix_limit": "abc"})
    assert r.status == 400
    assert client.store.settings.daily_autofix_unlimited is False


async def test_blocklist_on_repeat_is_off_by_default_and_saves_when_checked(client):
    assert client.store.settings.autofix_blocklist_on_repeat is False
    r = await _post(client, {"autofix_blocklist_on_repeat": "on"})
    assert r.status == 200
    assert client.store.settings.autofix_blocklist_on_repeat is True
    r = await _post(client, {})
    assert client.store.settings.autofix_blocklist_on_repeat is False


async def test_the_tab_renders_the_switch_with_its_three_risks(client):
    client.store.settings.autofix_blocklist_on_repeat = True
    r = await client.get("/admin?tab=autofix", cookies=_cookies())
    body = await r.text()
    import re
    box = re.search(r'<input[^>]*name="autofix_blocklist_on_repeat"[^>]*>', body)
    assert box and "checked" in box.group(0)
    client.store.settings.autofix_blocklist_on_repeat = False
    off = await (await client.get("/admin?tab=autofix", cookies=_cookies())).text()
    box = re.search(r'<input[^>]*name="autofix_blocklist_on_repeat"[^>]*>', off)
    assert box and "checked" not in box.group(0)
    assert "Auto-fix blocklists on a repeat" in body
    assert "good release can be blocklisted when the download client was at fault" in body
    assert "only available release can be removed" in body
    assert "blocklist grows" in body
