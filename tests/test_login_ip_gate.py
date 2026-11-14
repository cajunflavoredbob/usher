"""Per-IP login gate with the real thresholds, and the known-device cookie:
a browser that signed in before keeps working during a flood from clients
sharing its peer IP; everyone else gets 429 before any hashing."""
from __future__ import annotations

import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import auth_util
import webui
from auth_util import (
    LoginThrottle,
    THROTTLE_IP_MAX_FAILURES,
    THROTTLE_MAX_FAILURES,
)

SECRET = b"0123456789abcdef0123456789abcdef"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.delenv("USHER_WEBHOOK_SECRET", raising=False)
    # Fresh throttles at the real thresholds (no raised limits).
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
        yield c


async def _login_resp(client, username, password, device=None):
    csrf = webui.generate_csrf_token(SECRET)
    cookies = {"usher_csrf": csrf}
    if device is not None:
        cookies["usher_device"] = device
    return await client.post(
        "/admin/login",
        data={"username": username, "password": password, "csrf_token": csrf},
        cookies=cookies, allow_redirects=False)


async def _login(client, username, password, device=None):
    return (await _login_resp(client, username, password, device)).status


async def _flood(client, n):
    return [await _login(client, f"user{i}", "wrong") for i in range(n)]


def _device_cookie(client, pwd_ver=0, username="admin", device_id="dev-1"):
    return webui._make_device_cookie(SECRET, username, pwd_ver, device_id)


def _hash_counter(monkeypatch):
    calls = []
    real = webui.verify_password
    monkeypatch.setattr(
        webui, "verify_password",
        lambda *a, **k: calls.append(1) or real(*a, **k))
    return calls


async def test_successful_login_sets_device_cookie_with_flags(client):
    r = await _login_resp(client, "admin", "right-password")
    assert r.status == 302
    morsel = r.cookies["usher_device"]
    assert morsel["httponly"]
    assert morsel["samesite"] == "Strict"
    assert int(morsel["max-age"]) == 180 * 24 * 3600
    assert webui._verify_device_cookie(
        SECRET, morsel.value, "admin", 0) is not None


async def test_device_cookie_secure_flag_follows_request(client, monkeypatch):
    csrf = webui.generate_csrf_token(SECRET)
    r = await client.post(
        "/admin/login",
        data={"username": "admin", "password": "right-password",
              "csrf_token": csrf},
        cookies={"usher_csrf": csrf},
        headers={"X-Forwarded-Proto": "https"}, allow_redirects=False)
    assert r.status == 302
    # X-Forwarded-Proto is ignored without TRUSTED_PROXIES, so not Secure.
    assert not r.cookies["usher_device"]["secure"]
    monkeypatch.setattr(webui, "request_is_secure", lambda req: True)
    client.session.cookie_jar.clear()
    r = await _login_resp(client, "admin", "right-password")
    assert r.cookies["usher_device"]["secure"]


async def test_flood_blocks_admin_without_device_cookie(client):
    statuses = await _flood(client, THROTTLE_IP_MAX_FAILURES)
    assert statuses == [401] * THROTTLE_IP_MAX_FAILURES
    assert await _login(client, "admin", "right-password") == 429


async def test_flood_does_not_block_admin_with_device_cookie(client):
    await _flood(client, 20)
    assert await _login(
        client, "admin", "right-password", _device_cookie(client)) == 302


async def test_no_username_oracle_while_locked(client, monkeypatch):
    await _flood(client, THROTTLE_IP_MAX_FAILURES)
    calls = _hash_counter(monkeypatch)
    assert await _login(client, "admin", "wrong") == 429
    assert await _login(client, "Admin", "wrong") == 429
    assert await _login(client, "another-user", "wrong") == 429
    assert calls == []


async def test_rotating_username_flood_gets_429_without_hashing(
        client, monkeypatch):
    await _flood(client, THROTTLE_IP_MAX_FAILURES)
    calls = _hash_counter(monkeypatch)
    assert await _login(client, "another-user", "wrong") == 429
    assert await _login(client, "yet-another", "wrong") == 429
    assert calls == []


async def test_forged_cookie_counts_as_absent(client):
    await _flood(client, 20)
    good = _device_cookie(client)
    body, sig = good.split(".")
    forged = body + "." + sig[::-1]
    assert await _login(client, "admin", "right-password", forged) == 429
    assert await _login(client, "admin", "right-password", "junk") == 429


async def test_cookie_for_other_secret_counts_as_absent(client):
    await _flood(client, 20)
    other = webui._make_device_cookie(b"x" * 32, "admin", 0, "dev-1")
    assert await _login(client, "admin", "right-password", other) == 429


