"""The change-password throttle holds against parallel guesses: the lockout
check, the verify and the failure record run under one lock, so a burst
cannot all pass the check before the first failure is recorded."""
from __future__ import annotations

import asyncio
import secrets
import time
from hashlib import pbkdf2_hmac

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import webui

SECRET = b"0123456789abcdef0123456789abcdef"
_SESSION = webui._make_session_cookie(SECRET, "admin")
CSRF = webui.generate_csrf_token(SECRET, _SESSION.rsplit(".", 1)[-1])
REAL_PW = "right-password-1"


def _fast_hash(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = pbkdf2_hmac("sha256", pw.encode(), salt, 1000)
    return f"pbkdf2_sha256$1000${salt.hex()}${h.hex()}"


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.delenv("USHER_WEBHOOK_SECRET", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    settings_path = data_dir / "settings.json"
    store = webui.SettingsStore(settings_path)
    store.settings.admin.username = "admin"
    store.settings.admin.password_hash = _fast_hash(REAL_PW)
    store.save()
    app = web.Application()
    webui.attach_webui(
        app, settings_store=store, session_secret=SECRET,
        data_dir=data_dir, settings_path=settings_path,
        db_path=data_dir / "mappings.sqlite",
        enc_key_path=data_dir / "encryption.key",
    )
    # A slow verify makes the parallel requests overlap, as a real 600k
    # iteration PBKDF2 does.
    real = webui.verify_password

    def slow(pw, stored):
        time.sleep(0.03)
        return real(pw, stored)

    monkeypatch.setattr(webui, "verify_password", slow)
    async with TestClient(TestServer(app)) as c:
        yield c, store


def _form(current: str) -> dict:
    return {"csrf_token": CSRF, "current": current,
            "new": "brand-new-password-1", "confirm": "brand-new-password-1"}


def _post(client, current: str):
    return client.post("/admin/password", data=_form(current),
                       cookies={webui.SESSION_COOKIE: _SESSION,
                                "usher_csrf": CSRF})


async def test_twenty_parallel_wrong_guesses_get_five_400s_then_429(env):
    client, _ = env
    resps = await asyncio.gather(*[_post(client, f"wrong-{i}") for i in range(20)])
    statuses = sorted(r.status for r in resps)
    assert statuses == [400] * 5 + [429] * 15


async def test_a_right_guess_in_a_burst_after_the_limit_gets_429(env):
    client, store = env
    old_hash = store.settings.admin.password_hash
    tasks = [asyncio.create_task(_post(client, f"wrong-{i}")) for i in range(5)]
    await asyncio.sleep(0.01)
    right = asyncio.create_task(_post(client, REAL_PW))
    resps = await asyncio.gather(*tasks)
    assert [r.status for r in resps] == [400] * 5
    resp = await right
    assert resp.status == 429
    assert store.settings.admin.password_hash == old_hash
    assert store.settings.admin.password_version == 0


async def test_a_right_guess_alone_still_changes_the_password(env):
    client, store = env
    resp = await _post(client, REAL_PW)
    assert resp.status == 200
    assert store.settings.admin.password_version == 1
