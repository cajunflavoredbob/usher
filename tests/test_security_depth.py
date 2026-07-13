"""Tests for the stage-5 audit fixes: session invalidation on password change
(P2-10), the loopback-only webhook self-test (P2-1), and the unencrypted-
backup acknowledgement gate (P2-11)."""
from __future__ import annotations

from types import SimpleNamespace

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
    store.settings.admin.password_hash = webui.hash_password("old-password")
    store.save()

    app = web.Application()
    webui.attach_webui(
        app,
        settings_store=store,
        session_secret=SECRET,
        data_dir=tmp_path,
        settings_path=settings_path,
        db_path=tmp_path / "mappings.sqlite",
        http_port=8765,
    )
    async with TestClient(TestServer(app)) as c:
        c.settings_store = store  # type: ignore[attr-defined]
        yield c


def _cookies(pwd_ver: int = 0):
    return {
        webui.SESSION_COOKIE: webui._make_session_cookie(SECRET, "admin", pwd_ver),
        "hermes_csrf": CSRF,
    }


# --- P2-10: sessions die on password change --------------------------------------


def test_session_invalidated_by_password_version_bump():
    cookie = webui._make_session_cookie(SECRET, "admin", 0)
    assert webui._verify_session_cookie(SECRET, cookie, 0) == "admin"
    assert webui._verify_session_cookie(SECRET, cookie, 1) is None


def test_legacy_cookie_without_version_field_counts_as_v0():
    """Cookies minted before 0.12.0 have no "v"; they stay valid on installs
    that never changed the password (version 0) and die after one change."""
    import json as _json
    import time as _time
    payload = _json.dumps({"u": "admin",
                           "exp": int(_time.time()) + 3600}).encode()
    body = webui._b64(payload)
    sig = webui._b64(webui._sign(SECRET, body.encode()))
    legacy = f"{body}.{sig}"
    assert webui._verify_session_cookie(SECRET, legacy, 0) == "admin"
    assert webui._verify_session_cookie(SECRET, legacy, 1) is None


async def test_password_change_kills_old_session(client):
    resp = await client.post("/admin/password", data={
        "csrf_token": CSRF, "current": "old-password",
        "new": "new-password-123", "confirm": "new-password-123",
    }, cookies=_cookies(pwd_ver=0))
    assert resp.status == 200
    assert client.settings_store.settings.admin.password_version == 1
    # The pre-change cookie (version 0) no longer authenticates.
    resp2 = await client.get("/admin", cookies=_cookies(pwd_ver=0),
                             allow_redirects=False)
    assert resp2.status == 302
    assert "/admin/login" in resp2.headers["Location"]
    # A cookie at the new version does.
    resp3 = await client.get("/admin", cookies=_cookies(pwd_ver=1),
                             allow_redirects=False)
    assert resp3.status == 200


# --- P2-1: webhook self-test hits loopback only ------------------------------------


async def test_webhook_selftest_posts_to_loopback(client, monkeypatch):
    store = client.settings_store
    store.settings.webhook_secret = "s3cret"
    store.save()

    captured = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured["auth"] = kwargs.get("headers", {}).get("Authorization")
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(webui.httpx, "AsyncClient", FakeAsyncClient)
    resp = await client.post("/admin/test/webhook",
                             data={"csrf_token": CSRF},
                             cookies=_cookies())
    assert resp.status == 200
    # The secret-bearing POST must target loopback, never a Host-derived URL.
    assert captured["url"] == "http://127.0.0.1:8765/webhook/seerr"
    assert captured["auth"] == "s3cret"


# --- P2-11: unencrypted backup needs explicit acknowledgement -----------------------


async def test_backup_without_passphrase_or_ack_is_refused(client):
    resp = await client.post("/admin/backup",
                             data={"csrf_token": CSRF, "passphrase": ""},
                             cookies=_cookies())
    assert resp.status == 400


async def test_backup_with_passphrase_needs_no_ack(client):
    resp = await client.post("/admin/backup",
                             data={"csrf_token": CSRF, "passphrase": "hunter22"},
                             cookies=_cookies())
    assert resp.status == 200
    assert "hermes-backup" in resp.headers["Content-Disposition"]
