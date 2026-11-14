"""A Fix of a file that holds several episodes: the Progress card filters its
queue read by them, a sibling that vanished is dropped, a timeout names what
is still missing, and Sonarr's wording names the siblings."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from bot import autofix_poll
from bot.autofix_poll import _missing_episodes, _notify_timeout, _paint_progress
from http_util import NotFoundAPIError
from sonarr import SonarrClient
from store import PendingAutofix
from tests._handler_harness import make_ctx, make_message


USER = 42
ADMIN = 999


def _fix(**kw) -> PendingAutofix:
    now = datetime.now(timezone.utc)
    base = dict(
        id=1, chat_id=100, user_id=USER, media_type="tv", radarr_movie_id=None,
        sonarr_series_id=8, sonarr_episode_id=501, sonarr_season=None,
        expected_episode_ids=[501, 502], label="Show S01E01", ticket_id=7,
        ticket_url="http://seerr.example/issues/7",
        started_at=now.strftime("%Y-%m-%d %H:%M:%S"),
        timeout_at=(now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))
    base.update(kw)
    return PendingAutofix(**base)


# --- the Progress card reads the queue for every tracked episode ----------------


async def test_progress_card_filters_the_queue_by_the_tracked_episodes():
    ctx = make_ctx()
    sonarr = ctx.bot_data["sonarr"]
    sonarr.get_queue_progress = AsyncMock(return_value=None)
    ctx.bot.edit_message_text = AsyncMock()
    await _paint_progress(ctx, ctx.bot_data["store"], _fix(message_id=300))
    sonarr.get_queue_progress.assert_awaited_once_with(8, [501, 502])


# --- a Sonarr client over canned episodes ----------------------------------------


def _client(handler) -> SonarrClient:
    c = SonarrClient("http://sonarr.test", "key", timeout=1.0)
    c._client = httpx.AsyncClient(base_url="http://sonarr.test/api/v3",
                                  transport=httpx.MockTransport(handler))
    return c


EPS = [
    {"id": 501, "seasonNumber": 1, "episodeNumber": 1, "title": "One",
     "hasFile": True, "episodeFileId": 9},
    {"id": 502, "seasonNumber": 1, "episodeNumber": 2, "title": "?",
     "hasFile": True, "episodeFileId": 9},
]


async def test_search_failure_names_the_episodes_the_deleted_file_held():
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 8, "tvdbId": 42, "title": "Show"}])
        if p == "/api/v3/episode":
            return httpx.Response(200, json=EPS)
        if p == "/api/v3/history":
            return httpx.Response(200, json={"records": []})
        if p.startswith("/api/v3/episodefile/"):
            return httpx.Response(200, json={})
        if p == "/api/v3/command":
            return httpx.Response(400, json={"message": "no"})
        return httpx.Response(404, json={})
    result = await _client(handler).auto_fix_episode(42, 1, 1)
    assert result.status == "partial"
    assert "couldn't trigger search" in result.message
    assert "also held S01E02" in result.message


async def test_preview_drops_a_missing_title_and_does_not_double_the_name():
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 8, "tvdbId": 42, "title": "Show"}])
        return httpx.Response(200, json=[{**EPS[1], "hasFile": False}])
    text = await _client(handler).describe_episode(42, 1, 2)
    assert text == "Sonarr: S01E02"

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")
    err = await _client(broken).describe_episode(42, 1, 2)
    assert err.startswith("Sonarr lookup failed")


# --- a sibling that 404s is dropped, the rest keep polling ------------------------


def _gone() -> NotFoundAPIError:
    return NotFoundAPIError("gone", status_code=404, service="Sonarr")


async def test_a_vanished_sibling_is_dropped_and_polling_goes_on():
    async def has_file(eid):
        if eid == 502:
            raise _gone()
        return eid == 501
    fix = _fix()
    assert await fix.is_complete(None, SimpleNamespace(episode_has_file=has_file)) == (True, "")
    assert fix.tracked_episode_ids() == [501]

    pending = _fix(expected_episode_ids=[501, 502, 503])
    assert await pending.is_complete(
        None, SimpleNamespace(episode_has_file=has_file)) == (False, "")


async def test_the_reported_episode_vanishing_still_raises():
    async def has_file(_eid):
        raise _gone()
    with pytest.raises(NotFoundAPIError):
        await _fix().is_complete(None, SimpleNamespace(episode_has_file=has_file))


# --- a timeout says which episodes are still missing -------------------------------


async def test_missing_episodes_names_only_the_ones_without_a_file():
    sonarr = SimpleNamespace(episode_file_states=AsyncMock(
        return_value=[("S01E01", True), ("S01E02", False)]))
    assert await _missing_episodes(_fix(), sonarr) == "S01E02"


async def test_missing_episodes_is_empty_when_nothing_came_back_or_unknowable():
    none_back = SimpleNamespace(episode_file_states=AsyncMock(
        return_value=[("S01E01", False), ("S01E02", False)]))
    assert await _missing_episodes(_fix(), none_back) == ""
    boom = SimpleNamespace(episode_file_states=AsyncMock(side_effect=RuntimeError))
    assert await _missing_episodes(_fix(), boom) == ""
    assert await _missing_episodes(_fix(expected_episode_ids=[]), none_back) == ""
    assert await _missing_episodes(_fix(), None) == ""


async def test_the_timeout_dm_names_the_missing_episodes():
    ctx = make_ctx(admin_id=ADMIN)
    ctx.bot.send_message = AsyncMock(return_value=make_message(message_id=5))
    await _notify_timeout(ctx, _fix(), "S01E02")
    text = ctx.bot.send_message.call_args.kwargs["text"]
    assert "Still no new file for S01E02." in text
    assert "No new file was imported" not in text


async def test_the_timeout_card_names_the_missing_episodes():
    fix = _fix(message_id=300, timeout_at="2020-01-01 00:00:00")
    ctx = make_ctx(admin_id=ADMIN)
    store = ctx.bot_data["store"]
    store.list_pending_autofixes = AsyncMock(return_value=[fix])
    store.mark_autofix_status = AsyncMock()
    ctx.bot_data["sonarr"].episode_file_states = AsyncMock(
        return_value=[("S01E01", True), ("S01E02", False)])
    ctx.bot.send_message = AsyncMock(return_value=make_message(message_id=5))
    ctx.bot.edit_message_text = AsyncMock()
    autofix_poll._inflight.clear()
    await autofix_poll.poll_pending_autofixes(ctx)
    assert "still missing S01E02" in ctx.bot.edit_message_text.call_args.kwargs["text"]


async def test_episode_file_states_skips_an_episode_sonarr_forgot():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/episode/502":
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"seasonNumber": 1, "episodeNumber": 1,
                                         "hasFile": True})
    assert await _client(handler).episode_file_states([501, 502]) == [("S01E01", True)]


# --- a dropped sibling stays dropped across a restart ---------------------------


async def test_a_dropped_sibling_is_saved_to_the_store(fresh_store):
    await fresh_store.add_pending_autofix(
        chat_id=100, user_id=USER, media_type="tv", label="Show S01E01",
        ticket_id=7, ticket_url="http://seerr.example/issues/7",
        sonarr_series_id=8, sonarr_episode_id=501,
        expected_episode_ids=[501, 502, 503])
    ctx = make_ctx(admin_id=ADMIN)
    ctx.bot_data["store"] = fresh_store

    async def has_file(eid):
        if eid == 502:
            raise _gone()
        return eid == 501
    ctx.bot_data["sonarr"] = SimpleNamespace(
        episode_has_file=has_file, refresh_monitored_downloads=AsyncMock(),
        episode_file_states=AsyncMock(return_value=[]))
    ctx.bot_data["radarr"] = None
    autofix_poll._inflight.clear()
    await autofix_poll.poll_pending_autofixes(ctx)
    [row] = await fresh_store.list_pending_autofixes()
    assert row.expected_episode_ids == [501, 503]
