"""Regression for audit CONC #11: SeerrClient._as_user reuses cached
authenticated clients per Plex token. LRU + TTL bounded."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

import seerr
from seerr import SeerrClient


@pytest.fixture
def client(monkeypatch):
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
    """After TTL expires, _as_user closes the stale client and mints a fresh
    one (different instance)."""
    a = await client._as_user("token-X")
    # Advance the monotonic clock past TTL.
    real_monotonic = time.monotonic
    fake_now = [real_monotonic() + seerr._USER_CLIENT_TTL_S + 1]
    monkeypatch.setattr(seerr.time, "monotonic", lambda: fake_now[0])
    b = await client._as_user("token-X")
    assert a is not b
    # The original client should have been aclose()'d during eviction.
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
    # t1 should have been evicted (LRU) and its client closed.
    assert "t1" not in client._user_clients
    assert a.is_closed
    assert {"t2", "t3", "t4"}.issubset(client._user_clients.keys())


async def test_touching_an_entry_promotes_it_in_lru(client: SeerrClient, monkeypatch):
    """An entry hit refreshes its LRU position; the next eviction shouldn't
    target it."""
    monkeypatch.setattr(seerr, "_USER_CLIENT_MAX", 3)
    a = await client._as_user("t1")
    b = await client._as_user("t2")
    c = await client._as_user("t3")
    # Touch t1 -> moves it to the most-recent end.
    await client._as_user("t1")
    # Exceed cap; t2 (now LRU) gets evicted, not t1.
    await client._as_user("t4")
    assert "t2" not in client._user_clients
    assert "t1" in client._user_clients
    assert b.is_closed
    assert not a.is_closed


# --- close() drains cache ---


async def test_close_drains_cache(client: SeerrClient):
    a = await client._as_user("token-Y")
    b = await client._as_user("token-Z")
    await client.close()
    assert client._user_clients == {}
    assert a.is_closed
    assert b.is_closed
