"""Request remaining seasons: the button on a show's detail card and
availability notice (only when a season is left to request), the confirm that
names those seasons, and the send that asks Seerr for "all" as the user."""
from __future__ import annotations

from seerr import (
    REQUEST_STATUS_APPROVED,
    REQUEST_STATUS_PENDING,
    CreatedRequest,
    MediaResult,
    QuotaExceededError,
    SeasonAvailability,
    NothingToRequestError,
)
from bot.media_card import card_keyboard, send_detail_card
from bot.request_flow import remaining_seasons_ask, remaining_seasons_send
from bot.subscriptions import fan_out_availability

from tests._handler_harness import make_ctx, make_link, make_update

USER_ID = 42
ADMIN_ID = 999
SHOW = 1396


def _seasons(*pairs):
    return [SeasonAvailability(n, ok) for n, ok in pairs]


def _cbs(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _linked_ctx(seasons=None):
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID))
    ctx.bot_data["seerr"].get_tv_season_availability.return_value = (
        seasons if seasons is not None
        else _seasons((1, False), (2, True), (3, True)))
    return ctx


# --- The button -----------------------------------------------------------------

def test_card_keyboard_remaining_button_only_for_shows_when_asked():
    show = MediaResult("tv", SHOW, "T", "2008", None)
    movie = MediaResult("movie", 5, "T", "2008", None)
    assert f"rqrem:{SHOW}" in _cbs(card_keyboard(show, False, True))
    assert f"rqrem:{SHOW}" not in _cbs(card_keyboard(show, False, False))
    assert not any(c.startswith("rqrem") for c in _cbs(card_keyboard(movie, None, True)))


async def test_detail_card_shows_button_when_a_season_is_requestable():
    show = MediaResult("tv", SHOW, "T", "2008", None)
    ctx = _linked_ctx()
    await send_detail_card(ctx, USER_ID, show)
    kb = ctx.bot.send_message.call_args.kwargs["reply_markup"]
    assert f"rqrem:{SHOW}" in _cbs(kb)


async def test_detail_card_has_no_button_when_every_season_is_covered():
    show = MediaResult("tv", SHOW, "T", "2008", None)
    ctx = _linked_ctx(_seasons((1, False), (2, False)))
    await send_detail_card(ctx, USER_ID, show)
    kb = ctx.bot.send_message.call_args.kwargs["reply_markup"]
    assert not any(c.startswith("rqrem") for c in _cbs(kb))


async def test_detail_card_has_no_button_when_the_lookup_fails():
    show = MediaResult("tv", SHOW, "T", "2008", None)
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].get_tv_season_availability.side_effect = RuntimeError("down")
    await send_detail_card(ctx, USER_ID, show)
    kb = ctx.bot.send_message.call_args.kwargs["reply_markup"]
    assert not any(c.startswith("rqrem") for c in _cbs(kb))


async def test_detail_card_has_no_button_for_an_unlinked_viewer():
    show = MediaResult("tv", SHOW, "T", "2008", None)
    ctx = make_ctx(admin_id=ADMIN_ID, link=None)
    ctx.bot_data["seerr"].get_tv_season_availability.return_value = _seasons((1, True))
    await send_detail_card(ctx, USER_ID, show)
    kb = ctx.bot.send_message.call_args.kwargs["reply_markup"]
    assert not any(c.startswith("rqrem") for c in _cbs(kb))


async def test_availability_notice_shows_button_when_a_season_is_requestable():
    app = _linked_ctx().application
    app.bot_data["store"].pop_subscribers.return_value = [USER_ID]
    await fan_out_availability(app, "tv", SHOW, "📺 Show", set())
    kb = app.bot.send_message.call_args.kwargs["reply_markup"]
    assert f"rqrem:{SHOW}" in _cbs(kb)


async def test_availability_notice_has_no_button_when_nothing_is_left():
    ctx = _linked_ctx(_seasons((1, False)))
    app = ctx.application
    app.bot_data["store"].pop_subscribers.return_value = [USER_ID]
    await fan_out_availability(app, "tv", SHOW, "📺 Show", set())
    kb = app.bot.send_message.call_args.kwargs["reply_markup"]
    assert not any(c.startswith("rqrem") for c in _cbs(kb))


# --- The confirm ----------------------------------------------------------------

