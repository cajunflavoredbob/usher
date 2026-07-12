"""Wire-level tests for SeerrClient._as_user token-failure classification.

Seerr reports a revoked/expired Plex token on /auth/plex as a 500 "Unable
to authenticate." -- which classify_response reads as transient, producing
a false "Seerr isn't responding; try again in a minute" for an error no
retry can fix. _as_user must convert that (and 401/403) into
PlexTokenInvalidError so the bot can offer the guided re-link instead,
while leaving genuine 5xx outages classified as transient.
"""
import httpx
import pytest

import seerr as seerr_mod
from http_util import TransientAPIError
from seerr import PlexTokenInvalidError, SeerrClient


def _make_client(monkeypatch, auth_status: int, auth_body: dict) -> SeerrClient:
    """SeerrClient whose HTTP layer is a mock transport; /auth/plex answers
    with the given status/body, everything else 200s."""
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/plex"):
            return httpx.Response(auth_status, json=auth_body)
        return httpx.Response(200, json={})

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    # _as_user mints its own AsyncClient internally, so the transport has to
    # be injected at the httpx level rather than through the constructor.
    monkeypatch.setattr(seerr_mod.httpx, "AsyncClient", patched)
    return SeerrClient("http://seerr.test", "key")


async def test_unable_to_authenticate_500_raises_token_invalid(monkeypatch):
    client = _make_client(monkeypatch, 500, {"message": "Unable to authenticate."})
    with pytest.raises(PlexTokenInvalidError):
        await client._as_user("dead-token")
    await client.close()


async def test_auth_403_raises_token_invalid(monkeypatch):
    client = _make_client(monkeypatch, 403, {"message": "Access denied."})
    with pytest.raises(PlexTokenInvalidError):
        await client._as_user("dead-token")
    await client.close()


async def test_other_500_stays_transient(monkeypatch):
    """A genuine Seerr outage must keep the transient classification."""
    client = _make_client(monkeypatch, 500, {"message": "Database is locked"})
    with pytest.raises(TransientAPIError) as exc_info:
        await client._as_user("token")
    assert not isinstance(exc_info.value, PlexTokenInvalidError)
    await client.close()


async def test_failed_auth_caches_nothing(monkeypatch):
    """A rejected token must not leave a client in the LRU cache."""
    client = _make_client(monkeypatch, 500, {"message": "Unable to authenticate."})
    with pytest.raises(PlexTokenInvalidError):
        await client._as_user("dead-token")
    assert len(client._user_clients) == 0
    await client.close()
