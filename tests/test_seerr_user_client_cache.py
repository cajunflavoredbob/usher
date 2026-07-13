"""Regression for SeerrClient._as_user reuses cached
authenticated clients per Plex token. LRU + TTL bounded."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

import seerr
from seerr import SeerrClient


@pytest.fixture
async def client(monkeypatch):
    """SeerrClient with _as_user's network call mocked so we can assert on
    cache hits without actually talking to Seerr."""
    c = SeerrClient("http://seerr.example.com:5056", "api-key", timeout=10.0)
    # Patch execute() to no-op for the auth POST.
    async def fake_execute(*args, **kwargs):
        # Return something innocuous; _as_user only checks for an exception.
        from types import SimpleNamespace
        return SimpleNamespace(json=lambda: {})
    monkeypatch.setattr(seerr, "execute", fake_execute)
    yield c
    # Drain retired clients + cancel deferred-close tasks so nothing
    # outlives the test's event loop.
    await c.close()


# --- cache hit ---


async def test_second_call_returns_same_client(client: SeerrClient):
    a = await client._as_user("plex-token-1")
    b = await client._as_user("plex-token-1")
    assert a is b


async def test_different_tokens_get_different_clients(client: SeerrClient):
    a = await client._as_user("token-A")
    b = await client._as_user("token-B")
    assert a is not b
    # And both are cached.
    assert "token-A" in client._user_clients
    assert "token-B" in client._user_clients


# --- TTL eviction ---


async def test_expired_entry_is_replaced(client: SeerrClient, monkeypatch):
    """After TTL expires, _as_user retires the stale client (deferred close,
    an immediate aclose could kill a request another coroutine
    is mid-flight on) and mints a fresh one."""
    a = await client._as_user("token-X")
    # Advance the monotonic clock past TTL.
    real_monotonic = time.monotonic
    fake_now = [real_monotonic() + seerr._USER_CLIENT_TTL_S + 1]
    monkeypatch.setattr(seerr.time, "monotonic", lambda: fake_now[0])
    b = await client._as_user("token-X")
    assert a is not b
    # NOT closed yet -- retired for grace-period close, drained by close().
    assert not a.is_closed
    assert a in client._retired_user_clients
    await client.close()
    assert a.is_closed


# --- LRU eviction ---


async def test_lru_evicts_oldest_when_over_cap(client: SeerrClient, monkeypatch):
    """Filling past _USER_CLIENT_MAX evicts the least-recently-used entry
    and closes its client."""
    # Lower the cap for the test so we don't have to mint 32 tokens.
    monkeypatch.setattr(seerr, "_USER_CLIENT_MAX", 3)

    a = await client._as_user("t1")
    await client._as_user("t2")
    await client._as_user("t3")
    # All three are present.
    assert {"t1", "t2", "t3"}.issubset(client._user_clients.keys())
    # Now exceed the cap.
    await client._as_user("t4")
    # t1 should have been evicted (LRU) and retired for deferred close.
    assert "t1" not in client._user_clients
    assert not a.is_closed
    assert a in client._retired_user_clients
    assert {"t2", "t3", "t4"}.issubset(client._user_clients.keys())
    await client.close()
    assert a.is_closed


async def test_touching_an_entry_promotes_it_in_lru(client: SeerrClient, monkeypatch):
    """An entry hit refreshes its LRU position; the next eviction shouldn't
    target it."""
    monkeypatch.setattr(seerr, "_USER_CLIENT_MAX", 3)
    a = await client._as_user("t1")
    b = await client._as_user("t2")
    c = await client._as_user("t3")
    # Touch t1 -> moves it to the most-recent end.
    await client._as_user("t1")
    # Exceed cap; t2 (now LRU) gets evicted (retired), not t1.
    await client._as_user("t4")
    assert "t2" not in client._user_clients
    assert "t1" in client._user_clients
    assert b in client._retired_user_clients
    assert a not in client._retired_user_clients


# --- concurrent-miss lock ---


async def test_concurrent_misses_share_one_client(monkeypatch):
    """Two coroutines missing the cache for the same token must not both
    POST /auth/plex; the loser previously orphaned its client (FD leak)."""
    import asyncio

    auth_calls = []

    async def fake_execute(client, method, path, **kwargs):
        from types import SimpleNamespace
        auth_calls.append(path)
        await asyncio.sleep(0)  # yield so the second coroutine can race
        return SimpleNamespace(json=lambda: {})

    monkeypatch.setattr(seerr, "execute", fake_execute)
    c = SeerrClient("http://seerr.example.com:5056", "api-key")
    a, b = await asyncio.gather(c._as_user("tok"), c._as_user("tok"))
    assert a is b
    assert auth_calls == ["/auth/plex"]
    await c.close()


# --- close() drains cache ---


async def test_close_drains_cache(client: SeerrClient):
    a = await client._as_user("token-Y")
    b = await client._as_user("token-Z")
    await client.close()
    assert client._user_clients == {}
    assert a.is_closed
    assert b.is_closed
