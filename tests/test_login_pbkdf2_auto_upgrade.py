"""Regression for a successful login with a stale-iter-count
PBKDF2 hash gets the hash rehashed with the current iteration count, and
the upgrade is audit-logged. Login itself still succeeds."""
from __future__ import annotations

import logging
import secrets
from hashlib import pbkdf2_hmac
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import webui
from settings import (
    PBKDF2_ITERATIONS,
    AdminAccount,
    SettingsStore,
    hash_password,
    verify_password,
)


def _make_stale_hash(plaintext: str, *, iters: int) -> str:
    """Produce a pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex> using a chosen
    iteration count (typically lower than the current PBKDF2_ITERATIONS)."""
    salt = secrets.token_bytes(16)
    h = pbkdf2_hmac("sha256", plaintext.encode(), salt, iters)
    return f"pbkdf2_sha256${iters}${salt.hex()}${h.hex()}"


@pytest.fixture
def webui_client(tmp_path: Path, monkeypatch):
    """An aiohttp TestClient running the webui against a tmp SettingsStore
    with a known admin password. Disables the throttle so repeated runs
    in a session don't lock out the test."""
    monkeypatch.delenv("HERMES_WEBHOOK_SECRET", raising=False)
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)
    # Skip the throttle. Module-level singleton so we reset its state.
    webui._throttle._failures.clear()
    return store


async def _login(store: SettingsStore, password: str = "hunter2-test"):
    """Post a login + return the (response, store) pair. Builds a fresh
    aiohttp app each call so the CSRF cookie comes through."""
    app = web.Application()
    webui.attach_webui(
        app,
        settings_store=store,
        session_secret=b"\x00" * 32,
        data_dir=Path("/tmp/x-test"),  # unused for login path
        settings_path=Path(store.path),
        db_path=Path("/tmp/x-test.sqlite"),
    )
    async with TestClient(TestServer(app)) as client:
        # GET the login page first to receive the CSRF cookie.
        get = await client.get("/admin/login")
        assert get.status == 200
        # aiohttp's TestClient cookie_jar drops cookies from non-yarl URLs.
        # Parse Set-Cookie directly off the response headers instead.
        from http.cookies import SimpleCookie
        sc: SimpleCookie = SimpleCookie()
        for set_cookie in get.headers.getall("Set-Cookie", []):
            sc.load(set_cookie)
        csrf = sc["hermes_csrf"].value
        post = await client.post(
            "/admin/login",
            data={"username": store.settings.admin.username,
                  "password": password,
                  "csrf_token": csrf},
            cookies={"hermes_csrf": csrf},
            allow_redirects=False,
        )
        return post


async def test_current_iters_hash_is_not_rehashed(webui_client, caplog):
    """A hash already at PBKDF2_ITERATIONS shouldn't be touched."""
    store = webui_client
    store.settings.admin = AdminAccount(
        username="admin",
        password_hash=hash_password("hunter2-test"),  # uses current iters
    )
    store.save()
    original_hash = store.settings.admin.password_hash

    caplog.set_level(logging.WARNING, logger="hermes.audit")
    resp = await _login(store)
    assert resp.status == 302  # redirect to /admin
    # Reload from disk to confirm no rewrite.
    fresh = SettingsStore(store.path)
    assert fresh.settings.admin.password_hash == original_hash
    # No password_rehashed audit entry.
    audit_msgs = [r.getMessage() for r in caplog.records if r.name == "hermes.audit"]
    assert not any("password_rehashed" in m for m in audit_msgs)


async def test_stale_iters_hash_is_rehashed(webui_client, caplog):
    """A hash with a lower iter count gets rehashed + audit-logged."""
    store = webui_client
    stale_iters = max(1, PBKDF2_ITERATIONS // 2)
    store.settings.admin = AdminAccount(
        username="admin",
        password_hash=_make_stale_hash("hunter2-test", iters=stale_iters),
    )
    store.save()
    original_hash = store.settings.admin.password_hash

    caplog.set_level(logging.WARNING, logger="hermes.audit")
    resp = await _login(store)
    assert resp.status == 302

    # Reload from disk; hash must now use the current iter count.
    fresh = SettingsStore(store.path)
    new_hash = fresh.settings.admin.password_hash
    assert new_hash != original_hash
    assert new_hash.split("$")[1] == str(PBKDF2_ITERATIONS)
    # New hash still verifies the same password.
    assert verify_password("hunter2-test", new_hash)
    # Audit log captures the upgrade.
    audit_msgs = [r.getMessage() for r in caplog.records if r.name == "hermes.audit"]
    assert any("password_rehashed" in m and f"from_iters={stale_iters}" in m
               and f"to_iters={PBKDF2_ITERATIONS}" in m
               for m in audit_msgs), audit_msgs


async def test_malformed_hash_skips_rehash_without_crashing(webui_client):
    """A malformed stored hash (e.g., from a hand-edited settings.json)
    fails verify_password -> login fails with 401 (not 500). The auto-upgrade
    block never runs because the verify branch wasn't taken."""
    store = webui_client
    store.settings.admin = AdminAccount(
        username="admin",
        password_hash="not-a-real-hash-format",
    )
    store.save()

    resp = await _login(store)
    # Login fails (invalid credentials). The crucial bit: we don't 500.
    assert resp.status == 401
