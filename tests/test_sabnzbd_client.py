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


async def test_get_items_status_batches_queue_and_history():
    """Both lookups are FILTERED batch calls (queue then history for the
    remainder), never per-id fetches."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        if request.url.params["mode"] == "queue":
            return httpx.Response(200, json={"queue": {"slots": [
                {"nzo_id": "a", "percentage": "37", "timeleft": "0:03:12"}]}})
        return httpx.Response(200, json={"history": {"slots": [
            {"nzo_id": "b", "status": "Extracting"},
            {"nzo_id": "c", "status": "Completed"},
            {"nzo_id": "f", "status": "Failed"}]}})

    c = _client(handler)
    out = await c.get_items_status(["a", "b", "c", "f", "ghost"])
    assert out == {
        "a": ("downloading", 37, "0:03:12"),
        "b": ("postproc", 100, "Extracting"),
        "c": ("completed", 100, ""),
        "f": ("failed", 100, ""),
    }
    assert len(calls) == 2
    assert calls[0]["mode"] == "queue" and calls[0]["nzo_ids"] == "a,b,c,f,ghost"
    assert calls[1]["mode"] == "history" and "a" not in calls[1]["nzo_ids"].split(",")
    await c.close()


async def test_get_items_status_all_in_queue_skips_history():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["mode"])
        return httpx.Response(200, json={"queue": {"slots": [
            {"nzo_id": "a", "percentage": "50", "timeleft": ""}]}})

    c = _client(handler)
    out = await c.get_items_status(["a"])
    assert out["a"][0] == "downloading"
    assert calls == ["queue"]
    await c.close()


async def test_get_items_status_empty_and_unknown():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"queue": {"slots": []},
                                         "history": {"slots": []}})

    c = _client(handler)
    assert await c.get_items_status([]) == {}
    assert await c.get_items_status(["nope"]) == {}
    await c.close()
