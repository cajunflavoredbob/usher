"""Wire-level tests for RadarrClient.mark_failed.

Regression tests for the v0.11.24 Mark Failed bug: the client filtered
history with `movieId=` on the paginated /history endpoint, a param Radarr
silently ignores (the real filter is `movieIds`), so it saw the newest 20
events for the WHOLE library and could blocklist another movie's grab.
Client-level mocks can't catch a wrong URL/param, so these tests drive the
real RadarrClient over httpx.MockTransport and assert the requests it makes.
"""
import httpx

from radarr import RadarrClient

MOVIE = {"id": 5, "title": "The Death of Robin Hood",
         "hasFile": True, "movieFile": {"id": 9}}


def _client_with_history(history_records):
    """RadarrClient wired to a mock Radarr; returns (client, request_log)."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/api/v3/movie" and request.method == "GET":
            return httpx.Response(200, json=[MOVIE])
        if path == "/api/v3/history/movie" and request.method == "GET":
            return httpx.Response(200, json=history_records)
        if path.startswith("/api/v3/history/failed/") and request.method == "POST":
            return httpx.Response(200, json={})
        if path == "/api/v3/moviefile/9" and request.method == "DELETE":
            return httpx.Response(200, json={})
        if path == "/api/v3/command" and request.method == "POST":
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"message": f"unexpected {request.method} {path}"})

    client = RadarrClient("http://radarr.test", "key")
    # Swap the real transport for the mock; base_url/headers stay as built.
    client._client = httpx.AsyncClient(
        base_url="http://radarr.test/api/v3",
        headers={"X-Api-Key": "key"},
        transport=httpx.MockTransport(handler),
    )
    return client, requests


async def test_mark_failed_blocklists_via_per_movie_history():
    # Newest-first, an import above the grab: it must pick the grab, not rec 0.
    client, requests = _client_with_history([
        {"id": 200, "eventType": "downloadFolderImported"},
        {"id": 123, "eventType": "grabbed"},
    ])
    result = await client.mark_failed(tmdb_id=42)
    await client.close()

    assert result.status == "ok"
    assert result.steps_done == ["blocklist", "delete", "search"]

    history_reqs = [r for r in requests if r.url.path == "/api/v3/history/movie"]
    assert len(history_reqs) == 1
    assert history_reqs[0].url.params["movieId"] == "5"
    # The paginated library-wide endpoint must never be hit (the old bug).
    assert not any(r.url.path == "/api/v3/history" for r in requests)
    assert any(r.url.path == "/api/v3/history/failed/123" for r in requests)


async def test_mark_failed_no_grab_falls_back_to_delete_search():
    client, requests = _client_with_history([
        {"id": 200, "eventType": "downloadFolderImported"},
    ])
    result = await client.mark_failed(tmdb_id=42)
    await client.close()

    assert result.status == "ok"
    assert result.steps_done == ["delete", "search"]
    assert "No prior grab to blocklist" in result.message
    assert not any("/history/failed/" in r.url.path for r in requests)