async def test_stale_password_version_cookie_counts_as_absent(client):
    await _flood(client, 20)
    stale = _device_cookie(client, pwd_ver=3)
    assert await _login(client, "admin", "right-password", stale) == 429


async def test_cookie_for_other_username_counts_as_absent(client):
    await _flood(client, 20)
    other = _device_cookie(client, username="someone-else")
    assert await _login(client, "admin", "right-password", other) == 429


async def test_expired_cookie_counts_as_absent(client, monkeypatch):
    await _flood(client, 20)
    cookie = _device_cookie(client)
    real_time = webui.time.time
    monkeypatch.setattr(webui.time, "time",
                        lambda: real_time() + 181 * 24 * 3600)
    assert await _login(client, "admin", "right-password", cookie) == 429


async def test_failures_with_cookie_lock_that_device_only(client, monkeypatch):
    cookie = _device_cookie(client, device_id="dev-1")
    other = _device_cookie(client, device_id="dev-2")
    for _ in range(THROTTLE_MAX_FAILURES):
        assert await _login(client, "admin", "wrong", cookie) == 401
    calls = _hash_counter(monkeypatch)
    assert await _login(client, "admin", "right-password", cookie) == 429
    assert calls == []
    assert await _login(client, "admin", "right-password", other) == 302
    # The address-keyed limits were not charged by the device failures.
    client.session.cookie_jar.clear()
    assert await _login(client, "admin", "right-password") == 302


async def test_device_cookie_survives_logout(client):
    session = webui._make_session_cookie(SECRET, "admin", 0)
    csrf = webui.generate_csrf_token(SECRET, session.rsplit(".", 1)[-1])
    device = _device_cookie(client)
    r = await client.post(
        "/admin/logout", data={"csrf_token": csrf},
        cookies={"usher_session": session, "usher_csrf": csrf,
                 "usher_device": device}, allow_redirects=False)
    assert r.status == 302
    assert "usher_device" not in r.cookies
    assert any(m.key == "usher_session" for m in r.cookies.values())
    # The browser still has the device cookie: a flood login still gets in.
    client.session.cookie_jar.clear()
    await _flood(client, 20)
    assert await _login(client, "admin", "right-password", device) == 302


async def test_device_success_does_not_reset_ip_gate(client):
    await _flood(client, THROTTLE_IP_MAX_FAILURES - 1)
    assert await _login(
        client, "admin", "right-password", _device_cookie(client)) == 302
    client.session.cookie_jar.clear()
    # One more failure must still reach the limit: the success above did
    # not clear the per-IP count.
    assert await _login(client, "user-x", "wrong") == 401
    assert await _login(client, "admin", "right-password") == 429


async def test_device_failures_do_not_charge_ip_gate(client):
    for n in range(THROTTLE_IP_MAX_FAILURES // THROTTLE_MAX_FAILURES + 1):
        cookie = _device_cookie(client, device_id=f"dev-{n}")
        for _ in range(THROTTLE_MAX_FAILURES):
            assert await _login(client, "admin", "wrong", cookie) == 401
    client.session.cookie_jar.clear()
    assert await _login(client, "admin", "right-password") == 302


async def test_admins_own_failures_without_cookie_lock_that_key(client):
    for _ in range(THROTTLE_MAX_FAILURES):
        assert await _login(client, "admin", "wrong") == 401
    assert await _login(client, "admin", "right-password") == 429


def test_untrusted_peer_with_forwarded_for_warns_once(monkeypatch, caplog):
    from types import SimpleNamespace
    monkeypatch.setattr(auth_util, "_warned_untrusted_forwarded", False)
    req = SimpleNamespace(
        remote="192.0.2.10", headers={"X-Forwarded-For": "198.51.100.5"},
        app={})
    with caplog.at_level(logging.WARNING, logger=auth_util.logger.name):
        assert auth_util.client_ip(req) == "192.0.2.10"
        assert auth_util.client_ip(req) == "192.0.2.10"
    hits = [r for r in caplog.records if "TRUSTED_PROXIES" in r.getMessage()]
    assert len(hits) == 1


def test_no_warning_without_forwarded_for(monkeypatch, caplog):
    from types import SimpleNamespace
    monkeypatch.setattr(auth_util, "_warned_untrusted_forwarded", False)
    req = SimpleNamespace(remote="192.0.2.10", headers={}, app={})
    with caplog.at_level(logging.WARNING, logger=auth_util.logger.name):
        auth_util.client_ip(req)
    assert not [r for r in caplog.records if "TRUSTED_PROXIES" in r.getMessage()]
