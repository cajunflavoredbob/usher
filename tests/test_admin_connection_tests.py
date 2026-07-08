"""Tests for the /admin/test/* connection-test endpoints in webui.py.

Exercises the real auth + CSRF path end to end (forged session + CSRF cookies)
and monkeypatches the outbound clients / httpx so no network is touched.
"""
from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import webui

SECRET = b"0123456789abcdef0123456789abcdef"
CSRF = "csrf-token-value"


# --- fakes ------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeHTTP:
    """Stand-in for httpx.AsyncClient as an async context manager."""

    def __init__(self, *, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        if self._exc:
            raise self._exc
        return self._resp

    async def post(self, url, **kw):
        if self._exc:
            raise self._exc
        return self._resp


class _FakeArr:
    """Stand-in for SeerrClient/RadarrClient/SonarrClient (ping + close)."""

    def __init__(self, *a, version="9.9.9", exc=None, **kw):
        self._version = version
        self._exc = exc
        self.closed = False

    async def ping(self):
        if self._exc:
            raise self._exc
        return self._version

    async def close(self):
        self.closed = True


def _patch_httpx(monkeypatch, *, resp=None, exc=None):
    monkeypatch.setattr(webui.httpx, "AsyncClient",
                        lambda *a, **k: _FakeHTTP(resp=resp, exc=exc))


# --- app / client fixture ---------------------------------------------------

@pytest.fixture
async def client(tmp_path, monkeypatch):
    # Seed a settings store with an admin set (so auth_middleware lets us in)
    # and a known webhook secret.
    monkeypatch.delenv("HERMES_WEBHOOK_SECRET", raising=False)
    settings_path = tmp_path / "settings.json"
    store = webui.SettingsStore(settings_path)
    store.settings.admin.username = "admin"
    store.settings.admin.password_hash = "x"  # is_set() only checks truthiness
    store.settings.webhook_secret = "saved-secret"
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


def _auth_cookies():
    return {
        webui.SESSION_COOKIE: webui._make_session_cookie(SECRET, "admin"),
        "hermes_csrf": CSRF,
    }


async def _post(c, which, fields):
    data = dict(fields)
    data["csrf_token"] = CSRF
    return await c.post(f"/admin/test/{which}", data=data, cookies=_auth_cookies())


async def _body(resp):
    return json.loads(await resp.text())


# --- auth / csrf ------------------------------------------------------------

async def test_requires_login(client):
    # No session cookie -> middleware redirects to login (302).
    r = await client.post("/admin/test/seerr", data={"csrf_token": CSRF},
                          cookies={"hermes_csrf": CSRF}, allow_redirects=False)
    assert r.status == 302


async def test_rejects_bad_csrf(client):
    r = await client.post("/admin/test/seerr",
                          data={"csrf_token": "wrong", "seerr_url": "http://x",
                                "seerr_api_key": "k"},
                          cookies=_auth_cookies())
    assert r.status == 403
    assert (await _body(r))["ok"] is False


# --- telegram ---------------------------------------------------------------

async def test_telegram_pass(client, monkeypatch):
    _patch_httpx(monkeypatch,
                 resp=_FakeResp(200, {"ok": True, "result": {"username": "mybot"}}))
    r = await _post(client, "telegram", {"telegram_bot_token": "123:abc"})
    body = await _body(r)
    assert body["ok"] is True
    assert "mybot" in body["detail"]


async def test_telegram_fail_bad_token(client, monkeypatch):
    _patch_httpx(monkeypatch,
                 resp=_FakeResp(401, {"ok": False, "description": "Unauthorized"}))
    r = await _post(client, "telegram", {"telegram_bot_token": "bad"})
    body = await _body(r)
    assert body["ok"] is False
    assert "Unauthorized" in body["detail"]


async def test_telegram_no_token(client):
    r = await _post(client, "telegram", {"telegram_bot_token": "  "})
    body = await _body(r)
    assert body["ok"] is False
    assert "No bot token" in body["detail"]


# --- seerr ------------------------------------------------------------------

async def test_seerr_pass(client, monkeypatch):
    monkeypatch.setattr(webui, "SeerrClient",
                        lambda *a, **k: _FakeArr(version="1.2.3"))
    r = await _post(client, "seerr",
                    {"seerr_url": "http://seerr", "seerr_api_key": "k"})
    body = await _body(r)
    assert body["ok"] is True
    assert "1.2.3" in body["detail"]


async def test_seerr_fail(client, monkeypatch):
    monkeypatch.setattr(webui, "SeerrClient",
                        lambda *a, **k: _FakeArr(exc=RuntimeError("boom")))
    r = await _post(client, "seerr",
                    {"seerr_url": "http://seerr", "seerr_api_key": "k"})
    assert (await _body(r))["ok"] is False


async def test_seerr_missing_fields(client):
    r = await _post(client, "seerr", {"seerr_url": "", "seerr_api_key": ""})
    body = await _body(r)
    assert body["ok"] is False
    assert "required" in body["detail"].lower()


# --- autofix ----------------------------------------------------------------

async def test_autofix_both_pass(client, monkeypatch):
    monkeypatch.setattr(webui, "RadarrClient",
                        lambda *a, **k: _FakeArr(version="5.0"))
    monkeypatch.setattr(webui, "SonarrClient",
                        lambda *a, **k: _FakeArr(version="4.0"))
    r = await _post(client, "autofix", {
        "radarr_url": "http://r", "radarr_api_key": "rk",
        "sonarr_url": "http://s", "sonarr_api_key": "sk",
    })
    body = await _body(r)
    assert body["ok"] is True
    assert "Radarr: v5.0" in body["detail"]
    assert "Sonarr: v4.0" in body["detail"]


async def test_autofix_one_fails(client, monkeypatch):
    monkeypatch.setattr(webui, "RadarrClient",
                        lambda *a, **k: _FakeArr(version="5.0"))
    monkeypatch.setattr(webui, "SonarrClient",
                        lambda *a, **k: _FakeArr(exc=RuntimeError("down")))
    r = await _post(client, "autofix", {
        "radarr_url": "http://r", "radarr_api_key": "rk",
        "sonarr_url": "http://s", "sonarr_api_key": "sk",
    })
    body = await _body(r)
    assert body["ok"] is False
    assert "Radarr: v5.0" in body["detail"]


async def test_autofix_none_configured(client):
    r = await _post(client, "autofix", {"radarr_url": "", "sonarr_url": ""})
    body = await _body(r)
    assert body["ok"] is False
    assert "Neither" in body["detail"]


async def test_autofix_url_without_key(client):
    r = await _post(client, "autofix",
                    {"radarr_url": "http://r", "radarr_api_key": ""})
    body = await _body(r)
    assert body["ok"] is False
    assert "API key missing" in body["detail"]


# --- webhook ----------------------------------------------------------------

async def test_webhook_pass(client, monkeypatch):
    _patch_httpx(monkeypatch, resp=_FakeResp(200))
    r = await _post(client, "webhook", {})
    body = await _body(r)
    assert body["ok"] is True


async def test_webhook_401(client, monkeypatch):
    _patch_httpx(monkeypatch, resp=_FakeResp(401))
    r = await _post(client, "webhook", {})
    body = await _body(r)
    assert body["ok"] is False
    assert "401" in body["detail"]


async def test_webhook_no_saved_secret(client, monkeypatch):
    client.store.settings.webhook_secret = ""
    r = await _post(client, "webhook", {})
    body = await _body(r)
    assert body["ok"] is False
    assert "No saved secret" in body["detail"]
