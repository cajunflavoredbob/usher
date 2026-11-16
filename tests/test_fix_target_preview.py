"""A TV Fix shows which Sonarr episode the typed number resolves to before the
file is deleted, and the episode picker refuses a number the season lacks."""
from __future__ import annotations

from unittest.mock import AsyncMock

import httpx

from bot.callback_prefixes import TICKET_AUTOFIX_OFFER
from bot.ticket_flow import ticket_offer_autofix, ticket_pick_episode
from bot.tickets import describe_fix_target, tk_fix
from seerr import IssueListItem, TvSeason
from sonarr import SonarrClient
from tests._handler_harness import make_ctx, make_link, make_update

USER_ID = 42
ADMIN_ID = 999
MEDIA = {"type": "tv", "tmdb_id": 7, "tvdb_id": 42, "title": "Show"}


def _sonarr(episodes, series_ok=True):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/series":
            return httpx.Response(
                200, json=[{"id": 8, "tvdbId": 42, "title": "Show"}] if series_ok else [])
        if request.url.path == "/api/v3/episode":
            if episodes is None:
                return httpx.Response(500, json={})
            return httpx.Response(200, json=episodes)
        return httpx.Response(404, json={})

    client = SonarrClient("http://sonarr.test", "key", timeout=1.0)
    client._client = httpx.AsyncClient(
        base_url="http://sonarr.test/api/v3", transport=httpx.MockTransport(handler))
    return client


EPS = [
    {"id": 1, "seasonNumber": 1, "episodeNumber": 1, "title": "Pilot",
     "hasFile": True, "episodeFileId": 9},
    {"id": 2, "seasonNumber": 1, "episodeNumber": 2, "title": "Second",
     "hasFile": True, "episodeFileId": 9},
    {"id": 3, "seasonNumber": 1, "episodeNumber": 3, "title": "Third",
     "hasFile": True, "episodeFileId": 10},
]


async def test_describe_shows_the_sonarr_title():
    text = await _sonarr(EPS).describe_episode(42, 1, 3)
    assert text == 'Episode: S01E03 "Third"'


async def test_describe_names_the_siblings_of_a_shared_file():
    text = await _sonarr(EPS).describe_episode(42, 1, 1)
    assert 'Episode: S01E01 "Pilot"' in text
    assert "also holds S01E02" in text


async def test_describe_says_when_the_episode_is_not_in_sonarr():
    text = await _sonarr(EPS).describe_episode(42, 1, 9)
    assert text == "Episode: S01E09 not found in the download system."


async def test_describe_says_when_the_show_is_not_in_sonarr():
    text = await _sonarr(EPS, series_ok=False).describe_episode(42, 1, 1)
    assert text == "Episode: Series isn't in the download system."


async def test_describe_says_when_the_lookup_fails(monkeypatch):
    async def _no_sleep(_):
        return None
    monkeypatch.setattr("http_util.asyncio.sleep", _no_sleep)
    text = await _sonarr(None).describe_episode(42, 1, 1)
    assert text.startswith("Episode: Couldn't look up the episode")


async def test_describe_fix_target_is_empty_without_an_episode_or_sonarr():
    sonarr = _sonarr(EPS)
    assert await describe_fix_target(MEDIA, 1, None, sonarr) == ""
    assert await describe_fix_target(MEDIA, 1, 1, None) == ""


async def test_describe_fix_target_says_so_for_a_movie_with_no_title():
    sonarr = _sonarr(EPS)
    assert await describe_fix_target(
        {"type": "movie", "tmdb_id": 1}, None, None, sonarr
    ) == "Movie: couldn't check which movie this is."


async def test_describe_fix_target_names_a_movie_by_title_and_year():
    movie = {"type": "movie", "tmdb_id": 348, "title": "Alien", "year": "1979"}
    assert await describe_fix_target(movie, None, None, None) == "Movie: Alien (1979)"
    no_year = {**movie, "year": ""}
    assert await describe_fix_target(no_year, None, None, None) == "Movie: Alien"


async def test_reporter_confirm_shows_the_sonarr_episode():
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID),
                   user_data={"search_version": 1, "media": MEDIA,
                              "season": 1, "episode": 3})
    ctx.bot_data["sonarr"] = _sonarr(EPS)
    upd = make_update(callback_data=f"{TICKET_AUTOFIX_OFFER}:1:yes", user_id=USER_ID)
    await ticket_offer_autofix(upd, ctx)
    text = upd.callback_query.edits[-1]["text"]
    assert 'Episode: S01E03 "Third"' in text
    assert "delete the current file" in text


async def test_reporter_confirm_still_asks_when_sonarr_lookup_fails():
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID),
                   user_data={"search_version": 1, "media": MEDIA,
                              "season": 1, "episode": 3})
    ctx.bot_data["sonarr"].describe_episode = AsyncMock(
        return_value="Sonarr: couldn't check which episode this is.")
    upd = make_update(callback_data=f"{TICKET_AUTOFIX_OFFER}:1:yes", user_id=USER_ID)
    await ticket_offer_autofix(upd, ctx)
    text = upd.callback_query.edits[-1]["text"]
    assert "couldn't check which episode" in text
    assert "Confirm?" in text


async def test_admin_fix_menu_shows_the_sonarr_episode():
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["seerr"].get_issue.return_value = IssueListItem(
        id=5, issue_type=1, status=1, created_at="2026-05-29T12:00:00Z",
        tmdb_id=7, media_type="tv", problem_season=1, problem_episode=3,
        created_by="someone")
    ctx.bot_data["seerr"].get_tv_seasons.return_value = ([], 42)
    ctx.bot_data["sonarr"] = _sonarr(EPS)
    upd = make_update(callback_data="tkf:5", user_id=ADMIN_ID)
    await tk_fix(upd, ctx)
    text = upd.callback_query.edits[-1]["text"]
    assert 'Episode: S01E03 "Third"' in text
    assert "delete the current file" in text


async def test_pick_episode_refuses_a_number_past_the_season():
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID),
                   user_data={"search_version": 1, "season": 1,
                              "seasons": {1: TvSeason(season_number=1, episode_count=5)}})
    upd = make_update(callback_data="ep:1:6", user_id=USER_ID)
    await ticket_pick_episode(upd, ctx)
    assert "isn't in this season" in upd.callback_query.edits[0]["text"]
    assert "episode" not in ctx.user_data


async def test_pick_episode_accepts_the_last_episode_and_the_whole_season():
    for data, expected in (("ep:1:5", 5), ("ep:1:0", None)):
        ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID),
                       user_data={"search_version": 1, "season": 1,
                                  "media": {"type": "tv", "title": "Show"},
                                  "seasons": {1: TvSeason(season_number=1, episode_count=5)}})
        await ticket_pick_episode(make_update(callback_data=data, user_id=USER_ID), ctx)
        assert ctx.user_data["episode"] == expected
