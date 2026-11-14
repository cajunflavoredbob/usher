"""Login and setup form input limits: a long username never becomes a
throttle key or a long audit line, and a value that cannot be encoded as
UTF-8 is a failed login (or CSRF mismatch), never a 500."""
from __future__ import annotations

import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import auth_util
import webui
from auth_util import LoginThrottle, THROTTLE_IP_MAX_FAILURES, safe_compare
from settings import verify_password

SECRET = b"0123456789abcdef0123456789abcdef"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.delenv("USHER_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(webui, "_throttle", LoginThrottle())
    monkeypatch.setattr(
        webui, "_ip_throttle", LoginThrottle(max_failures=THROTTLE_IP_MAX_FAILURES))
    monkeypatch.setattr(webui, "_device_throttle", LoginThrottle())
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
        c.settings_store = store  # type: ignore[attr-defined]
        yield c


@pytest.fixture
async def proxied_client(tmp_path, monkeypatch):
    """Same app behind a trusted proxy at the loopback address, so the test
    can pick the client address through X-Forwarded-For."""
    monkeypatch.delenv("USHER_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(webui, "_throttle", LoginThrottle())
    monkeypatch.setattr(
        webui, "_ip_throttle", LoginThrottle(max_failures=THROTTLE_IP_MAX_FAILURES))
    monkeypatch.setattr(webui, "_device_throttle", LoginThrottle())
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
        trusted_proxies=auth_util.parse_trusted_proxies("127.0.0.1/32"),
    )
    async with TestClient(TestServer(app)) as c:
        yield c


def _post_login(client, username, password):
    csrf = webui.generate_csrf_token(SECRET)
    return client.post(
        "/admin/login",
        data={"username": username, "password": password, "csrf_token": csrf},
        cookies={"usher_csrf": csrf}, allow_redirects=False)


def test_safe_compare_never_raises_on_lone_surrogate():
    assert safe_compare("\ud800", "\ud800") is True
    assert safe_compare("\ud800", "abc") is False
    assert safe_compare("abc", "\udc80") is False


def test_verify_password_false_on_lone_surrogate():
    stored = webui.hash_password("right-password")
    assert verify_password("\ud800", stored) is False


async def test_long_username_stores_no_key_logs_short_and_counts_on_ip(
        client, caplog):
    caplog.set_level(logging.WARNING, logger="usher.audit")
    long_name = "u" * 60_000
    resp = await _post_login(client, long_name, "right-password")
    assert resp.status == 401
    assert "Invalid credentials" in await resp.text()
    assert webui._throttle._failures == {}
    assert len(webui._ip_throttle._failures) == 1
    assert sum(len(v) for v in webui._ip_throttle._failures.values()) == 1
    assert all(len(r.getMessage()) < 1000 for r in caplog.records)
    assert any("event=login_fail" in r.getMessage() for r in caplog.records)


async def test_long_username_floods_reach_ip_lockout(client):
    for _ in range(THROTTLE_IP_MAX_FAILURES):
        assert (await _post_login(client, "u" * 500, "x")).status == 401
    assert (await _post_login(client, "admin", "right-password")).status == 429


async def test_username_at_the_limit_is_still_checked(client):
    resp = await _post_login(client, "a" * auth_util.MAX_USERNAME_CHARS, "x")
    assert resp.status == 401
    assert len(webui._throttle._failures) == 1


async def _utf7_post(client, path, fields, csrf_cookie):
    boundary = "xBOUNDARYx"
    body = b""
    for name, value in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="{name}"\r\nContent-Type: text/plain; '
                 f"charset=utf-7\r\n\r\n{value}\r\n").encode()
    body += f"--{boundary}--\r\n".encode()
    return await client.post(
        path, data=body, cookies={"usher_csrf": csrf_cookie},
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        allow_redirects=False)


async def test_utf7_lone_surrogate_password_is_a_failed_login(client):
    csrf = webui.generate_csrf_token(SECRET)
    resp = await _utf7_post(
        client, "/admin/login",
        {"username": "admin", "password": "+2AA-", "csrf_token": csrf}, csrf)
    assert resp.status == 401
    assert len(webui._ip_throttle._failures) == 1


