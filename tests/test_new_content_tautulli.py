"""The new-content poller's Tautulli source: recently added items land once,
items without a TMDb id are skipped, and the source switches with the link."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.new_content import poll_new_content

from tests._handler_harness import make_ctx

ADMIN_ID = 999
MARK = "1700000000"


def _item(key, added, **kw):
    return {"rating_key": key, "added_at": str(added), **kw}


def _movie(key, added, guids, title="Fight Club"):
    return _item(key, added, media_type="movie", title=title, guids=guids)


def _episode(key, added, season, number, show_key=7):
    return _item(key, added, media_type="episode", parent_media_index=season,
                 media_index=number, grandparent_rating_key=show_key,
                 grandparent_title="Breaking Bad", guids=["tvdb://999"])


def _tautulli(items, show_guids=("tmdb://1396", "tvdb://81189")):
    return SimpleNamespace(
        get_recently_added=AsyncMock(return_value=items),
        get_metadata=AsyncMock(return_value={"guids": list(show_guids)}))


def _arr():
    return SimpleNamespace(
        get_history_imports_since=AsyncMock(return_value=[]),
        get_series_ids=AsyncMock(return_value=None),
        get_episode_numbers=AsyncMock(return_value=None))


def _ctx(store, *, tautulli=None, radarr=None, sonarr=None):
    ctx = make_ctx(admin_id=ADMIN_ID)
    bd = ctx.application.bot_data
    bd.update(store=store, tautulli=tautulli, radarr=radarr, sonarr=sonarr)
    bd["settings_store"].settings.tg_notify_follows = True
    return ctx


async def _rows(store):
    return await store.list_new_content_between("2000-01-01 00:00:00",
                                                "2100-01-01 00:00:00")


async def test_recent_movies_and_episodes_land_once(fresh_store):
    await fresh_store.set_poller_mark("tautulli_added", MARK)
    tau = _tautulli([
        _movie(1, 1700000500, ["tmdb://550"]),
        _episode(2, 1700000600, 1, 1), _episode(3, 1700000700, 1, 2),
        _movie(4, 1699999000, ["tmdb://680"], "Old")])  # before the mark
    ctx = _ctx(fresh_store, tautulli=tau)
    await poll_new_content(ctx)
    await poll_new_content(ctx)  # the same items come back
    rows = await _rows(fresh_store)
    assert sorted(rows) == sorted([
        ("movie", 550, "Fight Club", None, None),
        ("episode", 1396, "Breaking Bad", 1, 1),
        ("episode", 1396, "Breaking Bad", 1, 2)])
    assert await fresh_store.get_poller_mark("tautulli_added") == "1700000700"
    tau.get_metadata.assert_awaited_once_with(7)  # the show, once


async def test_items_without_a_tmdb_id_are_skipped(fresh_store):
    await fresh_store.set_poller_mark("tautulli_added", MARK)
    tau = _tautulli([_movie(1, 1700000500, ["imdb://tt0137523"]),
                     _movie(2, 1700000600, ["tvdb://5"]),
                     _episode(3, 1700000700, 1, 1)], show_guids=("imdb://x",))
    await poll_new_content(_ctx(fresh_store, tautulli=tau))
    assert await _rows(fresh_store) == []


async def test_first_run_sets_the_mark_and_records_nothing(fresh_store):
    tau = _tautulli([_movie(1, 1700000500, ["tmdb://550"])])
    await poll_new_content(_ctx(fresh_store, tautulli=tau))
    assert await fresh_store.get_poller_mark("tautulli_added")
    tau.get_recently_added.assert_not_awaited()


async def test_a_failing_read_keeps_the_mark(fresh_store):
    await fresh_store.set_poller_mark("tautulli_added", MARK)
    tau = _tautulli([])
    tau.get_recently_added.side_effect = RuntimeError("down")
    await poll_new_content(_ctx(fresh_store, tautulli=tau))
    assert await fresh_store.get_poller_mark("tautulli_added") == MARK


async def test_with_tautulli_linked_the_arr_history_is_not_read(fresh_store):
    await fresh_store.set_poller_mark("radarr_history", "2026-11-14T09:00:00Z")
    await fresh_store.set_poller_mark("sonarr_history", "2026-11-14T09:00:00Z")
    radarr, sonarr = _arr(), _arr()
    await poll_new_content(_ctx(fresh_store, tautulli=_tautulli([]),
                                radarr=radarr, sonarr=sonarr))
    radarr.get_history_imports_since.assert_not_awaited()
    sonarr.get_history_imports_since.assert_not_awaited()


async def test_unlinking_switches_to_arr_without_rerecording(fresh_store):
    await fresh_store.set_poller_mark("tautulli_added", MARK)
    await fresh_store.set_poller_mark("radarr_history", "2026-11-14T09:00:00Z")
    tau = _tautulli([_movie(1, 1700000500, ["tmdb://550"])])
    await poll_new_content(_ctx(fresh_store, tautulli=tau))
    # Radarr's history holds the same movie from before; its mark moved on
    # while Tautulli was linked, so it asks only for what is newer.
    radarr = SimpleNamespace(get_history_imports_since=AsyncMock(
        return_value=[{"id": 9, "date": "2026-11-14T10:00:00Z",
                       "movie": {"tmdbId": 550, "title": "Fight Club"}}]))
    await poll_new_content(_ctx(fresh_store, radarr=radarr))
    since = radarr.get_history_imports_since.call_args.args[0]
    assert since > "2026-11-14T09:00:00Z"
    assert len(await _rows(fresh_store)) == 1  # the same movie, once


async def test_without_tautulli_the_arr_history_is_read_as_before(fresh_store):
    await fresh_store.set_poller_mark("radarr_history", "2026-11-14T09:00:00Z")
    radarr = SimpleNamespace(get_history_imports_since=AsyncMock(
        return_value=[{"id": 9, "date": "2026-11-14T10:00:00Z",
                       "movie": {"tmdbId": 550, "title": "Fight Club"}}]))
    await poll_new_content(_ctx(fresh_store, radarr=radarr))
    assert [r[1] for r in await _rows(fresh_store)] == [550]
