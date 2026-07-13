"""Wire-level tests for the audit / P2-3 fixes: 2xx responses with
garbage bodies must raise a clean AmbiguousResponseError (never look like a
retryable failure after a write landed), and list_issues must report Seerr's
full matching count so /tickets can be honest about truncation."""
from __future__ import annotations

import httpx
import pytest

import seerr as seerr_mod
from seerr import AmbiguousResponseError, SeerrClient


def _client(monkeypatch, handler) -> SeerrClient:
    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(seerr_mod.httpx, "AsyncClient", patched)
    return SeerrClient("http://seerr.test", "key")


async def test_create_issue_html_2xx_raises_ambiguous(monkeypatch):
    """A proxy's HTML 200 after the POST: the issue may exist in Seerr, so
    the error must be the non-retryable ambiguous kind, not a generic
    failure that invites a duplicate retry."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway ok</html>")

    client = _client(monkeypatch, handler)
    with pytest.raises(AmbiguousResponseError) as exc_info:
        await client.create_issue(issue_type=4, message="m", seerr_media_id=5,
                                  media_type="movie")
    assert "unreadable" in str(exc_info.value)
    await client.close()


async def test_create_issue_missing_id_raises_ambiguous(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})  # no id

    client = _client(monkeypatch, handler)
    with pytest.raises(AmbiguousResponseError) as exc_info:
        await client.create_issue(issue_type=4, message="m", seerr_media_id=5,
                                  media_type="movie")
    assert "duplicate" in str(exc_info.value)
    await client.close()


async def test_create_issue_valid_response_still_works(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 99})

    client = _client(monkeypatch, handler)
    created = await client.create_issue(issue_type=4, message="m",
                                        seerr_media_id=5, media_type="movie")
    assert created.id == 99
    assert created.url.endswith("/issues/99")
    await client.close()


async def test_list_issues_returns_total_from_page_info(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "pageInfo": {"results": 40},
            "results": [{"id": 1, "media": {}, "createdBy": {}}],
        })

    client = _client(monkeypatch, handler)
    items, total = await client.list_issues(take=25)
    assert [i.id for i in items] == [1]
    assert total == 40
    await client.close()


async def test_list_issues_total_falls_back_to_len(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "results": [{"id": 1, "media": {}, "createdBy": {}}],
        })

    client = _client(monkeypatch, handler)
    items, total = await client.list_issues(take=25)
    assert total == len(items) == 1
    await client.close()


async def test_ping_garbage_2xx_raises_clean_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    client = _client(monkeypatch, handler)
    with pytest.raises(AmbiguousResponseError):
        await client.ping()
    await client.close()
