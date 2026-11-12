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

MOVIE = {"id": 5, "tmdbId": 42, "title": "The Death of Robin Hood",
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
        {"id": 200, "eventType": "downloadFolderImported", "downloadId": "A"},
        {"id": 123, "eventType": "grabbed", "downloadId": "A"},
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


async def test_lookup_scans_for_requested_tmdb_id():
    """Identity guard: if the tmdbId filter is ignored (version drift, proxy
    stripping the query string), the response holds arbitrary movies. The
    lookup must scan for the requested ID, never trust items[0] -- this
    result feeds a blocklist+delete workflow."""
    other = {"id": 7, "tmdbId": 999, "title": "Some Other Movie",
             "hasFile": True, "movieFile": {"id": 3}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[other, MOVIE])

    client = RadarrClient("http://radarr.test", "key")
    client._client = httpx.AsyncClient(
        base_url="http://radarr.test/api/v3",
        transport=httpx.MockTransport(handler),
    )
    movie = await client.get_movie_by_tmdb(42)
    await client.close()
    assert movie is not None and movie.id == 5


async def test_lookup_returns_none_when_only_wrong_movies_returned():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": 7, "tmdbId": 999, "title": "Wrong",
                                          "hasFile": True, "movieFile": {"id": 3}}])

    client = RadarrClient("http://radarr.test", "key")
    client._client = httpx.AsyncClient(
        base_url="http://radarr.test/api/v3",
        transport=httpx.MockTransport(handler),
    )
    assert await client.get_movie_by_tmdb(42) is None
    await client.close()


async def test_mark_failed_no_grab_falls_back_to_delete_search():
    client, requests = _client_with_history([
        {"id": 200, "eventType": "downloadFolderImported"},
    ])
    result = await client.mark_failed(tmdb_id=42)
    await client.close()

    assert result.status == "ok"
    assert result.steps_done == ["delete", "search"]
    assert "Couldn't identify the release" in result.message
    assert not any("/history/failed/" in r.url.path for r in requests)


async def test_mark_failed_blocklists_grab_that_matches_the_import_not_the_newest_grab():
    # A later grab (B) is in flight; the file on disk came from grab A.
    client, requests = _client_with_history([
        {"id": 300, "eventType": "grabbed", "downloadId": "B"},
        {"id": 200, "eventType": "downloadFolderImported", "downloadId": "A"},
        {"id": 123, "eventType": "grabbed", "downloadId": "A"},
    ])
    result = await client.mark_failed(tmdb_id=42)
    await client.close()

    assert result.steps_done == ["blocklist", "delete", "search"]
    failed = [r.url.path for r in requests if "/history/failed/" in r.url.path]
    assert failed == ["/api/v3/history/failed/123"]


async def test_mark_failed_skips_blocklist_when_no_grab_shares_the_import_download_id():
    client, requests = _client_with_history([
        {"id": 300, "eventType": "grabbed", "downloadId": "B"},
        {"id": 200, "eventType": "downloadFolderImported", "downloadId": "A"},
    ])
    result = await client.mark_failed(tmdb_id=42)
    await client.close()

    assert result.status == "ok"
    assert result.steps_done == ["delete", "search"]
    assert "wasn't blocklisted" in result.message
    assert not any("/history/failed/" in r.url.path for r in requests)


async def test_mark_failed_skips_blocklist_when_there_is_no_import_event():
    client, requests = _client_with_history([
        {"id": 123, "eventType": "grabbed", "downloadId": "A"},
    ])
    result = await client.mark_failed(tmdb_id=42)
    await client.close()

    assert result.steps_done == ["delete", "search"]
    assert not any("/history/failed/" in r.url.path for r in requests)


async def test_mark_failed_skips_blocklist_when_the_import_has_no_download_id():
    client, requests = _client_with_history([
        {"id": 200, "eventType": "downloadFolderImported", "downloadId": ""},
        {"id": 123, "eventType": "grabbed", "downloadId": ""},
    ])
    result = await client.mark_failed(tmdb_id=42)
    await client.close()

    assert result.steps_done == ["delete", "search"]
    assert not any("/history/failed/" in r.url.path for r in requests)


def _stateful_client(*, delete_landed: bool):
    """Mock Radarr whose first DELETE times out; the retry gets a 404 because
    the first one landed (delete_landed) or is a plain 404 with the file kept."""
    state = {"has_file": True, "deletes": 0}
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/api/v3/movie" and request.method == "GET":
            return httpx.Response(200, json=[MOVIE])
        if path == "/api/v3/movie/5" and request.method == "GET":
            return httpx.Response(200, json={**MOVIE, "hasFile": state["has_file"]})
        if path == "/api/v3/moviefile/9" and request.method == "DELETE":
            state["deletes"] += 1
            if state["deletes"] == 1:
                if delete_landed:
                    state["has_file"] = False
                raise httpx.ReadTimeout("slow")
            return httpx.Response(404, json={"message": "NotFound"})
        if path == "/api/v3/command" and request.method == "POST":
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"message": "unexpected"})

    client = RadarrClient("http://radarr.test", "key")
    client._client = httpx.AsyncClient(
        base_url="http://radarr.test/api/v3",
        transport=httpx.MockTransport(handler),
    )
    return client, requests


async def test_auto_fix_searches_when_retried_delete_404s_after_landing(monkeypatch):
    async def _no_sleep(_):
        return None
    monkeypatch.setattr("http_util.asyncio.sleep", _no_sleep)
    client, requests = _stateful_client(delete_landed=True)
    result = await client.auto_fix(tmdb_id=42)
    await client.close()

    assert result.status == "ok"
    assert result.steps_done == ["delete", "search"]
    assert any(r.url.path == "/api/v3/command" for r in requests)


async def test_auto_fix_stays_partial_when_file_still_there_after_failed_delete(monkeypatch):
    async def _no_sleep(_):
        return None
    monkeypatch.setattr("http_util.asyncio.sleep", _no_sleep)
    client, requests = _stateful_client(delete_landed=False)
    result = await client.auto_fix(tmdb_id=42)
    await client.close()

    assert result.status == "partial"
    assert "couldn't delete file" in result.message
    assert not any(r.url.path == "/api/v3/command" for r in requests)


async def test_auto_fix_stays_partial_when_recheck_also_fails(monkeypatch):
    async def _no_sleep(_):
        return None
    monkeypatch.setattr("http_util.asyncio.sleep", _no_sleep)
    client, requests = _stateful_client(delete_landed=True)
    inner = client._client._transport

    def handler(request):
        if request.url.path == "/api/v3/movie/5":
            return httpx.Response(400, json={"message": "bad"})
        return inner.handler(request)

    client._client = httpx.AsyncClient(
        base_url="http://radarr.test/api/v3",
        transport=httpx.MockTransport(handler),
    )
    result = await client.auto_fix(tmdb_id=42)
    await client.close()

    assert result.status == "partial"
    assert not any(r.url.path == "/api/v3/command" for r in requests)
