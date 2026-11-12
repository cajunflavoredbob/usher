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
    """A churn-heavy episode can push the grabbed event past the
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
                # A full page of non-grab churn pushes the grab off page 1;
                # the newest import (first record) names the release.
                return httpx.Response(200, json={"records": [
                    {"id": 1000 + i, "eventType": "downloadFolderImported",
                     "downloadId": "A"}
                    for i in range(_HISTORY_PAGE_SIZE)
                ]})
            return httpx.Response(200, json={"records": [
                {"id": 777, "eventType": "grabbed", "downloadId": "A"},
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


def _episode_history_client(records):
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
            return httpx.Response(200, json={"records": records})
        if path.startswith("/api/v3/history/failed/"):
            return httpx.Response(200, json={})
        if path == "/api/v3/episodefile/9":
            return httpx.Response(200, json={})
        if path == "/api/v3/command":
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"message": "unexpected"})

    client = SonarrClient("http://sonarr.test", "key")
    client._client = httpx.AsyncClient(
        base_url="http://sonarr.test/api/v3",
        transport=httpx.MockTransport(handler),
    )
    return client, requests


async def test_mark_failed_blocklists_grab_that_matches_the_import_not_the_newest_grab():
    client, requests = _episode_history_client([
        {"id": 300, "eventType": "grabbed", "downloadId": "B"},
        {"id": 200, "eventType": "downloadFolderImported", "downloadId": "A"},
        {"id": 123, "eventType": "grabbed", "downloadId": "A"},
    ])
    result = await client.mark_failed_episode(42, 1, 5)
    await client.close()

    assert result.steps_done == ["blocklist", "delete", "search"]
    failed = [r.url.path for r in requests if "/history/failed/" in r.url.path]
    assert failed == ["/api/v3/history/failed/123"]


async def test_mark_failed_skips_blocklist_when_no_grab_shares_the_import_download_id():
    client, requests = _episode_history_client([
        {"id": 300, "eventType": "grabbed", "downloadId": "B"},
        {"id": 200, "eventType": "downloadFolderImported", "downloadId": "A"},
    ])
    result = await client.mark_failed_episode(42, 1, 5)
    await client.close()

    assert result.status == "ok"
    assert result.steps_done == ["delete", "search"]
    assert "wasn't blocklisted" in result.message
    assert not any("/history/failed/" in r.url.path for r in requests)


async def test_mark_failed_skips_blocklist_when_there_is_no_import_event():
    client, requests = _episode_history_client([
        {"id": 123, "eventType": "grabbed", "downloadId": "A"},
    ])
    result = await client.mark_failed_episode(42, 1, 5)
    await client.close()

    assert result.steps_done == ["delete", "search"]
    assert not any("/history/failed/" in r.url.path for r in requests)


async def test_mark_failed_skips_blocklist_when_the_import_has_no_download_id():
    client, requests = _episode_history_client([
        {"id": 200, "eventType": "downloadFolderImported", "downloadId": ""},
        {"id": 123, "eventType": "grabbed", "downloadId": ""},
    ])
    result = await client.mark_failed_episode(42, 1, 5)
    await client.close()

    assert result.steps_done == ["delete", "search"]
    assert not any("/history/failed/" in r.url.path for r in requests)


async def test_queue_progress_falls_back_when_records_lack_episode_ids():
    """Season packs carry no per-episode ids; the filter must fall back to
    the series' records instead of reporting nothing downloading."""
    import httpx
    from sonarr import SonarrClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/queue/details")
        return httpx.Response(200, json=[
            {"size": 1000, "sizeleft": 250, "timeleft": "00:05:00",
             "downloadId": "SABnzbd_nzo_pack", "title": "Season pack"},
        ])

    c = SonarrClient("http://sonarr.test:8989", "k")
    c._client = httpx.AsyncClient(base_url="http://sonarr.test:8989/api/v3",
                                  transport=httpx.MockTransport(handler))
    progress = await c.get_queue_progress(5, episode_ids=[9])
    assert progress is not None and progress.percent == 75
    await c.close()


