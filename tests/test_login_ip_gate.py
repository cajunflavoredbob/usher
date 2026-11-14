"""Per-IP login gate with the real thresholds: clients that share a peer IP
cannot lock the Admin out, and a username-rotating flood still gets 429
before any password hashing."""
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


async def _login(client, username, password):
    csrf = webui.generate_csrf_token(SECRET)
    r = await client.post(
        "/admin/login",
        data={"username": username, "password": password, "csrf_token": csrf},
        cookies={"usher_csrf": csrf}, allow_redirects=False)
    return r.status


async def _flood(client, n):
    return [await _login(client, f"user{i}", "wrong") for i in range(n)]


async def test_admin_login_survives_other_clients_failures(client):
    statuses = await _flood(client, THROTTLE_IP_MAX_FAILURES)
    assert statuses == [401] * THROTTLE_IP_MAX_FAILURES
    assert await _login(client, "admin", "right-password") == 302


async def test_rotating_username_flood_still_gets_429_without_hashing(
        client, monkeypatch):
    await _flood(client, THROTTLE_IP_MAX_FAILURES)
    calls = []
    real = webui.verify_password
    monkeypatch.setattr(
        webui, "verify_password",
        lambda *a, **k: calls.append(1) or real(*a, **k))
    assert await _login(client, "another-user", "wrong") == 429
    assert await _login(client, "yet-another", "wrong") == 429
    assert calls == []


async def test_admins_own_failures_still_lock_that_key(client):
    for _ in range(THROTTLE_MAX_FAILURES):
        assert await _login(client, "admin", "wrong") == 401
    assert await _login(client, "admin", "right-password") == 429


async def test_admin_username_still_hashes_when_aggregate_locked(
        client, monkeypatch):
    await _flood(client, THROTTLE_IP_MAX_FAILURES)
    calls = []
    real = webui.verify_password
    monkeypatch.setattr(
        webui, "verify_password",
        lambda *a, **k: calls.append(1) or real(*a, **k))
    assert await _login(client, "admin", "wrong") == 401
    assert calls == [1]


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
