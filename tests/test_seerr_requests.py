"""Wire-level tests for the Seerr request-client surface: create_request's
error taxonomy (409 duplicate, 403 quota-vs-permission, the 202 no-seasons
"success", ambiguous 2xx bodies), request-list parsing, the season
requestability cross-reference, and quota parsing."""
from __future__ import annotations

import json

import httpx
import pytest

import seerr as seerr_mod
from seerr import (
    MEDIA_STATUS_AVAILABLE,
    MEDIA_STATUS_PROCESSING,
    MEDIA_STATUS_UNKNOWN,
    REQUEST_STATUS_APPROVED,
    REQUEST_STATUS_COMPLETED,
    REQUEST_STATUS_DECLINED,
    REQUEST_STATUS_PENDING,
    AmbiguousResponseError,
    DuplicateRequestError,
    NothingToRequestError,
    QuotaExceededError,
    RequestPermissionError,
    SeerrClient,
)


def _client(monkeypatch, handler) -> SeerrClient:
    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(seerr_mod.httpx, "AsyncClient", patched)
    return SeerrClient("http://seerr.test", "key")


# --- create_request ----------------------------------------------------------

async def test_create_request_movie_sends_tmdb_id_and_parses_response(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 7, "status": REQUEST_STATUS_PENDING})

    client = _client(monkeypatch, handler)
    created = await client.create_request(media_type="movie", tmdb_id=550)
    assert captured["path"].endswith("/request")
    # mediaId is the TMDb id for requests (unlike issues, which use the
    # internal media.id) and no seasons key is sent for movies.
    assert captured["body"] == {"mediaType": "movie", "mediaId": 550}
    assert created.id == 7
    assert created.status == REQUEST_STATUS_PENDING
    await client.close()


async def test_create_request_tv_sends_season_list(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 8, "status": REQUEST_STATUS_APPROVED})

    client = _client(monkeypatch, handler)
    created = await client.create_request(media_type="tv", tmdb_id=1399,
                                          seasons=[1, 3])
    assert captured["body"]["seasons"] == [1, 3]
    # Auto-approved users get APPROVED back immediately; the flow words its
    # confirmation off this.
    assert created.status == REQUEST_STATUS_APPROVED
    await client.close()


async def test_create_request_tv_all_seasons_literal(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 9, "status": REQUEST_STATUS_PENDING})

    client = _client(monkeypatch, handler)
    await client.create_request(media_type="tv", tmdb_id=1399, seasons="all")
    assert captured["body"]["seasons"] == "all"
    await client.close()


async def test_create_request_409_raises_duplicate(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={
            "status": 409, "message": "Request for this media already exists."})

    client = _client(monkeypatch, handler)
    with pytest.raises(DuplicateRequestError) as exc_info:
        await client.create_request(media_type="movie", tmdb_id=550)
    assert "already exists" in str(exc_info.value)
    await client.close()


async def test_create_request_403_quota_raises_quota_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={
            "status": 403, "message": "Movie Quota exceeded."})

    client = _client(monkeypatch, handler)
    with pytest.raises(QuotaExceededError):
        await client.create_request(media_type="movie", tmdb_id=550)
    await client.close()


async def test_create_request_403_permission_raises_permission_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={
            "status": 403,
            "message": "You do not have permission to make movie requests."})

    client = _client(monkeypatch, handler)
    with pytest.raises(RequestPermissionError):
        await client.create_request(media_type="movie", tmdb_id=550)
    await client.close()


async def test_create_request_403_blocklist_is_permission_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={
            "status": 403, "message": "This media is blocklisted."})

    client = _client(monkeypatch, handler)
    with pytest.raises(RequestPermissionError) as exc_info:
        await client.create_request(media_type="movie", tmdb_id=550)
    assert "blocklisted" in str(exc_info.value)
    await client.close()


async def test_create_request_202_raises_nothing_to_request(monkeypatch):
    """Seerr's NoSeasonsAvailableError is a 202 'success' whose body is an
    error envelope; nothing was created and the flow must say 'already
    covered', not show a failure."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={
            "status": 202, "message": "No seasons available to request"})

    client = _client(monkeypatch, handler)
    with pytest.raises(NothingToRequestError):
        await client.create_request(media_type="tv", tmdb_id=1399, seasons="all")
    await client.close()


async def test_create_request_2xx_garbage_body_raises_ambiguous(monkeypatch):
    """A proxy's HTML 200 after the POST: the request may exist in Seerr, so
    the error must be the non-retryable ambiguous kind."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway ok</html>")

    client = _client(monkeypatch, handler)
    with pytest.raises(AmbiguousResponseError):
        await client.create_request(media_type="movie", tmdb_id=550)
    await client.close()


