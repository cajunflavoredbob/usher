"""/mystats and the Trending list's "My stats" button, with a fake Tautulli."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.discover_cmd import cmd_trending, trending_hub
from bot.mystats_cmd import (NOT_LINKED_MSG, UNKNOWN_USER_MSG, cmd_mystats,
                             format_watch_time, mystats_button)
from bot.requests_hub import REQUESTS_HUB

from tests._handler_harness import make_ctx, make_link, make_update

USER_ID = 42


def _tautulli(users=None):
    users = [{"user_id": 9, "username": "User1Plex"}] if users is None else users
    return SimpleNamespace(
        get_users=AsyncMock(return_value=users),
        get_user_watch_time_stats=AsyncMock(return_value=[
            {"query_days": 7, "total_plays": 3, "total_time": 7200},
            {"query_days": 30, "total_plays": 1, "total_time": 90000},
        ]),
        get_home_stats=AsyncMock(return_value=[
            {"stat_id": "top_movies", "rows": [
                {"title": "Big Movie", "total_plays": 2}]},
            {"stat_id": "top_tv", "rows": [
                {"title": "Long Show", "total_plays": 1}]},
        ]),
    )


def _ctx(tau, link="default"):
    link = make_link(telegram_id=USER_ID) if link == "default" else link
    return make_ctx(link=link, bot_data_overrides={"tautulli": tau})


def test_format_watch_time():
    assert format_watch_time(7200) == "2h 0m"
    assert format_watch_time(90000) == "1d 1h"
    assert format_watch_time(300) == "5m"


async def test_mystats_shows_both_windows_and_top_titles():
    tau = _tautulli()
    ctx = _ctx(tau)
    upd = make_update(text="/mystats", user_id=USER_ID)
    await cmd_mystats(upd, ctx)
    text = upd.effective_message.reply_calls[-1]["text"]
    assert "Last 7 days: 3 plays, 2h 0m watched" in text
    assert "Last 30 days: 1 play, 1d 1h watched" in text
    assert "1. Big Movie (2 plays)" in text
    assert "1. Long Show (1 play)" in text
    assert tau.get_user_watch_time_stats.call_args.args[0] == 9
    assert tau.get_home_stats.call_args.kwargs["user_id"] == 9


async def test_unmapped_linked_user_told_tautulli_doesnt_know_them():
    ctx = _ctx(_tautulli(users=[{"user_id": 1, "username": "someoneelse"}]))
    upd = make_update(text="/mystats", user_id=USER_ID)
    await cmd_mystats(upd, ctx)
    assert upd.effective_message.reply_calls[-1]["text"] == UNKNOWN_USER_MSG


async def test_unlinked_user_told_to_link():
    tau = _tautulli()
    ctx = _ctx(tau, link=None)
    upd = make_update(text="/mystats", user_id=USER_ID)
    await cmd_mystats(upd, ctx)
    assert upd.effective_message.reply_calls[-1]["text"] == NOT_LINKED_MSG
    tau.get_users.assert_not_awaited()


async def test_tautulli_error_is_reported_not_raised():
    tau = _tautulli()
    tau.get_user_watch_time_stats.side_effect = RuntimeError("boom")
    ctx = _ctx(tau)
    upd = make_update(text="/mystats", user_id=USER_ID)
    await cmd_mystats(upd, ctx)
    assert "Couldn't fetch your stats" in upd.effective_message.reply_calls[-1]["text"]


async def test_top_titles_failure_says_so_and_hub_offers_retry():
    tau = _tautulli()
    tau.get_home_stats.side_effect = RuntimeError("boom")
    ctx = _ctx(tau)
    upd = make_update(callback_data="mys", user_id=USER_ID)
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    upd.callback_query.message.reply_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("b", callback_data=f"hubo:{REQUESTS_HUB}")]])
    await mystats_button(upd, ctx)
    edit = upd.callback_query.edits[-1]
    assert "Last 7 days" in edit["text"]
    assert "Couldn't load your most watched titles" in edit["text"]
    assert "No movies or shows" not in edit["text"]
    cbs = [b.callback_data for row in edit["reply_markup"].inline_keyboard
           for b in row]
    assert "mys" in cbs


async def _trending_callbacks(ctx):
    ctx.args = []
    ctx.bot_data["seerr"].discover.return_value = []
    from seerr import MediaResult
    ctx.bot_data["seerr"].discover.return_value = [
        MediaResult("movie", 100, "T", "2026", None)]
    upd = make_update(text="/trending", user_id=USER_ID)
    await cmd_trending(upd, ctx)
    markup = upd.effective_message.reply_calls[-1]["reply_markup"]
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def test_trending_shows_button_only_with_tautulli():
    assert "mys" in await _trending_callbacks(_ctx(_tautulli()))
    assert "mys" not in await _trending_callbacks(_ctx(None))


async def test_hub_trending_keeps_back_row_with_button():
    ctx = _ctx(_tautulli())
    from seerr import MediaResult
    ctx.bot_data["seerr"].discover.return_value = [
        MediaResult("movie", 100, "T", "2026", None)]
    upd = make_update(callback_data="rqht", user_id=USER_ID)
    await trending_hub(upd, ctx)
    markup = upd.callback_query.edits[-1]["reply_markup"]
    cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "mys" in cbs and f"hubo:{REQUESTS_HUB}" in cbs


async def test_button_from_hub_edits_in_place_with_back_row():
    ctx = _ctx(_tautulli())
    upd = make_update(callback_data="mys", user_id=USER_ID)
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    upd.callback_query.message.reply_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("b", callback_data=f"hubo:{REQUESTS_HUB}")]])
    await mystats_button(upd, ctx)
    edit = upd.callback_query.edits[-1]
    assert "Last 7 days" in edit["text"]
    cbs = [b.callback_data for row in edit["reply_markup"].inline_keyboard
           for b in row]
    assert cbs == [f"hubo:{REQUESTS_HUB}"]


async def test_button_outside_hub_sends_new_message():
    ctx = _ctx(_tautulli())
    upd = make_update(callback_data="mys", user_id=USER_ID)
    await mystats_button(upd, ctx)
    assert upd.callback_query.edits == []
    assert "Last 7 days" in upd.callback_query.message.reply_calls[-1]["text"]