async def test_queue_progress_filters_when_episode_ids_present():
    import httpx
    from sonarr import SonarrClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"size": 1000, "sizeleft": 0, "episodeId": 9,
             "downloadId": "a"},
            {"size": 1000, "sizeleft": 1000, "episodeId": 10,
             "downloadId": "b"},
        ])

    c = SonarrClient("http://sonarr.test:8989", "k")
    c._client = httpx.AsyncClient(base_url="http://sonarr.test:8989/api/v3",
                                  transport=httpx.MockTransport(handler))
    progress = await c.get_queue_progress(5, episode_ids=[9])
    assert progress.count == 1 and progress.percent == 100
    await c.close()


async def test_queue_progress_ignores_other_episodes_downloads():
    """A Fix on episode 9 must not report (or boost) episode 10's download."""
    import httpx
    from sonarr import SonarrClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"size": 1000, "sizeleft": 500, "episodeId": 10,
             "downloadId": "b"},
            {"size": 1000, "sizeleft": 500, "episodes": [{"id": 11}],
             "downloadId": "c"},
        ])

    c = SonarrClient("http://sonarr.test:8989", "k")
    c._client = httpx.AsyncClient(base_url="http://sonarr.test:8989/api/v3",
                                  transport=httpx.MockTransport(handler))
    assert await c.get_queue_progress(5, episode_ids=[9]) is None
    await c.close()


async def test_queue_progress_idless_record_counts_beside_other_episodes():
    import httpx
    from sonarr import SonarrClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"size": 1000, "sizeleft": 500, "episodeId": 10,
             "downloadId": "b"},
            {"size": 1000, "sizeleft": 250, "episodes": [],
             "downloadId": "pack"},
        ])

    c = SonarrClient("http://sonarr.test:8989", "k")
    c._client = httpx.AsyncClient(base_url="http://sonarr.test:8989/api/v3",
                                  transport=httpx.MockTransport(handler))
    progress = await c.get_queue_progress(5, episode_ids=[9])
    assert progress is not None and progress.count == 1 and progress.percent == 75
    await c.close()


def _stateful_client(*, delete_landed: bool):
    """Mock Sonarr whose first DELETE times out; the retry gets a 404."""
    state = {"has_file": True, "deletes": 0}
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 8, "tvdbId": 42, "title": "Show"}])
        if path == "/api/v3/episode" and request.method == "GET":
            return httpx.Response(200, json=[{
                "id": 70, "seasonNumber": 1, "episodeNumber": 9, "title": "E",
                "hasFile": True, "episodeFileId": 33}])
        if path == "/api/v3/episode/70" and request.method == "GET":
            return httpx.Response(200, json={"id": 70, "hasFile": state["has_file"]})
        if path == "/api/v3/episodefile/33" and request.method == "DELETE":
            state["deletes"] += 1
            if state["deletes"] == 1:
                if delete_landed:
                    state["has_file"] = False
                raise httpx.ReadTimeout("slow")
            return httpx.Response(404, json={"message": "NotFound"})
        if path == "/api/v3/command" and request.method == "POST":
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"message": "unexpected"})

    client = SonarrClient("http://sonarr.test", "key")
    client._client = httpx.AsyncClient(
        base_url="http://sonarr.test/api/v3",
        transport=httpx.MockTransport(handler),
    )
    return client, requests


async def test_auto_fix_episode_searches_when_retried_delete_404s_after_landing(monkeypatch):
    async def _no_sleep(_):
        return None
    monkeypatch.setattr("http_util.asyncio.sleep", _no_sleep)
    client, requests = _stateful_client(delete_landed=True)
    result = await client.auto_fix_episode(tvdb_id=42, season=1, episode=9)
    await client.close()

    assert result.status == "ok"
    assert result.steps_done == ["delete", "search"]
    assert any(r.url.path == "/api/v3/command" for r in requests)


async def test_auto_fix_episode_stays_partial_when_file_still_there(monkeypatch):
    async def _no_sleep(_):
        return None
    monkeypatch.setattr("http_util.asyncio.sleep", _no_sleep)
    client, requests = _stateful_client(delete_landed=False)
    result = await client.auto_fix_episode(tvdb_id=42, season=1, episode=9)
    await client.close()

    assert result.status == "partial"
    assert "couldn't delete file" in result.message
    assert not any(r.url.path == "/api/v3/command" for r in requests)