async def test_confirm_lists_the_requestable_seasons():
    ctx = _linked_ctx(_seasons((1, False), (2, True), (3, True), (5, True)))
    ctx.bot_data["seerr"].get_media_title.return_value = ("Show", "2008")
    upd = make_update(callback_data=f"rqrem:{SHOW}", user_id=USER_ID)
    await remaining_seasons_ask(upd, ctx)
    sent = upd.callback_query.message.reply_calls[-1]
    assert "Show (2008)" in sent["text"]
    assert "S2-S3, S5" in sent["text"]
    assert f"rqrgo:{SHOW}" in _cbs(sent["reply_markup"])
    ctx.bot_data["seerr"].create_request.assert_not_awaited()


async def test_confirm_says_nothing_left_when_every_season_is_covered():
    ctx = _linked_ctx(_seasons((1, False)))
    upd = make_update(callback_data=f"rqrem:{SHOW}", user_id=USER_ID)
    await remaining_seasons_ask(upd, ctx)
    assert "Nothing left to request" in upd.callback_query.message.reply_calls[-1]["text"]


async def test_confirm_needs_a_link():
    ctx = make_ctx(admin_id=ADMIN_ID, link=None)
    upd = make_update(callback_data=f"rqrem:{SHOW}", user_id=USER_ID)
    await remaining_seasons_ask(upd, ctx)
    assert "/link" in upd.callback_query.message.reply_calls[-1]["text"]
    ctx.bot_data["seerr"].get_tv_season_availability.assert_not_awaited()


# --- The send -------------------------------------------------------------------

async def test_send_posts_all_seasons_as_the_user():
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].create_request.return_value = CreatedRequest(
        id=3, status=REQUEST_STATUS_APPROVED, seasons=[2, 3])
    upd = make_update(callback_data=f"rqrgo:{SHOW}", user_id=USER_ID)
    await remaining_seasons_send(upd, ctx)
    kwargs = ctx.bot_data["seerr"].create_request.call_args.kwargs
    assert kwargs["seasons"] == "all"
    assert kwargs["media_type"] == "tv" and kwargs["tmdb_id"] == SHOW
    assert kwargs["as_plex_token"] == "plex-abc"
    assert "S2-S3" in upd.callback_query.edits[-1]["text"]
    assert "Approved" in upd.callback_query.edits[-1]["text"]


async def test_send_pending_request_says_waiting():
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].create_request.return_value = CreatedRequest(
        id=3, status=REQUEST_STATUS_PENDING, seasons=[2])
    upd = make_update(callback_data=f"rqrgo:{SHOW}", user_id=USER_ID)
    await remaining_seasons_send(upd, ctx)
    assert "Waiting for approval" in upd.callback_query.edits[-1]["text"]


async def test_send_202_says_nothing_left_to_request():
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].create_request.side_effect = NothingToRequestError()
    upd = make_update(callback_data=f"rqrgo:{SHOW}", user_id=USER_ID)
    await remaining_seasons_send(upd, ctx)
    assert upd.callback_query.edits[-1]["text"] == "Nothing left to request 🎉"


async def test_send_quota_error_reuses_the_request_flow_message():
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].create_request.side_effect = QuotaExceededError(
        "Series Quota exceeded.")
    upd = make_update(callback_data=f"rqrgo:{SHOW}", user_id=USER_ID)
    await remaining_seasons_send(upd, ctx)
    text = upd.callback_query.edits[-1]["text"]
    assert "Series Quota exceeded." in text
    assert "Your quota frees up as older requests age out of the window." in text


async def test_send_double_tap_files_one_request():
    ctx = _linked_ctx()
    ctx.user_data["rq_remaining_busy"] = {SHOW}
    upd = make_update(callback_data=f"rqrgo:{SHOW}", user_id=USER_ID)
    await remaining_seasons_send(upd, ctx)
    ctx.bot_data["seerr"].create_request.assert_not_awaited()


async def test_send_permission_error_reuses_the_request_flow_message():
    from seerr import RequestPermissionError
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].create_request.side_effect = RequestPermissionError("No.")
    upd = make_update(callback_data=f"rqrgo:{SHOW}", user_id=USER_ID)
    await remaining_seasons_send(upd, ctx)
    assert upd.callback_query.edits[-1]["text"] == "🚫 Seerr said no: No."


async def test_send_unlinked_user_is_told_to_link():
    ctx = make_ctx(admin_id=ADMIN_ID, link=None)
    upd = make_update(callback_data=f"rqrgo:{SHOW}", user_id=USER_ID)
    await remaining_seasons_send(upd, ctx)
    assert "/link" in upd.callback_query.edits[-1]["text"]
    ctx.bot_data["seerr"].create_request.assert_not_awaited()