async def test_create_request_missing_id_raises_ambiguous(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"status": REQUEST_STATUS_PENDING})

    client = _client(monkeypatch, handler)
    with pytest.raises(AmbiguousResponseError) as exc_info:
        await client.create_request(media_type="movie", tmdb_id=550)
    assert "duplicate" in str(exc_info.value)
    await client.close()


# --- list_requests / get_request --------------------------------------------

_REQUEST_ITEM = {
    "id": 12,
    "status": REQUEST_STATUS_PENDING,
    "createdAt": "2026-08-16T10:00:00.000Z",
    "media": {"tmdbId": 1399, "mediaType": "tv",
              "status": MEDIA_STATUS_PROCESSING},
    "requestedBy": {"id": 42, "displayName": "user1"},
    "seasons": [{"seasonNumber": 1}, {"seasonNumber": 3}],
}


async def test_list_requests_parses_items_and_total(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["filter"] == "all"
        return httpx.Response(200, json={
            "pageInfo": {"results": 31},
            "results": [_REQUEST_ITEM, {"media": {}}],  # id-less row skipped
        })

    client = _client(monkeypatch, handler)
    items, total = await client.list_requests()
    assert len(items) == 1
    item = items[0]
    assert (item.id, item.status, item.media_type, item.tmdb_id) == \
        (12, REQUEST_STATUS_PENDING, "tv", 1399)
    assert item.seasons == [1, 3]
    assert item.requested_by == "user1"
    assert item.requested_by_id == 42
    assert total == 31
    await client.close()


async def test_get_request_parses_single_item(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/request/12")
        return httpx.Response(200, json=_REQUEST_ITEM)

    client = _client(monkeypatch, handler)
    item = await client.get_request(12)
    assert item.id == 12
    await client.close()


async def test_delete_request_issues_delete(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(204)

    client = _client(monkeypatch, handler)
    await client.delete_request(12)
    assert captured["method"] == "DELETE"
    assert captured["path"].endswith("/request/12")
    await client.close()


# --- get_tv_season_availability ---------------------------------------------

async def test_season_availability_mirrors_seerr_drop_rules(monkeypatch):
    """S1 available (entity status), S2 in an active pending request, S3 in a
    DECLINED request (requestable again), S4 untouched, S5 in a FAILED
    request (still blocks, matching the server), S6 in a 4K-only request
    (does NOT block the standard track), S0 omitted."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "seasons": [
                {"seasonNumber": 0, "episodeCount": 3},
                {"seasonNumber": 1, "episodeCount": 10},
                {"seasonNumber": 2, "episodeCount": 10},
                {"seasonNumber": 3, "episodeCount": 10},
                {"seasonNumber": 4, "episodeCount": 8},
                {"seasonNumber": 5, "episodeCount": 8},
                {"seasonNumber": 6, "episodeCount": 8},
            ],
            "mediaInfo": {
                "seasons": [
                    {"seasonNumber": 1, "status": MEDIA_STATUS_AVAILABLE},
                    {"seasonNumber": 3, "status": MEDIA_STATUS_UNKNOWN},
                ],
                "requests": [
                    {"status": REQUEST_STATUS_PENDING,
                     "seasons": [{"seasonNumber": 2}]},
                    {"status": REQUEST_STATUS_DECLINED,
                     "seasons": [{"seasonNumber": 3}]},
                    {"status": 4,  # FAILED
                     "seasons": [{"seasonNumber": 5}]},
                    {"status": REQUEST_STATUS_PENDING, "is4k": True,
                     "seasons": [{"seasonNumber": 6}]},
                ],
            },
        })

    client = _client(monkeypatch, handler)
    seasons = await client.get_tv_season_availability(1399)
    by_number = {s.season_number: s.requestable for s in seasons}
    assert 0 not in by_number  # specials never offered
    assert by_number == {1: False, 2: False, 3: True, 4: True,
                         5: False, 6: True}
    await client.close()


async def test_season_availability_completed_request_does_not_block(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "seasons": [{"seasonNumber": 1, "episodeCount": 10}],
            "mediaInfo": {
                "seasons": [{"seasonNumber": 1, "status": MEDIA_STATUS_UNKNOWN}],
                "requests": [
                    {"status": REQUEST_STATUS_COMPLETED,
                     "seasons": [{"seasonNumber": 1}]},
                ],
            },
        })

    client = _client(monkeypatch, handler)
    seasons = await client.get_tv_season_availability(1399)
    assert seasons[0].requestable is True
    await client.close()


async def test_season_availability_no_media_info(monkeypatch):
    """A show Seerr has never seen: every non-special season requestable."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "seasons": [{"seasonNumber": 1, "episodeCount": 10},
                        {"seasonNumber": 2, "episodeCount": 10}],
        })

    client = _client(monkeypatch, handler)
    seasons = await client.get_tv_season_availability(1399)
    assert all(s.requestable for s in seasons)
    assert [s.season_number for s in seasons] == [1, 2]
    await client.close()


