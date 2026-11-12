"""A scheduled restart (restore, token change) blocks settings saves and ends
abandoned link polls, so the restart is not held up or overwritten."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import procutil
import webui
from bot import link_flow

SECRET = b"0123456789abcdef0123456789abcdef"
_SESSION = webui._make_session_cookie(SECRET, "admin")
CSRF = webui.generate_csrf_token(SECRET, _SESSION.rsplit(".", 1)[-1])


@pytest.fixture(autouse=True)
def _reset_flag():
    procutil.reset_restart_pending()
    yield
    procutil.reset_restart_pending()


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.delenv("USHER_WEBHOOK_SECRET", raising=False)
    settings_path = tmp_path / "settings.json"
    store = webui.SettingsStore(settings_path)
    store.settings.admin.username = "admin"
    store.settings.admin.password_hash = "x"
    store.settings.seerr_url = "http://seerr.test"
    store.settings.seerr_api_key = "key"
    store.save()
    app = web.Application()
    webui.attach_webui(app, settings_store=store, session_secret=SECRET,
                       data_dir=tmp_path, settings_path=settings_path,
                       db_path=tmp_path / "mappings.sqlite")
    async with TestClient(TestServer(app)) as c:
        c.settings_store = store  # type: ignore[attr-defined]
        yield c


def _cookies():
    return {webui.SESSION_COOKIE: _SESSION, "usher_csrf": CSRF}


async def test_schedule_clean_exit_sets_flag(monkeypatch):
    monkeypatch.setattr("procutil.os.kill", lambda *a: None)
    assert not procutil.restart_pending()
    procutil.schedule_clean_exit(60.0)
    assert procutil.restart_pending()


async def test_tab_post_refused_while_restart_pending(client):
    procutil.mark_restart_pending()
    resp = await client.post(
        "/admin/seerr",
        data={"csrf_token": CSRF, "seerr_url": "http://evil.test",
              "seerr_api_key": "k2"},
        cookies=_cookies(), allow_redirects=False)
    assert resp.status == 503
    assert "Usher is restarting. Refresh in a moment." in await resp.text()
    assert client.settings_store.settings.seerr_url == "http://seerr.test"


async def test_newplex_autosave_refused_while_restart_pending(client, monkeypatch):
    s = client.settings_store.settings
    s.seerr_new_plex_login_ack = True

    class FakeSeerr:
        def __init__(self, *a, **k):
            pass

        async def get_main_settings(self):
            return {"newPlexLogin": False}

        async def close(self):
            pass

    monkeypatch.setattr(webui, "SeerrClient", FakeSeerr)
    procutil.mark_restart_pending()
    resp = await client.get("/admin/seerr/newplex-warning", cookies=_cookies())
    assert resp.status == 503
    assert s.seerr_new_plex_login_ack is True


async def test_get_pages_still_served_while_restart_pending(client):
    procutil.mark_restart_pending()
    resp = await client.get("/admin", cookies=_cookies(), allow_redirects=False)
    assert resp.status == 200


@pytest.mark.parametrize("source", ["bot_data", "procutil"])
async def test_poll_exits_when_shutting_down(monkeypatch, source):
    monkeypatch.setattr(link_flow, "PLEX_POLL_INTERVAL_S", 0)
    plex = SimpleNamespace(poll_pin=AsyncMock(return_value=None))
    bot_data = {}
    ctx = SimpleNamespace(user_data={"link_active_loop": "L"},
                          application=SimpleNamespace(bot_data=bot_data),
                          bot=SimpleNamespace())
    if source == "bot_data":
        bot_data["shutting_down"] = True
    else:
        procutil.mark_restart_pending()
    result = await asyncio.wait_for(
        link_flow._poll_with_cancel(plex, 1, 1000, ctx, "L"), timeout=2)
    assert result is None
    assert plex.poll_pin.await_count == 0


async def test_login_post_refused_while_restart_pending(client):
    procutil.mark_restart_pending()
    resp = await client.post("/admin/login", data={"username": "admin", "password": "x"},
                             allow_redirects=False)
    assert resp.status == 503
