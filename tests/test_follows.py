"""Tests for Follows: the Follow / Unfollow taps, the store rules, the
new-content poller and its mark, the grouped Follow pings, the notice
keyboards and the Requests Hub's Follows list."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from seerr import MediaResult
from bot.follows import follow_add, follow_del, follow_remove, _render_follows
from bot.media_card import card_keyboard, send_detail_card
from bot.new_content import poll_new_content, send_follow_pings
from bot.requests_hub import render_requests_hub
from bot.subscriptions import fan_out_availability

from tests._handler_harness import make_ctx, make_link, make_update

USER_ID = 42
ADMIN_ID = 999
SHOW = 1396


def _store_ctx(store):
    """A handler ctx over a real store whose `get` says USER_ID is Linked."""
    store.get = AsyncMock(return_value=make_link(telegram_id=USER_ID))
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["store"] = store
    return ctx


def _cbs(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _btns(call):
    markup = call.kwargs.get("reply_markup")
    return [b for row in markup.inline_keyboard for b in row] if markup else []


# --- Follow / Unfollow taps ---------------------------------------------------

async def test_follow_tap_stores_one_row_then_shows_unfollow(fresh_store):
    ctx = _store_ctx(fresh_store)
    ctx.bot_data["seerr"].get_media_title.return_value = ("Show", "2008")
    upd = make_update(callback_data=f"folad:{SHOW}", user_id=USER_ID)
    await follow_add(upd, ctx)
    await follow_add(make_update(callback_data=f"folad:{SHOW}", user_id=USER_ID), ctx)
    rows = await fresh_store.list_follows(USER_ID)
    assert [(r[1], r[2]) for r in rows] == [(SHOW, "Show (2008)")]
    assert await fresh_store.is_following(USER_ID, SHOW)


async def test_follow_tap_swaps_the_button_to_unfollow(fresh_store):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    ctx = _store_ctx(fresh_store)
    upd = make_update(callback_data=f"folad:{SHOW}", user_id=USER_ID)
    upd.callback_query.message.reply_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Follow", callback_data=f"folad:{SHOW}")],
        [InlineKeyboardButton("🗑 Dismiss", callback_data="rqx")]])
    await follow_add(upd, ctx)
    assert _cbs(upd.callback_query.markup_edits[0]) == [f"foldl:{SHOW}", "rqx"]


async def test_unfollow_sets_removed_and_swaps_back(fresh_store):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    await fresh_store.add_follow(USER_ID, SHOW, 0, "Show")
    ctx = _store_ctx(fresh_store)
    upd = make_update(callback_data=f"foldl:{SHOW}", user_id=USER_ID)
    upd.callback_query.message.reply_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔕 Unfollow", callback_data=f"foldl:{SHOW}")]])
    await follow_del(upd, ctx)
    assert not await fresh_store.is_following(USER_ID, SHOW)
    assert _cbs(upd.callback_query.markup_edits[0]) == [f"folad:{SHOW}"]
    # The row stays, flagged removed, so an automatic follow can see it.
    assert await fresh_store.add_follow(USER_ID, SHOW, 0, "Show") is True


async def test_follow_gated_for_unlinked():
    ctx = make_ctx(admin_id=ADMIN_ID, link=None)
    upd = make_update(callback_data=f"folad:{SHOW}", user_id=USER_ID)
    await follow_add(upd, ctx)
    ctx.bot_data["store"].add_follow.assert_not_awaited()
    assert any(alert for _, alert in upd.callback_query.answers)


async def test_store_follow_is_unique_per_user_and_show(fresh_store):
    assert await fresh_store.add_follow(1, SHOW, 0, "A") is True
    assert await fresh_store.add_follow(1, SHOW, 0, "A") is False
    assert await fresh_store.add_follow(2, SHOW, 0, "A") is True
    assert sorted(await fresh_store.list_followers(SHOW)) == [1, 2]
    # A forged id belonging to someone else is a no-op.
    follow_id = (await fresh_store.list_follows(1))[0][0]
    assert await fresh_store.remove_follow_by_id(follow_id, 2) is False
    assert await fresh_store.remove_follow_by_id(follow_id, 1) is True
    assert await fresh_store.list_followers(SHOW) == [2]


# --- Detail card and notices --------------------------------------------------

def test_card_keyboard_follow_button_only_for_shows_when_allowed():
    show = MediaResult("tv", SHOW, "T", "2008", None)
    movie = MediaResult("movie", 5, "T", "2008", None)
    assert f"folad:{SHOW}" in _cbs(card_keyboard(show, False))
    assert f"foldl:{SHOW}" in _cbs(card_keyboard(show, True))
    assert not any(c.startswith("fol") for c in _cbs(card_keyboard(show, None)))
    assert not any(c.startswith("fol") for c in _cbs(card_keyboard(movie, False)))


async def test_detail_card_offers_follow_to_linked_user_only():
    show = MediaResult("tv", SHOW, "T", "2008", None)
    linked = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID))
    await send_detail_card(linked, USER_ID, show)
    kb = linked.bot.send_message.call_args.kwargs["reply_markup"]
    assert f"folad:{SHOW}" in _cbs(kb)
    stranger = make_ctx(admin_id=ADMIN_ID, link=None)
    await send_detail_card(stranger, USER_ID, show)
    kb = stranger.bot.send_message.call_args.kwargs["reply_markup"]
    assert not any(c.startswith("fol") for c in _cbs(kb))


async def test_show_availability_notice_has_watch_now_and_follow():
    app = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID)).application
    app.bot_data["seerr"].get_media_url.return_value = "https://plex.example/x"
    app.bot_data["store"].pop_subscribers.return_value = [USER_ID]
    await fan_out_availability(app, "tv", SHOW, "📺 Show", set())
    buttons = _btns(app.bot.send_message.call_args)
    assert [b.text for b in buttons] == ["▶️ Watch now", "➕ Follow"]


async def test_notice_shows_unfollow_when_already_following():
    app = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID)).application
    app.bot_data["store"].is_following.return_value = True
    app.bot_data["store"].pop_subscribers.return_value = [USER_ID]
    await fan_out_availability(app, "tv", SHOW, "📺 Show", set())
    assert [b.callback_data for b in _btns(app.bot.send_message.call_args)] == [
        f"foldl:{SHOW}"]


async def test_movie_notice_has_no_follow():
    app = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID)).application
    app.bot_data["store"].pop_subscribers.return_value = [USER_ID]
    await fan_out_availability(app, "movie", 5, "🎬 Movie", set())
    assert _btns(app.bot.send_message.call_args) == []


# --- Poller -------------------------------------------------------------------

def _sonarr(records):
    return SimpleNamespace(
        get_history_imports_since=AsyncMock(return_value=records),
        get_series_ids=AsyncMock(return_value={
            "tmdb_id": SHOW, "tvdb_id": 81189, "title": "Breaking Bad"}),
        get_episode_numbers=AsyncMock(return_value=None))


def _rec(rec_id, season, ep, date="2026-11-14T10:00:00Z"):
    return {"id": rec_id, "date": date, "seriesId": 3, "episodeId": rec_id,
            "episode": {"seasonNumber": season, "episodeNumber": ep}}


def _app(store, *, sonarr=None, radarr=None, follows=True):
    ctx = make_ctx(admin_id=ADMIN_ID)
    app = ctx.application
    app.bot_data["store"] = store
    app.bot_data["sonarr"] = sonarr
    app.bot_data["radarr"] = radarr
    app.bot_data["settings_store"].settings.tg_notify_follows = follows
    app.bot_data["seerr"].get_media_url.return_value = "https://plex.example/show"
    return ctx


async def test_first_poll_sets_the_mark_and_records_nothing(fresh_store):
    sonarr = _sonarr([_rec(1, 1, 1)])
    ctx = _app(fresh_store, sonarr=sonarr)
    await poll_new_content(ctx)
    assert await fresh_store.get_poller_mark("sonarr_history")
    sonarr.get_history_imports_since.assert_not_awaited()


async def test_poller_records_each_import_once_and_keeps_its_mark(fresh_store):
    await fresh_store.set_poller_mark("sonarr_history", "2026-11-14T09:00:00Z")
    await fresh_store.add_follow(USER_ID, SHOW, 0, "Breaking Bad")
    sonarr = _sonarr([_rec(1, 1, 1, "2026-11-14T10:00:00Z"),
                      _rec(2, 1, 2, "2026-11-14T10:05:00Z")])
    ctx = _app(fresh_store, sonarr=sonarr)
    await poll_new_content(ctx)
    await poll_new_content(ctx)  # the same records come back
    assert sonarr.get_history_imports_since.call_args.args == (
        "2026-11-14T10:05:00Z",)
    assert not await fresh_store.add_new_content(
        "sonarr:1", "episode", SHOW, "Breaking Bad", 1, 1)
    assert not await fresh_store.add_new_content(
        "sonarr:2", "episode", SHOW, "Breaking Bad", 1, 2)
    assert ctx.application.bot.send_message.await_count == 1


async def test_poller_records_radarr_movies(fresh_store):
    await fresh_store.set_poller_mark("radarr_history", "2026-11-14T09:00:00Z")
    radarr = SimpleNamespace(get_history_imports_since=AsyncMock(return_value=[
        {"id": 9, "date": "2026-11-14T10:00:00Z",
         "movie": {"tmdbId": 550, "title": "Fight Club"}}]))
    ctx = _app(fresh_store, radarr=radarr)
    await poll_new_content(ctx)
    await poll_new_content(ctx)
    assert not await fresh_store.add_new_content("radarr:9", "movie", 550, "x")
    assert await fresh_store.get_poller_mark("radarr_history") == (
        "2026-11-14T10:00:00Z")


async def test_a_failing_source_keeps_its_mark(fresh_store):
    await fresh_store.set_poller_mark("sonarr_history", "2026-11-14T09:00:00Z")
    sonarr = _sonarr([])
    sonarr.get_history_imports_since.side_effect = RuntimeError("down")
    await poll_new_content(_app(fresh_store, sonarr=sonarr))
    assert await fresh_store.get_poller_mark("sonarr_history") == (
        "2026-11-14T09:00:00Z")


# --- Follow pings -------------------------------------------------------------

async def _polled(store, *, follows=True, records=None):
    await store.set_poller_mark("sonarr_history", "2026-11-14T09:00:00Z")
    sonarr = _sonarr(records if records is not None
                     else [_rec(1, 1, 1), _rec(2, 1, 2)])
    ctx = _app(store, sonarr=sonarr, follows=follows)
    await poll_new_content(ctx)
    return ctx


async def test_two_imports_in_one_poll_send_one_ping_with_watch_now(fresh_store):
    await fresh_store.add_follow(USER_ID, SHOW, 0, "Breaking Bad")
    ctx = await _polled(fresh_store)
    send = ctx.application.bot.send_message
    assert send.await_count == 1
    call = send.call_args
    assert call.kwargs["chat_id"] == USER_ID
    assert "S01E01 and S01E02" in call.kwargs["text"]
    assert "Breaking Bad" in call.kwargs["text"]
    assert [b.text for b in _btns(call)] == ["▶️ Watch now", "🔕 Unfollow"]


async def test_an_episode_is_never_pinged_twice(fresh_store):
    await fresh_store.add_follow(USER_ID, SHOW, 0, "Breaking Bad")
    ctx = await _polled(fresh_store)
    await poll_new_content(ctx)
    await send_follow_pings(ctx.application)
    # A fresh app on the same store is a restart.
    await send_follow_pings(_app(fresh_store).application)
    assert ctx.application.bot.send_message.await_count == 1
    # The same episode imported again (an upgrade) is not a new episode.
    assert not await fresh_store.add_new_content(
        "sonarr:77", "episode", SHOW, "Breaking Bad", 1, 1)


async def test_a_later_episode_gets_its_own_ping(fresh_store):
    await fresh_store.add_follow(USER_ID, SHOW, 0, "Breaking Bad")
    ctx = await _polled(fresh_store, records=[_rec(1, 1, 1)])
    await fresh_store.add_new_content("sonarr:2", "episode", SHOW,
                                      "Breaking Bad", 1, 2)
    await send_follow_pings(ctx.application)
    texts = [c.kwargs["text"] for c in ctx.application.bot.send_message.call_args_list]
    assert len(texts) == 2 and "S01E02" in texts[1] and "S01E01" not in texts[1]


async def test_the_follow_pings_switch_off_sends_none(fresh_store):
    await fresh_store.add_follow(USER_ID, SHOW, 0, "Breaking Bad")
    ctx = await _polled(fresh_store, follows=False)
    assert ctx.application.bot.send_message.await_count == 0
    # Missed events are not replayed once the switch is turned back on.
    ctx.application.bot_data["settings_store"].settings.tg_notify_follows = True
    await send_follow_pings(ctx.application)
    assert ctx.application.bot.send_message.await_count == 0


async def test_only_followers_of_that_show_are_pinged(fresh_store):
    await fresh_store.add_follow(USER_ID, SHOW, 0, "Breaking Bad")
    await fresh_store.add_follow(7, 555, 0, "Other")
    await fresh_store.add_follow(8, SHOW, 0, "Breaking Bad")
    await fresh_store.remove_follow(8, SHOW)
    ctx = await _polled(fresh_store)
    chats = [c.kwargs["chat_id"] for c in ctx.application.bot.send_message.call_args_list]
    assert chats == [USER_ID]


# --- Requests Hub list --------------------------------------------------------

async def test_requests_hub_has_a_follows_button():
    view = await render_requests_hub(None, None)
    assert "rqhf" in [b.callback_data for b in view.buttons]


async def test_follows_list_unfollows_own_rows_only():
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID))
    store = ctx.bot_data["store"]
    store.list_follows.return_value = [(5, SHOW, "Breaking Bad")]
    upd = make_update(callback_data="folrm:5", user_id=USER_ID)
    await follow_remove(upd, ctx)
    store.remove_follow_by_id.assert_awaited_once_with(5, USER_ID)
    text = upd.callback_query.edits[0]["text"]
    assert "Breaking Bad" in text
    assert "foldl" not in str(upd.callback_query.edits[0]["reply_markup"])
    assert "folrm:5" in [b.callback_data for row in
                         upd.callback_query.edits[0]["reply_markup"].inline_keyboard
                         for b in row]


async def test_empty_follows_list_hint():
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID))
    upd = make_update(callback_data="rqhf", user_id=USER_ID)
    await _render_follows(upd, ctx, upd.callback_query.message.reply_text,
                          user_id=USER_ID)
    assert "aren't following" in upd.callback_query.message.reply_calls[0]["text"]
