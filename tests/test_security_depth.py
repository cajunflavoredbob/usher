"""Tests for session invalidation on password change, the loopback-only
webhook self-test, the unencrypted-backup acknowledgement gate, and the
no-store header on /admin redirects."""
from __future__ import annotations

from http.cookies import SimpleCookie
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import webui

SECRET = b"0123456789abcdef0123456789abcdef"
# CSRF tokens are HMAC-signed AND session-bound since 0.13.0. Pin one
# session cookie per pwd_ver so its signature (which includes exp) is stable
# across the test, and bind the CSRF token to it.
def _csrf_bound(session_cookie: str) -> str:
    return webui.generate_csrf_token(SECRET, session_cookie.rsplit(".", 1)[-1])


_SESSION0 = webui._make_session_cookie(SECRET, "admin", 0)
_SESSION1 = webui._make_session_cookie(SECRET, "admin", 1)
# One fixed token per session (generate_csrf_token randomizes each call, so
# the cookie and the form value must be the SAME pinned token to double-submit).
CSRF = _csrf_bound(_SESSION0)
_CSRF1 = _csrf_bound(_SESSION1)


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.delenv("USHER_WEBHOOK_SECRET", raising=False)
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
    if pwd_ver == 1:
        return {webui.SESSION_COOKIE: _SESSION1, "usher_csrf": _CSRF1}
    return {webui.SESSION_COOKIE: _SESSION0, "usher_csrf": CSRF}


# --- sessions die on password change ------------------------------------------


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


# --- webhook self-test hits loopback only -------------------------------------


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


# --- unencrypted backup needs explicit acknowledgement ------------------------


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
    assert "usher-backup" in resp.headers["Content-Disposition"]


# --- /admin redirects keep Cache-Control: no-store ----------------------------


def _set_cookies(resp) -> SimpleCookie:
    sc: SimpleCookie = SimpleCookie()
    for set_cookie in resp.headers.getall("Set-Cookie", []):
        sc.load(set_cookie)
    return sc


@pytest.mark.parametrize("path,admin_set,logged_in,location", [
    ("/admin", True, False, "/admin/login"),
    ("/admin", False, False, "/admin/setup"),
    ("/admin/setup", True, False, "/admin/login"),
    ("/admin/login", True, True, "/admin"),
])
async def test_admin_redirects_carry_no_store(client, path, admin_set,
                                              logged_in, location):
    if not admin_set:
        # First run: no admin on file yet.
        client.settings_store.settings.admin.password_hash = ""
    resp = await client.get(path, cookies=_cookies() if logged_in else None,
                            allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == location
    assert resp.headers["Cache-Control"] == "no-store"


async def test_login_redirect_carries_no_store_and_session(client):
    # Login-page CSRF tokens use the no-session binding.
    csrf = webui.generate_csrf_token(SECRET)
    resp = await client.post("/admin/login", data={
        "username": "admin", "password": "old-password", "csrf_token": csrf,
    }, cookies={"usher_csrf": csrf}, allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/admin"
    assert resp.headers["Cache-Control"] == "no-store"
    session = _set_cookies(resp)[webui.SESSION_COOKIE].value
    assert webui._verify_session_cookie(SECRET, session, 0) == "admin"


async def test_logout_redirect_carries_no_store_and_clears_session(
        client, monkeypatch):
    # Throwaway denylist keeps the pinned session valid for later tests.
    monkeypatch.setattr(webui, "_revoked_sessions", {})
    resp = await client.post("/admin/logout", data={"csrf_token": CSRF},
                             cookies=_cookies(), allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/admin/login"
    assert resp.headers["Cache-Control"] == "no-store"
    session = _set_cookies(resp)[webui.SESSION_COOKIE]
    assert session.value == ""
    assert session["max-age"] == "0"
