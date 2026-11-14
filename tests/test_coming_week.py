"""Tests for the Requests Hub's "Coming this week": the 7-day window from
Sonarr and Radarr, each entry's day, the Only-what-I-follow filter, paging
and the cases where a service is missing or failing."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.callback_prefixes import REQUESTS_WEEK
from bot.coming_week import WEEK_PAGE_SIZE, coming_week_hub

from tests._handler_harness import make_ctx, make_link, make_update

USER_ID = 42
ADMIN_ID = 999
TODAY = date.today()


def _iso(day: date) -> str:
    return day.isoformat()


def _ep(title, tmdb, day, season=1, number=1):
    # Noon UTC keeps the local day equal to `day` in any zone within 11h.
    stamp = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)
    return {"airDateUtc": stamp.isoformat().replace("+00:00", "Z"),
            "seasonNumber": season, "episodeNumber": number,
            "series": {"title": title, "tmdbId": tmdb}}


def _movie(title, tmdb, **dates):
    return {"title": title, "tmdbId": tmdb, **dates}


def _ctx(*, sonarr=None, radarr=None, linked=True, follows=()):
    ctx = make_ctx(admin_id=ADMIN_ID)
    store = SimpleNamespace(
        get=AsyncMock(return_value=make_link(telegram_id=USER_ID)
                      if linked else None),
        is_following=AsyncMock(return_value=False),
        list_follow_ids=AsyncMock(return_value=[
            (t, v) for t, v in follows]))
    ctx.bot_data.update(store=store, sonarr=sonarr, radarr=radarr)
    return ctx


def _sonarr(eps):
    return SimpleNamespace(get_calendar=AsyncMock(return_value=eps))


def _radarr(movies):
    return SimpleNamespace(get_calendar=AsyncMock(return_value=movies))


async def _tap(ctx, data=REQUESTS_WEEK):
    upd = make_update(callback_data=data, user_id=USER_ID)
    await coming_week_hub(upd, ctx)
    return upd.callback_query.edits[-1]


def _cbs(edit):
    return [b.callback_data for row in edit["reply_markup"].inline_keyboard
            for b in row]


async def test_lists_episodes_and_movies_in_the_next_seven_days_by_day():
    d = lambda n: TODAY + timedelta(days=n)  # noqa: E731
    sonarr = _sonarr([_ep("Late Show", 2, d(5), 2, 3),
                      _ep("Early Show", 1, d(1), 1, 9),
                      _ep("Too Far", 3, d(7))])
    radarr = _radarr([_movie("Film A", 10, digitalRelease=_iso(d(3)) + "T00:00:00Z"),
                      _movie("Old Film", 11, inCinemas=_iso(d(-2)))])
    ctx = _ctx(sonarr=sonarr, radarr=radarr)
    edit = await _tap(ctx)
    text = edit["text"]
    assert text.index("Early Show") < text.index("Film A") < text.index("Late Show")
    assert "S01E09" in text and "S02E03" in text and "digital" in text
    assert "Too Far" not in text and "Old Film" not in text
    assert d(1).strftime("%a") in text
    start, end = sonarr.get_calendar.call_args.args
    assert start == _iso(TODAY) and end == _iso(d(7))
    assert radarr.get_calendar.call_args.args == (start, end)


async def test_movie_with_two_release_dates_in_the_window_appears_twice():
    radarr = _radarr([_movie("Film", 10, inCinemas=_iso(TODAY),
                             physicalRelease=_iso(TODAY + timedelta(days=2)))])
    edit = await _tap(_ctx(radarr=radarr))
    assert edit["text"].count("Film") == 2
    assert "in cinemas" in edit["text"] and "on disc" in edit["text"]


async def test_only_what_i_follow_keeps_followed_shows_alone():
    sonarr = _sonarr([_ep("Followed", 1, TODAY), _ep("Other", 2, TODAY)])
    radarr = _radarr([_movie("Film", 10, inCinemas=_iso(TODAY))])
    ctx = _ctx(sonarr=sonarr, radarr=radarr, follows=[(1, 0)])
    everything = await _tap(ctx)
    assert f"{REQUESTS_WEEK}:1:0" in _cbs(everything)
    assert all(n in everything["text"] for n in ("Followed", "Other", "Film"))
    only = await _tap(ctx, f"{REQUESTS_WEEK}:1:0")
    assert "Followed" in only["text"]
    assert "Other" not in only["text"] and "Film" not in only["text"]
    assert f"{REQUESTS_WEEK}:0:0" in _cbs(only)


async def test_unlinked_user_gets_no_filter_button_and_forced_everything():
    sonarr = _sonarr([_ep("Other", 2, TODAY)])
    ctx = _ctx(sonarr=sonarr, linked=False)
    edit = await _tap(ctx, f"{REQUESTS_WEEK}:1:0")
    assert "Other" in edit["text"]
    assert not any(c.startswith(f"{REQUESTS_WEEK}:") for c in _cbs(edit))


async def test_missing_radarr_leaves_out_movies_only():
    ctx = _ctx(sonarr=_sonarr([_ep("Show", 1, TODAY)]))
    edit = await _tap(ctx)
    assert "Show" in edit["text"] and "Couldn't" not in edit["text"]


async def test_neither_service_shows_a_clear_message():
    edit = await _tap(_ctx())
    assert "aren't set up" in edit["text"]


async def test_failing_service_is_noted_and_the_other_still_lists():
    radarr = SimpleNamespace(get_calendar=AsyncMock(side_effect=RuntimeError))
    ctx = _ctx(sonarr=_sonarr([_ep("Show", 1, TODAY)]), radarr=radarr)
    edit = await _tap(ctx)
    assert "Show" in edit["text"] and "Couldn't read Radarr" in edit["text"]


async def test_every_service_failing_says_so():
    sonarr = SimpleNamespace(get_calendar=AsyncMock(side_effect=RuntimeError))
    edit = await _tap(_ctx(sonarr=sonarr))
    assert "couldn't read the calendar" in edit["text"]


async def test_empty_week_says_nothing_is_coming():
    edit = await _tap(_ctx(sonarr=_sonarr([])))
    assert "Nothing on the calendar" in edit["text"]


async def test_pages_hold_ten_entries_with_prev_and_next():
    eps = [_ep(f"Show {i:02d}", i + 1, TODAY, 1, i) for i in range(WEEK_PAGE_SIZE + 3)]
    ctx = _ctx(sonarr=_sonarr(eps))
    first = await _tap(ctx)
    assert "Show 09" in first["text"] and "Show 10" not in first["text"]
    assert f"{REQUESTS_WEEK}:0:1" in _cbs(first)
    second = await _tap(ctx, f"{REQUESTS_WEEK}:0:1")
    assert "Show 10" in second["text"] and "Show 09" not in second["text"]
    assert f"{REQUESTS_WEEK}:0:0" in _cbs(second)


async def test_clients_ask_the_calendar_with_the_documented_params():
    import httpx
    from radarr import RadarrClient
    from sonarr import SonarrClient
    seen = []

    def handler(request):
        seen.append((request.url.path, dict(request.url.params)))
        return httpx.Response(200, json=[])

    for cls in (SonarrClient, RadarrClient):
        client = cls("http://arr.test", "key")
        client._client = httpx.AsyncClient(
            base_url="http://arr.test/api/v3",
            transport=httpx.MockTransport(handler))
        assert await client.get_calendar("2026-11-14", "2026-11-21") == []
    assert seen == [
        ("/api/v3/calendar", {"start": "2026-11-14", "end": "2026-11-21",
                              "includeSeries": "true"}),
        ("/api/v3/calendar", {"start": "2026-11-14", "end": "2026-11-21"})]


async def test_filter_matches_a_sonarr_v3_series_by_tvdb_id():
    ep = _ep("Old Sonarr", 0, TODAY)
    ep["series"] = {"title": "Old Sonarr", "tvdbId": 77}
    ctx = _ctx(sonarr=_sonarr([ep, _ep("Other", 2, TODAY)]),
               follows=[(5, 77)])
    edit = await _tap(ctx, f"{REQUESTS_WEEK}:1:0")
    assert "Old Sonarr" in edit["text"] and "Other" not in edit["text"]