# --- get_quota ---------------------------------------------------------------

async def test_get_quota_parses_buckets(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/user/42/quota")
        return httpx.Response(200, json={
            "movie": {"days": 7, "limit": 10, "used": 6, "remaining": 4,
                      "restricted": False},
            "tv": {"days": 7, "limit": 5, "used": 5, "remaining": 0,
                   "restricted": True},
        })

    client = _client(monkeypatch, handler)
    quota = await client.get_quota(42)
    assert (quota.movie.limit, quota.movie.remaining, quota.movie.restricted) == \
        (10, 4, False)
    assert (quota.tv.used, quota.tv.remaining, quota.tv.restricted) == (5, 0, True)
    await client.close()


async def test_get_quota_unlimited_has_no_remaining(monkeypatch):
    """limit 0 = unlimited: remaining is None, never a misleading number."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"movie": {}, "tv": {}})

    client = _client(monkeypatch, handler)
    quota = await client.get_quota(42)
    assert quota.movie.limit == 0
    assert quota.movie.remaining is None
    assert quota.movie.restricted is False
    await client.close()


# --- search availability passthrough -----------------------------------------

async def test_search_carries_media_status(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [
            {"mediaType": "movie", "id": 550, "title": "Fight Club",
             "releaseDate": "1999-10-15",
             "mediaInfo": {"id": 3, "status": MEDIA_STATUS_AVAILABLE}},
            {"mediaType": "movie", "id": 551, "title": "No Info",
             "releaseDate": "2001-01-01"},
        ]})

    client = _client(monkeypatch, handler)
    results = await client.search("fight club")
    assert results[0].status == MEDIA_STATUS_AVAILABLE
    assert results[1].status is None
    await client.close()


# --- per-user attribution -----------------------------------------------------

async def test_create_request_with_token_uses_user_session(monkeypatch):
    """With as_plex_token the POST /request must ride the per-user client
    (cookie session; no X-Api-Key header), so Seerr attributes the request
    to the real user and applies THEIR quota/permissions."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/auth/plex"):
            return httpx.Response(200, headers={"Set-Cookie": "connect.sid=abc"},
                                  json={"id": 42, "displayName": "user1"})
        return httpx.Response(201, json={"id": 5, "status": REQUEST_STATUS_PENDING})

    client = _client(monkeypatch, handler)
    created = await client.create_request(media_type="movie", tmdb_id=550,
                                          as_plex_token="plex-tok")
    assert created.id == 5
    request_call = seen[-1]
    assert request_call.url.path.endswith("/request")
    assert "x-api-key" not in {k.lower() for k in request_call.headers}
    await client.close()


# --- webhook notification-settings check --------------------------------------

async def test_webhook_notification_settings_parsed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/settings/notifications/webhook")
        return httpx.Response(200, json={"enabled": True, "types": 4062})

    client = _client(monkeypatch, handler)
    enabled, types = await client.get_webhook_notification_settings()
    assert enabled is True and types == 4062
    await client.close()


def test_required_webhook_bits_cover_all_handled_events():
    """The /status check must require exactly the events the bot handles
    (minus the deliberately silent/unhandled ones)."""
    from seerr import REQUIRED_WEBHOOK_TYPES
    assert set(REQUIRED_WEBHOOK_TYPES.values()) == {
        "Request Pending Approval", "Request Approved", "Request Available",
        "Request Processing Failed", "Request Declined",
        "Request Automatically Approved", "Issue Reported", "Issue Comment",
        "Issue Resolved"}
    # 4062 (a fully-configured live instance) satisfies the requirement.
    assert all(4062 & bit for bit in REQUIRED_WEBHOOK_TYPES)


async def test_create_request_parses_granted_seasons(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={
            "id": 8, "status": REQUEST_STATUS_PENDING,
            "seasons": [{"seasonNumber": 2}, {"seasonNumber": 3}]})

    client = _client(monkeypatch, handler)
    created = await client.create_request(media_type="tv", tmdb_id=1399,
                                          seasons=[1, 2, 3])
    assert created.seasons == [2, 3]
    await client.close()


async def test_search_percent_encodes_reserved_characters(monkeypatch):
    """Seerr 400s on raw reserved characters in the query string (a verbatim
    "Goodbye, Lara" search died on its comma); the client must encode with
    no safe characters."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["raw"] = request.url.raw_path.decode()
        return httpx.Response(200, json={"results": []})

    client = _client(monkeypatch, handler)
    await client.search("goodbye, lara & friends")
    assert "query=goodbye%2C%20lara%20%26%20friends" in captured["raw"]
    await client.close()
