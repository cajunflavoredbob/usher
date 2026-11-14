"""Automatic follows: shows watched twice or more in 30 days get one
automatic Follow and one message; fewer, a removed Follow, the switch off and
a re-run add nothing."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.auto_follow import run_auto_follow

from tests._handler_harness import make_ctx

ADMIN_ID = 999
USER = 42
SHOW_KEY = 7
TMDB = 1396


def _row(ep, show=SHOW_KEY, days_ago=1):
    return {"rating_key": ep, "grandparent_rating_key": show,
            "grandparent_title": "Breaking Bad",
            "date": int(time.time()) - days_ago * 86400}


def _tautulli(rows, guids=("tmdb://1396", "tvdb://81189")):
    return SimpleNamespace(
        get_users=AsyncMock(return_value=[
            {"user_id": 5, "username": "P42"}]),
        get_history=AsyncMock(return_value=rows),
        get_metadata=AsyncMock(return_value={"guids": list(guids)}))


async def _setup(store, rows, *, linked=True, tell=True, **kw):
    if linked:
        await store.link_with_plex(
            telegram_id=USER, seerr_id=1, seerr_display="U", plex_token="t",
            plex_uuid="u", plex_username="p42")
    ctx = make_ctx(admin_id=ADMIN_ID)
    bd = ctx.application.bot_data
    bd.update(store=store, tautulli=_tautulli(rows, **kw))
    bd["settings_store"].settings.tg_notify_follows = tell
    bd["seerr"].get_media_url.return_value = None
    return ctx.application


def _sends(app):
    return app.bot.send_message.call_args_list


async def test_three_episodes_give_one_follow_and_one_message(fresh_store):
    app = await _setup(fresh_store, [_row(1), _row(2), _row(3)])
    await run_auto_follow(app)
    follows = await fresh_store.list_follows(USER)
    assert [(f[1], f[2]) for f in follows] == [(TMDB, "Breaking Bad")]
    assert len(_sends(app)) == 1
    call = _sends(app)[0]
    assert call.kwargs["chat_id"] == USER
    cbs = [b.callback_data for r in call.kwargs["reply_markup"].inline_keyboard
           for b in r]
    assert cbs == [f"foldl:{TMDB}"]


async def test_one_episode_or_one_episode_replayed_does_not_follow(fresh_store):
    app = await _setup(fresh_store, [_row(1), _row(1)])
    await run_auto_follow(app)
    assert await fresh_store.list_follows(USER) == []
    assert _sends(app) == []


async def test_episodes_older_than_30_days_do_not_count(fresh_store):
    app = await _setup(fresh_store, [_row(1), _row(2, days_ago=40)])
    await run_auto_follow(app)
    assert await fresh_store.list_follows(USER) == []


async def test_a_removed_follow_is_not_added_back(fresh_store):
    app = await _setup(fresh_store, [_row(1), _row(2)])
    await fresh_store.add_follow(USER, TMDB, 0, "Breaking Bad")
    await fresh_store.remove_follow(USER, TMDB)
    await run_auto_follow(app)
    assert await fresh_store.list_follows(USER) == []
    assert _sends(app) == []


async def test_an_existing_follow_is_left_alone_and_silent(fresh_store):
    app = await _setup(fresh_store, [_row(1), _row(2)])
    await fresh_store.add_follow(USER, TMDB, 0, "Breaking Bad")
    await run_auto_follow(app)
    assert len(await fresh_store.list_follows(USER)) == 1
    assert _sends(app) == []


async def test_the_switch_off_adds_none(fresh_store):
    app = await _setup(fresh_store, [_row(1), _row(2)])
    await fresh_store.set_pref(USER, "auto_follow", False)
    await run_auto_follow(app)
    assert await fresh_store.list_follows(USER) == []
    assert _sends(app) == []


async def test_a_rerun_adds_and_sends_nothing_new(fresh_store):
    app = await _setup(fresh_store, [_row(1), _row(2)])
    await run_auto_follow(app)
    await run_auto_follow(app)
    assert len(await fresh_store.list_follows(USER)) == 1
    assert len(_sends(app)) == 1


async def test_follow_pings_switch_off_follows_silently(fresh_store):
    app = await _setup(fresh_store, [_row(1), _row(2)], tell=False)
    await run_auto_follow(app)
    assert len(await fresh_store.list_follows(USER)) == 1
    assert _sends(app) == []


async def test_a_user_tautulli_does_not_know_or_a_show_without_tmdb(fresh_store):
    app = await _setup(fresh_store, [_row(1), _row(2)], guids=("tvdb://1",))
    await run_auto_follow(app)
    assert await fresh_store.list_follows(USER) == []
    app.bot_data["tautulli"].get_users.return_value = []
    await run_auto_follow(app)
    assert await fresh_store.list_follows(USER) == []


async def test_without_tautulli_nothing_runs(fresh_store):
    app = await _setup(fresh_store, [_row(1), _row(2)])
    app.bot_data["tautulli"] = None
    await run_auto_follow(app)
    assert await fresh_store.list_follows(USER) == []
