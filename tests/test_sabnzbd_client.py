"""Wire tests for the SABnzbd client: ping, priority set, and the
200-with-status-false failure mode."""
from __future__ import annotations

import httpx
import pytest

from http_util import PermanentAPIError
from sabnzbd import SabnzbdClient


def _client(handler) -> SabnzbdClient:
    c = SabnzbdClient("http://sab.test:8080", "key123")
    c._client = httpx.AsyncClient(base_url="http://sab.test:8080",
                                  transport=httpx.MockTransport(handler))
    return c


async def test_ping_returns_version():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["mode"] == "version"
        assert request.url.params["apikey"] == "key123"
        return httpx.Response(200, json={"version": "4.3.2"})

    c = _client(handler)
    assert await c.ping() == "4.3.2"
    await c.close()


async def test_set_priority_sends_expected_params():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.url.params)
        return httpx.Response(200, json={"status": True, "position": 0})

    c = _client(handler)
    assert await c.set_priority("SABnzbd_nzo_abc", "force") is True
    assert captured["mode"] == "queue"
    assert captured["name"] == "priority"
    assert captured["value"] == "SABnzbd_nzo_abc"
    assert captured["value2"] == "2"  # force
    await c.close()


async def test_set_priority_unknown_level_noops():
    async def boom(*a, **k):
        raise AssertionError("no request expected")
    c = SabnzbdClient("http://sab.test:8080", "k")
    c._call = boom
    assert await c.set_priority("nzo", "off") is False
    assert await c.set_priority("", "high") is False
    await c.close()


async def test_api_level_failure_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": False, "error": "API Key Incorrect"})

    c = _client(handler)
    with pytest.raises(PermanentAPIError) as exc_info:
        await c.ping()
    assert "API Key Incorrect" in str(exc_info.value)
    await c.close()
