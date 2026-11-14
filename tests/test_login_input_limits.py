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