async def test_utf7_lone_surrogate_csrf_is_rejected_not_500(client):
    csrf = webui.generate_csrf_token(SECRET)
    resp = await _utf7_post(
        client, "/admin/login",
        {"username": "admin", "password": "x", "csrf_token": "+2AA-"}, csrf)
    assert resp.status in (400, 403)


async def test_setup_rejects_long_username(tmp_path, monkeypatch):
    monkeypatch.delenv("USHER_WEBHOOK_SECRET", raising=False)
    settings_path = tmp_path / "settings.json"
    store = webui.SettingsStore(settings_path)
    app = web.Application()
    webui.attach_webui(
        app, settings_store=store, session_secret=SECRET, data_dir=tmp_path,
        settings_path=settings_path, db_path=tmp_path / "mappings.sqlite",
        http_port=8765,
    )
    token = auth_util.load_or_create_setup_token(tmp_path)
    csrf = webui.generate_csrf_token(SECRET)
    async with TestClient(TestServer(app)) as c:
        resp = await c.post(
            "/admin/setup",
            data={"username": "u" * 5000, "password": "p" * 20,
                  "confirm": "p" * 20, "csrf_token": csrf,
                  "setup_token": token},
            cookies={"usher_csrf": csrf}, allow_redirects=False)
        assert resp.status == 400
        assert "Username must be" in await resp.text()
    assert not store.settings.admin.is_set()


# --- ticket 37: IPv6 /64 throttle key, password-change race ----------------

def _post_login_from(client, forwarded_for, username, password):
    csrf = webui.generate_csrf_token(SECRET)
    return client.post(
        "/admin/login",
        data={"username": username, "password": password, "csrf_token": csrf},
        cookies={"usher_csrf": csrf},
        headers={"X-Forwarded-For": forwarded_for}, allow_redirects=False)


async def test_ipv6_client_rotating_inside_one_64_hits_the_ip_lockout(
        proxied_client):
    # Over-long usernames fail without PBKDF2 but still count on the address.
    for i in range(THROTTLE_IP_MAX_FAILURES):
        resp = await _post_login_from(
            proxied_client, f"2001:db8:1:2::{i + 1:x}", "u" * 500, "x")
        assert resp.status == 401
    resp = await _post_login_from(
        proxied_client, "2001:db8:1:2:ffff::9", "admin", "right-password")
    assert resp.status == 429
    assert len(webui._ip_throttle._failures) == 1


async def test_ipv6_in_another_64_is_not_locked_out(proxied_client):
    for i in range(THROTTLE_IP_MAX_FAILURES):
        await _post_login_from(
            proxied_client, f"2001:db8:1:2::{i + 1:x}", "u" * 500, "x")
    resp = await _post_login_from(
        proxied_client, "2001:db8:1:3::1", "admin", "right-password")
    assert resp.status == 302


async def test_ipv4_mapped_ipv6_shares_the_ipv4_bucket(proxied_client):
    for i in range(THROTTLE_IP_MAX_FAILURES):
        addr = "198.51.100.7" if i % 2 else "::ffff:198.51.100.7"
        await _post_login_from(proxied_client, addr, "u" * 500, "x")
    assert len(webui._ip_throttle._failures) == 1
    resp = await _post_login_from(
        proxied_client, "198.51.100.7", "admin", "right-password")
    assert resp.status == 429


async def test_audit_lines_keep_the_full_ipv6_address(proxied_client, caplog):
    caplog.set_level(logging.WARNING, logger="usher.audit")
    await _post_login_from(proxied_client, "2001:db8:1:2::abcd", "u" * 500, "x")
    assert any("ip=2001:db8:1:2::abcd" in r.getMessage()
               for r in caplog.records)


