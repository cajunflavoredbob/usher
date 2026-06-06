"""Tests for /admin/autofix POST: allow-all + unlimited flag parsing and the
retention of the underlying allowlist / daily-limit values."""
from __future__ import annotations

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
        c.store = store  # type: ignore[attr-defined]
        yield c


def _cookies():
    return {
        webui.SESSION_COOKIE: webui._make_session_cookie(SECRET, "admin"),
        "hermes_csrf": CSRF,
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
