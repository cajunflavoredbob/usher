"""Tests for the dismissible New-Plex-Sign-In warning: shown when Seerr's
newPlexLogin is on and unacknowledged, hidden after dismissal, re-armed by an
off -> on transition, and silent when Seerr is down/unconfigured/too old."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    store.settings.seerr_url = "http://seerr.test"
    store.settings.seerr_api_key = "key"
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
        c.settings_store = store  # type: ignore[attr-defined]
        yield c


def _cookies():
    return {
        webui.SESSION_COOKIE: webui._make_session_cookie(SECRET, "admin"),
        "hermes_csrf": CSRF,
    }


def _mock_seerr(monkeypatch, main_settings, *, raise_exc=None):
    """Replace webui.SeerrClient with a stub returning main_settings."""
    class FakeSeerr:
        def __init__(self, *a, **k):
            pass

        async def get_main_settings(self):
            if raise_exc is not None:
                raise raise_exc
            return main_settings

        async def close(self):
            pass

    monkeypatch.setattr(webui, "SeerrClient", FakeSeerr)


async def _check(client):
    resp = await client.get("/admin/seerr/newplex-warning", cookies=_cookies())
    assert resp.status == 200
    return (await resp.json())["show"]


async def test_shown_when_enabled_and_unacked(client, monkeypatch):
    _mock_seerr(monkeypatch, {"newPlexLogin": True})
    assert await _check(client) is True


async def test_hidden_after_dismiss(client, monkeypatch):
    _mock_seerr(monkeypatch, {"newPlexLogin": True})
    resp = await client.post("/admin/seerr/newplex-warning/dismiss",
                             data={"csrf_token": CSRF}, cookies=_cookies())
    assert resp.status == 200
    assert client.settings_store.settings.seerr_new_plex_login_ack is True
    assert await _check(client) is False


async def test_off_observation_rearms_dismissal(client, monkeypatch):
    """Dismiss -> setting turned off -> turned back on: warning returns."""
    client.settings_store.settings.seerr_new_plex_login_ack = True
    _mock_seerr(monkeypatch, {"newPlexLogin": False})
    assert await _check(client) is False
    assert client.settings_store.settings.seerr_new_plex_login_ack is False
    _mock_seerr(monkeypatch, {"newPlexLogin": True})
    assert await _check(client) is True


async def test_silent_when_seerr_unreachable(client, monkeypatch):
    _mock_seerr(monkeypatch, {}, raise_exc=RuntimeError("connect refused"))
    assert await _check(client) is False


async def test_silent_when_setting_absent(client, monkeypatch):
    """Older Seerr builds without the key: no warning, no crash."""
    _mock_seerr(monkeypatch, {"applicationTitle": "Seerr"})
    assert await _check(client) is False


async def test_silent_when_seerr_unconfigured(client, monkeypatch):
    client.settings_store.settings.seerr_url = ""
    called = []

    class Boom:
        def __init__(self, *a, **k):
            called.append(1)

    monkeypatch.setattr(webui, "SeerrClient", Boom)
    assert await _check(client) is False
    assert called == []  # no client even constructed


async def test_dismiss_requires_csrf(client, monkeypatch):
    resp = await client.post("/admin/seerr/newplex-warning/dismiss",
                             data={"csrf_token": "wrong"}, cookies=_cookies())
    assert resp.status == 403
    assert client.settings_store.settings.seerr_new_plex_login_ack is False