def _hold_first_verify(monkeypatch):
    """Make the first verify_password call wait until released, so a
    password change can be sent while a login is mid-verification."""
    import threading
    started, release = threading.Event(), threading.Event()
    real = webui.verify_password
    calls = {"n": 0}

    def gated(password, stored):
        calls["n"] += 1
        if calls["n"] == 1:
            started.set()
            assert release.wait(10)
        return real(password, stored)

    monkeypatch.setattr(webui, "verify_password", gated)
    return started, release


async def _until(event):
    import asyncio
    for _ in range(500):
        if event.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("verify never started")


async def test_login_in_flight_during_password_change_gets_a_dead_session(
        client, monkeypatch):
    import asyncio
    started, release = _hold_first_verify(monkeypatch)
    login = asyncio.ensure_future(
        _post_login(client, "admin", "right-password"))
    await _until(started)
    session0 = webui._make_session_cookie(SECRET, "admin", 0)
    bound = webui.generate_csrf_token(SECRET, session0.rsplit(".", 1)[-1])
    change = asyncio.ensure_future(client.post(
        "/admin/password",
        data={"csrf_token": bound, "current": "right-password",
              "new": "brand-new-password-1", "confirm": "brand-new-password-1"},
        cookies={webui.SESSION_COOKIE: session0, "usher_csrf": bound},
        allow_redirects=False))
    await asyncio.sleep(0.5)  # let the change reach its write
    release.set()
    login_resp, change_resp = await asyncio.gather(login, change)
    assert change_resp.status == 200
    assert client.settings_store.settings.admin.password_version == 1
    assert login_resp.status == 302
    stale = login_resp.cookies[webui.SESSION_COOKIE].value
    probe = await client.get(
        "/admin", cookies={webui.SESSION_COOKIE: stale}, allow_redirects=False)
    assert probe.status == 302
    assert probe.headers["Location"].endswith("/admin/login")


async def test_login_refuses_when_the_hash_changed_during_verify(
        client, monkeypatch):
    import threading
    real = webui.verify_password
    store = client.settings_store

    def swap_then_verify(password, stored):
        ok = real(password, stored)
        store.settings.admin.password_hash = webui.hash_password("other-pw-123")
        store.settings.admin.password_version += 1
        return ok

    monkeypatch.setattr(webui, "verify_password", swap_then_verify)
    resp = await _post_login(client, "admin", "right-password")
    assert resp.status == 401
    assert webui.SESSION_COOKIE not in resp.cookies
    # not the client's failure: nothing counted
    assert webui._throttle._failures == {}
    assert webui._ip_throttle._failures == {}


async def test_per_username_throttle_key_is_shared_across_one_64(
        proxied_client):
    for addr in ("2001:db8:1:2::1", "2001:db8:1:2::2"):
        resp = await _post_login_from(proxied_client, addr, "admin", "wrong")
        assert resp.status == 401
    assert len(webui._throttle._failures) == 1
    (bucket,) = webui._throttle._failures.values()
    assert len(bucket) == 2


async def test_success_clears_the_64s_failures_from_another_address(
        proxied_client):
    resp = await _post_login_from(
        proxied_client, "2001:db8:1:2::1", "admin", "wrong")
    assert resp.status == 401
    resp = await _post_login_from(
        proxied_client, "2001:db8:1:2::2", "admin", "right-password")
    assert resp.status == 302
    assert webui._throttle._failures == {}
    assert webui._ip_throttle._failures == {}


async def test_login_cookie_carries_the_version_read_with_the_verified_hash(
        client, monkeypatch):
    real = webui.verify_password
    store = client.settings_store

    def bump_version_during_verify(password, stored):
        ok = real(password, stored)
        store.settings.admin.password_version += 1
        return ok

    monkeypatch.setattr(webui, "verify_password", bump_version_during_verify)
    resp = await _post_login(client, "admin", "right-password")
    assert resp.status == 302
    cookie = resp.cookies[webui.SESSION_COOKIE].value
    probe = await client.get(
        "/admin", cookies={webui.SESSION_COOKIE: cookie}, allow_redirects=False)
    assert probe.status == 302  # stamped with the old version, now stale
