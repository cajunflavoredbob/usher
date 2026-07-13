"""Wire-level tests for SonarrClient.get_series_by_tvdb's identity guard:
the lookup feeds a delete workflow and Sonarr silently ignores unknown query
params, so the client must scan for the requested tvdbId rather than trust
items[0] of a possibly-unfiltered response."""
import httpx

from sonarr import SonarrClient


def _client(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = SonarrClient("http://sonarr.test", "key")
    client._client = httpx.AsyncClient(
        base_url="http://sonarr.test/api/v3",
        transport=httpx.MockTransport(handler),
    )
    return client


async def test_lookup_scans_for_requested_tvdb_id():
    client = _client([
        {"id": 3, "tvdbId": 999, "title": "Wrong Show"},
        {"id": 8, "tvdbId": 42, "title": "Right Show"},
    ])
    series = await client.get_series_by_tvdb(42)
    await client.close()
    assert series is not None and series.id == 8


async def test_lookup_returns_none_when_only_wrong_series_returned():
    client = _client([{"id": 3, "tvdbId": 999, "title": "Wrong Show"}])
    assert await client.get_series_by_tvdb(42) is None
    await client.close()


async def test_mark_failed_pages_history_until_grab_found():
    """Audit P2-5: a churn-heavy episode can push the grabbed event past the
    newest history page; the blocklist step must page deeper, not silently
    fall back to 'no prior grab' and re-grab the same release."""
    from sonarr import _HISTORY_PAGE_SIZE

    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 8, "tvdbId": 42, "title": "Show"}])
        if path == "/api/v3/episode":
            return httpx.Response(200, json=[
                {"id": 501, "seasonNumber": 1, "episodeNumber": 5,
                 "title": "Ep", "hasFile": True, "episodeFileId": 9},
            ])
        if path == "/api/v3/history":
            page = int(request.url.params["page"])
            if page == 1:
                # A full page of non-grab churn pushes the grab off page 1.
                return httpx.Response(200, json={"records": [
                    {"id": 1000 + i, "eventType": "downloadFolderImported"}
                    for i in range(_HISTORY_PAGE_SIZE)
                ]})
            return httpx.Response(200, json={"records": [
                {"id": 777, "eventType": "grabbed"},
            ]})
        if path.startswith("/api/v3/history/failed/") and request.method == "POST":
            return httpx.Response(200, json={})
        if path == "/api/v3/episodefile/9" and request.method == "DELETE":
            return httpx.Response(200, json={})
        if path == "/api/v3/command" and request.method == "POST":
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"message": f"unexpected {request.method} {path}"})

    client = SonarrClient("http://sonarr.test", "key")
    client._client = httpx.AsyncClient(
        base_url="http://sonarr.test/api/v3",
        transport=httpx.MockTransport(handler),
    )
    result = await client.mark_failed_episode(42, 1, 5)
    await client.close()

    assert result.status == "ok"
    assert result.steps_done == ["blocklist", "delete", "search"]
    history_pages = [r.url.params["page"] for r in requests
                     if r.url.path == "/api/v3/history"]
    assert history_pages == ["1", "2"]
    assert any(r.url.path == "/api/v3/history/failed/777" for r in requests)
