"""Admin panel hardening: login bursts, unauthenticated body limits,
one restore at a time, and the webhook self-test address."""
from __future__ import annotations

import asyncio
import gzip
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import webui
from auth_util import LoginThrottle, THROTTLE_MAX_FAILURES

SECRET = b"0123456789abcdef0123456789abcdef"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.delenv("USHER_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(webui, "_throttle", LoginThrottle())
    monkeypatch.setattr(webui, "_ip_throttle", LoginThrottle(max_failures=1000))
    settings_path = tmp_path / "settings.json"
    store = webui.SettingsStore(settings_path)
    store.settings.admin.username = "admin"
    store.settings.admin.password_hash = webui.hash_password("right-password")
    store.save()
    app = web.Application()
    webui.attach_webui(
        app, settings_store=store, session_secret=SECRET, data_dir=tmp_path,
        settings_path=settings_path, db_path=tmp_path / "mappings.sqlite",
        http_port=8765,
    )
    async with TestClient(TestServer(app)) as c:
        yield c


def _login_form(password: str, csrf: str) -> dict:
    return {"username": "admin", "password": password, "csrf_token": csrf}


async def test_parallel_login_burst_still_hits_the_lockout(client):
    csrf = webui.generate_csrf_token(SECRET)
    burst = THROTTLE_MAX_FAILURES + 5

    async def attempt():
        r = await client.post("/admin/login", data=_login_form("wrong", csrf),
                              cookies={"usher_csrf": csrf},
                              allow_redirects=False)
        return r.status

    statuses = await asyncio.gather(*(attempt() for _ in range(burst)))
    assert statuses.count(401) == THROTTLE_MAX_FAILURES
    assert statuses.count(429) == burst - THROTTLE_MAX_FAILURES


async def test_right_password_after_a_parallel_burst_is_locked_out(client):
    csrf = webui.generate_csrf_token(SECRET)

    async def attempt(password):
        r = await client.post("/admin/login", data=_login_form(password, csrf),
                              cookies={"usher_csrf": csrf},
                              allow_redirects=False)
        return r.status

    burst = await asyncio.gather(
        *(attempt("wrong") for _ in range(THROTTLE_MAX_FAILURES + 2)))
    assert 500 not in burst
    assert await attempt("right-password") == 429


async def test_login_lock_belongs_to_the_app(client):
    # One lock per panel app (so per event loop), and the form is read
    # before it is taken: a stalled upload never holds up other logins.
    assert isinstance(client.server.app["login_lock"], asyncio.Lock)
    assert not client.server.app["login_lock"].locked()


async def test_repeated_content_encoding_headers_are_refused(client):
    csrf = webui.generate_csrf_token(SECRET)
    body = f"username=admin&password=x&csrf_token={csrf}".encode()
    r = await client.post(
        "/admin/login", data=body, cookies={"usher_csrf": csrf},
        headers=[("Content-Type", "application/x-www-form-urlencoded"),
                 ("Content-Encoding", "identity"),
                 ("Content-Encoding", "gzip")])
    assert r.status == 415


def test_unsized_body_is_refused():
    req = SimpleNamespace(content_length=None, headers={})
    assert webui._unauth_form_rejection(req).status == 413


async def test_setup_post_applies_the_same_body_gate(client):
    admin = client.server.app["settings_store"].settings.admin
    admin.username = ""
    admin.password_hash = ""
    r = await client.post(
        "/admin/setup", data=b"username=a",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Content-Encoding": "gzip"}, allow_redirects=False)
    assert r.status == 415


async def test_non_utf8_login_form_is_a_400_not_a_500(client):
    csrf = webui.generate_csrf_token(SECRET)
    r = await client.post(
        "/admin/login", data=b"username=admin&password=" + bytes([0xFF]),
        cookies={"usher_csrf": csrf},
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status == 400


async def test_compressed_login_body_is_refused(client):
    csrf = webui.generate_csrf_token(SECRET)
    body = gzip.compress(f"username=admin&password=x&csrf_token={csrf}".encode())
    r = await client.post(
        "/admin/login", data=body, cookies={"usher_csrf": csrf},
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Content-Encoding": "gzip"})
    assert r.status == 415


async def test_oversized_login_body_is_refused(client):
    csrf = webui.generate_csrf_token(SECRET)
    big = "x" * (webui.UNAUTH_FORM_MAX_BYTES + 1)
    r = await client.post("/admin/login",
                          data={**_login_form(big, csrf)},
                          cookies={"usher_csrf": csrf})
    assert r.status == 413


async def test_restore_refused_while_another_restore_runs(client, monkeypatch):
    lock = asyncio.Lock()
    monkeypatch.setattr(webui, "_restore_error",
                        lambda request, message, status=400:
                        web.Response(text=message, status=status))
    await lock.acquire()
    try:
        resp = await webui.restore_upload(
            SimpleNamespace(app={"restore_lock": lock}))
    finally:
        lock.release()
    assert resp.status == 409


def _req_with_sockname(sockname):
    transport = SimpleNamespace(get_extra_info=lambda key: sockname)
    return SimpleNamespace(transport=transport)


@pytest.mark.parametrize("sockname, host", [
    (("0.0.0.0", 8765), "127.0.0.1"),
    (("::", 8765, 0, 0), "127.0.0.1"),
    (("192.0.2.20", 8765), "192.0.2.20"),
    (("2001:db8::5", 8765, 0, 0), "[2001:db8::5]"),
    (None, "127.0.0.1"),
])
def test_webhook_self_test_uses_the_local_socket_address(sockname, host):
    assert webui._self_test_host(_req_with_sockname(sockname)) == host


# --- crafted non-ASCII values are rejections, not crashes --------------------

def test_safe_compare_handles_non_ascii_and_surrogates():
    from auth_util import safe_compare
    assert safe_compare("abc", "abc")
    assert not safe_compare("café", "cafe")
    assert not safe_compare("bad\udcff", "bad")


def test_non_ascii_session_cookie_is_rejected_not_raised():
    good = webui._make_session_cookie(SECRET, "admin", 0)
    body = good.rsplit(".", 1)[0]
    assert webui._verify_session_cookie(SECRET, body + ".sigé") is None


async def test_non_ascii_session_cookie_redirects_to_login(client):
    r = await client.get("/admin", allow_redirects=False,
                         cookies={webui.SESSION_COOKIE: "a.é"})
    assert r.status in (302, 303)


def test_csrf_cookie_is_http_only():
    from auth_util import attach_csrf_cookie
    resp = web.Response()
    attach_csrf_cookie(resp, "tok", secure=False)
    assert resp.cookies["usher_csrf"]["httponly"] is True


@pytest.mark.parametrize("cookies", [
    {webui.SESSION_COOKIE: "\udcff.x"},
    {webui.SESSION_COOKIE: "a.\udcff"},
    {"usher_csrf": "\udcff.x"},
])
def test_undecodable_cookie_bytes_never_raise(cookies):
    from auth_util import csrf_for_request
    req = SimpleNamespace(cookies=cookies, app={"session_secret": SECRET})
    assert webui._verify_session_cookie(
        SECRET, cookies.get(webui.SESSION_COOKIE, "a.b")) is None
    assert isinstance(csrf_for_request(req), str)


def test_csrf_compare_rejects_non_ascii_without_raising():
    from auth_util import validate_csrf
    req = SimpleNamespace(cookies={"usher_csrf": "toké"},
                          app={"session_secret": SECRET})
    assert validate_csrf(req, "tok") is False
