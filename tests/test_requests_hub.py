"""Tests for the Requests Hub (/request with no title) and the flows its
buttons start: request conversation, My requests, Trending, Subscriptions, plus
the separate Progress card a successful submit sends."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from telegram.ext import ConversationHandler

from bot import hub
from bot.callback_prefixes import (HUB_OPEN, REQUESTS_MINE, REQUESTS_NEW,
                                   REQUESTS_SUBS, REQUESTS_TRENDING)
from bot.discover_cmd import cmd_trending, dv_page, trending_hub
from bot.request_flow import (_request_conversation, _request_timeout,
                              request_cancel, request_from_issue,
                              request_hub_new, request_start, request_submit,
                              request_title)
from bot.requests_cmd import cmd_requests, requests_hub_mine, rq_list_cancel
from bot.requests_hub import REQUESTS_HUB, render_requests_hub
from bot.subscriptions import (cmd_subscriptions, sub_del,
                               subscriptions_hub)
from bot.shared import RQ_CONFIRM, RQ_PICK_MEDIA, RQ_TITLE
from seerr import (REQUEST_STATUS_APPROVED, REQUEST_STATUS_PENDING,
                   CreatedRequest, MediaResult, QuotaExceededError,
                   RequestListItem)

from tests._handler_harness import make_ctx, make_mapping, make_update

USER_ID = 42
ADMIN_ID = 999
BACK = ("⬅️ Requests", f"{HUB_OPEN}:requests")
RETRY_NEW = ("🔁 Try again", REQUESTS_NEW)


def _flat(markup):
    return [(b.text, b.callback_data)
            for row in markup.inline_keyboard for b in row]


def _cbs(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _linked_ctx(**kwargs):
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=make_mapping(telegram_id=USER_ID),
                   **kwargs)
    ctx.args = []
    return ctx


def _hub_ctx(**kwargs):
    ctx = _linked_ctx(**kwargs)
    hub.set_hub_origin(ctx, REQUESTS_HUB)
    return ctx


def _from_hub(upd):
    """Mark the tapped message as carrying the Hub's back button."""
    upd.callback_query.message.reply_markup = hub.end_markup(REQUESTS_HUB)
    return upd


def _results(n=3):
    return [MediaResult("movie" if i % 2 == 0 else "tv", 100 + i,
                        f"Title {i + 1}", "2026", None)
            for i in range(n)]


def _items():
    return [
        RequestListItem(id=12, status=REQUEST_STATUS_PENDING,
                        media_type="movie", tmdb_id=550,
                        created_at="2026-08-16T10:00:00.000Z",
                        requested_by="user1"),
        RequestListItem(id=14, status=REQUEST_STATUS_PENDING,
                        media_type="movie", tmdb_id=551,
                        created_at="2026-08-16T09:00:00.000Z",
                        requested_by="user1"),
    ]


def _confirm_ctx(hub_name=None, media=None):
    ctx = _linked_ctx(user_data={
        "rq_media": media or {"type": "movie", "tmdb_id": 1,
                              "title": "New Movie", "year": "2026"},
        "rq_search_version": 1})
    if hub_name:
        hub.set_hub_origin(ctx, hub_name)
    ctx.bot.send_message.return_value = SimpleNamespace(message_id=777)
    return ctx


def _fake_conv(key=(100, USER_ID)):
    updates = []
    conv = SimpleNamespace(
        _get_key=lambda update: key,
        _conversations={key: 1},
        _update_state=lambda state, k: updates.append((state, k)),
        timeout_jobs={},
        updates=updates,
    )
    return conv


# --- the Hub itself ------------------------------------------------------------

async def test_request_without_title_shows_four_button_hub():
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=None)  # no gate for the Hub
    ctx.args = []
    upd = make_update(text="/request", user_id=USER_ID)
    state = await request_start(upd, ctx)
    assert state == ConversationHandler.END
    reply = upd.effective_message.reply_calls[0]
    assert reply["text"].startswith("🎬 <b>Requests</b>")
    assert reply["parse_mode"] == "HTML"
    assert _flat(reply["reply_markup"]) == [
        ("➕ New request", REQUESTS_NEW), ("📥 My requests", REQUESTS_MINE),
        ("🔥 Trending", REQUESTS_TRENDING), ("🔔 Subscriptions", REQUESTS_SUBS)]
    ctx.bot_data["seerr"].get_my_permissions.assert_not_awaited()


async def test_request_with_title_searches_without_hub():
    ctx = _linked_ctx()
    ctx.args = ["dune"]
    ctx.bot_data["seerr"].search.return_value = _results()
    upd = make_update(text="/request dune", user_id=USER_ID)
    state = await request_start(upd, ctx)
    assert state == RQ_PICK_MEDIA
    reply = upd.effective_message.reply_calls[0]
    assert reply["text"].startswith("Pick which one")
    assert HUB_OPEN not in " ".join(_cbs(reply["reply_markup"]))


async def test_requests_hub_rows_at_most_two():
    view = await render_requests_hub(None, None)
    rows = hub.hub_rows(view.buttons)
    assert [len(r) for r in rows] == [2, 2]


async def test_requests_back_button_shows_hub():
    ctx = _linked_ctx()
    upd = make_update(callback_data=BACK[1], user_id=USER_ID)
    await hub.hub_open_callback(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert edit["text"].startswith("🎬 <b>Requests</b>")
    assert [c for _, c in _flat(edit["reply_markup"])] == [
        REQUESTS_NEW, REQUESTS_MINE, REQUESTS_TRENDING, REQUESTS_SUBS]


# --- Hub buttons reach the same result as the typed commands -------------------

async def test_hub_new_request_edits_hub_into_title_prompt():
    ctx = _linked_ctx()
    upd = make_update(callback_data=REQUESTS_NEW, user_id=USER_ID)
    state = await request_hub_new(upd, ctx)
    assert state == RQ_TITLE
    assert hub.hub_origin(ctx) == REQUESTS_HUB
    edit = upd.callback_query.edits[0]
    assert edit["text"] == "What movie or show do you want? (Reply with the title.)"
    assert "reply_markup" not in edit
    assert upd.callback_query.message.reply_calls == []


async def test_hub_my_requests_matches_typed_list():
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].list_requests.return_value = (_items(), 2)
    typed = make_update(text="/requests", user_id=USER_ID)
    await cmd_requests(typed, ctx)
    typed_reply = typed.effective_message.reply_calls[-1]
    upd = make_update(callback_data=REQUESTS_MINE, user_id=USER_ID)
    await requests_hub_mine(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert upd.callback_query.message.reply_calls == []
    assert edit["text"] == typed_reply["text"]
    assert _flat(edit["reply_markup"])[:-1] == _flat(typed_reply["reply_markup"])


async def test_hub_trending_matches_typed_list():
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].discover.return_value = _results(7)
    typed = make_update(text="/trending", user_id=USER_ID)
    await cmd_trending(typed, ctx)
    typed_reply = typed.effective_message.reply_calls[-1]
    # A fresh context: each load bumps the browse version stamped on buttons.
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].discover.return_value = _results(7)
    upd = make_update(callback_data=REQUESTS_TRENDING, user_id=USER_ID)
    await trending_hub(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert upd.callback_query.message.reply_calls == []
    assert edit["text"] == typed_reply["text"]
    assert _flat(edit["reply_markup"])[:-1] == _flat(typed_reply["reply_markup"])


async def test_hub_subscriptions_matches_typed_list():
    ctx = _linked_ctx()
    ctx.bot_data["store"].list_subscriptions.return_value = [
        (1, "movie", 550, "Title A"), (2, "tv", 1399, "Title B")]
    typed = make_update(text="/subscriptions", user_id=USER_ID)
    await cmd_subscriptions(typed, ctx)
    typed_reply = typed.effective_message.reply_calls[-1]
    upd = make_update(callback_data=REQUESTS_SUBS, user_id=USER_ID)
    await subscriptions_hub(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert upd.callback_query.message.reply_calls == []
    assert edit["text"] == typed_reply["text"]
    assert _flat(edit["reply_markup"])[:-1] == _flat(typed_reply["reply_markup"])


# --- flows started from the Hub end with the back button -----------------------

async def test_hub_request_cancel_ends_with_back():
    ctx = _hub_ctx(user_data={"rq_search_version": 1})
    hub.set_hub_origin(ctx, REQUESTS_HUB)
    upd = make_update(callback_data="rqcancel:1", user_id=USER_ID)
    state = await request_cancel(upd, ctx)
    assert state == ConversationHandler.END
    edit = upd.callback_query.edits[0]
    assert edit["text"] == "Cancelled."
    assert _flat(edit["reply_markup"]) == [BACK]


async def test_hub_submit_ends_with_back():
    ctx = _confirm_ctx(REQUESTS_HUB)
    ctx.bot_data["seerr"].create_request.return_value = CreatedRequest(
        id=5, status=REQUEST_STATUS_PENDING)
    upd = make_update(callback_data="rqgo:1", user_id=USER_ID)
    state = await request_submit(upd, ctx)
    assert state == ConversationHandler.END
    edit = upd.callback_query.edits[-1]
    assert edit["text"].startswith("📨 Requested: <b>New Movie (2026)</b>.")
    assert _flat(edit["reply_markup"]) == [BACK]


async def test_hub_my_requests_list_ends_with_back():
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].list_requests.return_value = (_items(), 2)
    upd = make_update(callback_data=REQUESTS_MINE, user_id=USER_ID)
    await requests_hub_mine(upd, ctx)
    assert _flat(upd.callback_query.edits[0]["reply_markup"])[-1] == BACK
    ctx.bot_data["seerr"].list_requests.return_value = ([], 0)
    empty = make_update(callback_data=REQUESTS_MINE, user_id=USER_ID)
    await requests_hub_mine(empty, ctx)
    assert _flat(empty.callback_query.edits[0]["reply_markup"]) == [BACK]


async def test_hub_trending_list_ends_with_back():
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].discover.return_value = _results(7)
    upd = make_update(callback_data=REQUESTS_TRENDING, user_id=USER_ID)
    await trending_hub(upd, ctx)
    assert _flat(upd.callback_query.edits[0]["reply_markup"])[-1] == BACK
    ctx.bot_data["seerr"].discover.return_value = []
    empty = make_update(callback_data=REQUESTS_TRENDING, user_id=USER_ID)
    await trending_hub(empty, ctx)
    assert _flat(empty.callback_query.edits[0]["reply_markup"]) == [BACK]


async def test_hub_subscriptions_list_ends_with_back():
    ctx = _linked_ctx()
    ctx.bot_data["store"].list_subscriptions.return_value = [
        (1, "movie", 550, "Title A")]
    upd = make_update(callback_data=REQUESTS_SUBS, user_id=USER_ID)
    await subscriptions_hub(upd, ctx)
    assert _flat(upd.callback_query.edits[0]["reply_markup"])[-1] == BACK
    ctx.bot_data["store"].list_subscriptions.return_value = []
    empty = make_update(callback_data=REQUESTS_SUBS, user_id=USER_ID)
    await subscriptions_hub(empty, ctx)
    assert _flat(empty.callback_query.edits[0]["reply_markup"]) == [BACK]


# --- submit result and the separate Progress card ------------------------------

async def test_submit_result_has_back_and_progress_card_is_new_message():
    ctx = _confirm_ctx(REQUESTS_HUB)
    ctx.bot_data["seerr"].create_request.return_value = CreatedRequest(
        id=5, status=REQUEST_STATUS_APPROVED)
    upd = make_update(callback_data="rqgo:1", user_id=USER_ID)
    await request_submit(upd, ctx)
    edit = upd.callback_query.edits[-1]
    assert edit["text"] == ("✅ Approved: <b>New Movie (2026)</b> is being "
                            "grabbed.\nI'll DM you when it's available.")
    assert "/requests" not in edit["text"]
    assert edit["parse_mode"] == "HTML"
    assert _flat(edit["reply_markup"]) == [BACK]
    kwargs = ctx.bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == 100 and kwargs["parse_mode"] == "HTML"
    assert kwargs["text"] == ("<b>New Movie (2026)</b>\n✅ Approved — grabbing…"
                              "\n\nUse /requests to check on it.")
    watch = ctx.bot_data["store"].add_request_watch.call_args.kwargs
    assert watch["message_id"] == 777
    assert watch["message_id"] != upd.callback_query.message.message_id
    assert watch["chat_id"] == 100


async def test_typed_submit_watch_points_at_progress_card():
    ctx = _confirm_ctx()  # no Hub origin: the typed /request <title> path
    ctx.bot_data["seerr"].create_request.return_value = CreatedRequest(
        id=5, status=REQUEST_STATUS_PENDING)
    upd = make_update(callback_data="rqgo:1", user_id=USER_ID)
    await request_submit(upd, ctx)
    edit = upd.callback_query.edits[-1]
    assert edit["text"] == ("📨 Requested: <b>New Movie (2026)</b>.\n"
                            "Waiting for approval. I'll DM you when it moves.")
    assert _flat(edit["reply_markup"]) == [BACK]
    assert "⏳ Waiting for approval…" in ctx.bot.send_message.call_args.kwargs["text"]
    watch = ctx.bot_data["store"].add_request_watch.call_args.kwargs
    assert watch["message_id"] == 777
    assert watch["status"] == "waiting"


async def test_submit_card_send_failure_adds_no_watch():
    ctx = _confirm_ctx(REQUESTS_HUB)
    ctx.bot.send_message.side_effect = RuntimeError("telegram down")
    ctx.bot_data["seerr"].create_request.return_value = CreatedRequest(
        id=5, status=REQUEST_STATUS_APPROVED)
    upd = make_update(callback_data="rqgo:1", user_id=USER_ID)
    state = await request_submit(upd, ctx)
    assert state == ConversationHandler.END
    assert _flat(upd.callback_query.edits[-1]["reply_markup"]) == [BACK]
    ctx.bot_data["store"].add_request_watch.assert_not_awaited()


# --- menu, /start and registration ---------------------------------------------

def test_menu_drops_requests_trending_subscriptions():
    from bot.app import _ADMIN_BOT_COMMANDS, _BOT_COMMANDS
    for menu in (_BOT_COMMANDS, _ADMIN_BOT_COMMANDS):
        names = [c for c, _ in menu]
        assert names[0] == "account"
        for gone in ("requests", "trending", "subscriptions"):
            assert gone not in names
    assert ("request", "new request, your requests, trending, subscriptions") \
        in _BOT_COMMANDS


def test_typed_list_commands_still_registered():
    import re
    src = Path("bot/app.py").read_text(encoding="utf-8")
    registered = set(re.findall(r'CommandHandler\("([a-z]+)"', src))
    assert {"requests", "trending", "subscriptions"} <= registered


NEW_START_LINE = "  /request — new request, your requests, trending, subscriptions"
NEW_START_NOTE = ("(/link, /unlink, /requests, /trending and /subscriptions "
                  "still work when typed.)")


def _assert_start_text(text):
    assert NEW_START_LINE in text
    assert NEW_START_NOTE in text
    for old in ("/requests —", "/trending —", "/subscriptions —",
                "/request — request a movie or TV show"):
        assert old not in text


async def test_start_lists_request_hub_and_typed_note_for_user():
    from bot.app import cmd_start
    ctx = make_ctx(admin_id=ADMIN_ID)
    upd = make_update(text="/start", user_id=USER_ID)
    await cmd_start(upd, ctx)
    _assert_start_text(upd.effective_message.reply_calls[0]["text"])


async def test_start_lists_request_hub_and_typed_note_for_admin(monkeypatch):
    from unittest.mock import AsyncMock
    import bot.app as app_mod
    monkeypatch.setattr(app_mod, "_check_connections", AsyncMock(return_value={}))
    monkeypatch.setattr(app_mod, "format_status", lambda s: "ok")
    ctx = make_ctx(admin_id=ADMIN_ID)
    upd = make_update(text="/start", user_id=ADMIN_ID)
    await app_mod.cmd_start(upd, ctx)
    _assert_start_text(upd.effective_message.reply_calls[0]["text"])


# --- errors from the Hub carry Try again and back ------------------------------

async def test_hub_search_failure_offers_try_again_and_back():
    ctx = _hub_ctx()
    ctx.bot_data["seerr"].search.side_effect = RuntimeError("boom")
    upd = make_update(text="dune", user_id=USER_ID)
    state = await request_title(upd, ctx)
    assert state == ConversationHandler.END
    reply = upd.effective_message.reply_calls[0]
    assert reply["text"].startswith("Search failed.")
    assert _flat(reply["reply_markup"]) == [RETRY_NEW, BACK]


async def test_hub_submit_failure_offers_try_again_and_back():
    ctx = _confirm_ctx(REQUESTS_HUB)
    ctx.bot_data["seerr"].create_request.side_effect = RuntimeError("boom")
    upd = make_update(callback_data="rqgo:1", user_id=USER_ID)
    state = await request_submit(upd, ctx)
    assert state == ConversationHandler.END
    edit = upd.callback_query.edits[-1]
    assert edit["text"].startswith("Couldn't submit the request.")
    assert _flat(edit["reply_markup"]) == [RETRY_NEW, BACK]


async def test_hub_request_timeout_offers_try_again_and_back():
    ctx = _hub_ctx()
    upd = make_update(text="x", user_id=USER_ID)
    state = await _request_timeout(upd, ctx)
    assert state == ConversationHandler.END
    kwargs = ctx.bot.send_message.call_args.kwargs
    assert kwargs["text"] == "⏱️ The request timed out."
    assert _flat(kwargs["reply_markup"]) == [RETRY_NEW, BACK]


async def test_hub_my_requests_fetch_failure_offers_try_again_and_back():
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].list_requests.side_effect = RuntimeError("boom")
    upd = make_update(callback_data=REQUESTS_MINE, user_id=USER_ID)
    await requests_hub_mine(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert edit["text"].startswith("Couldn't fetch requests.")
    assert _flat(edit["reply_markup"]) == [("🔁 Try again", REQUESTS_MINE), BACK]


async def test_hub_trending_fetch_failure_offers_try_again_and_back():
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].discover.side_effect = RuntimeError("boom")
    upd = make_update(callback_data=REQUESTS_TRENDING, user_id=USER_ID)
    await trending_hub(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert edit["text"].startswith("Couldn't fetch the list.")
    assert _flat(edit["reply_markup"]) == [
        ("🔁 Try again", REQUESTS_TRENDING), BACK]


# --- refusals and guards carry back only ---------------------------------------

async def test_hub_submit_quota_ends_with_back_only():
    ctx = _confirm_ctx(REQUESTS_HUB)
    ctx.bot_data["seerr"].create_request.side_effect = QuotaExceededError(
        "Movie Quota exceeded.")
    upd = make_update(callback_data="rqgo:1", user_id=USER_ID)
    state = await request_submit(upd, ctx)
    assert state == ConversationHandler.END
    edit = upd.callback_query.edits[-1]
    assert "Movie Quota exceeded." in edit["text"]
    assert _flat(edit["reply_markup"]) == [BACK]


async def test_hub_new_request_unlinked_ends_with_back_only():
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=None)
    ctx.args = []
    upd = make_update(callback_data=REQUESTS_NEW, user_id=USER_ID)
    state = await request_hub_new(upd, ctx)
    assert state == ConversationHandler.END
    edit = upd.callback_query.edits[0]
    assert edit["text"].startswith("DM me /link first")
    assert _flat(edit["reply_markup"]) == [BACK]
    assert upd.callback_query.message.reply_calls == []


async def test_hub_buttons_without_seerr_keep_setup_text_with_back():
    for handler, data in ((request_hub_new, REQUESTS_NEW),
                          (requests_hub_mine, REQUESTS_MINE),
                          (trending_hub, REQUESTS_TRENDING),
                          (subscriptions_hub, REQUESTS_SUBS)):
        ctx = _linked_ctx()
        ctx.bot_data["seerr"] = None
        upd = make_update(callback_data=data, user_id=USER_ID)
        await handler(upd, ctx)
        reply = upd.callback_query.message.reply_calls[0]
        assert "isn't set up yet" in reply["text"]
        assert _flat(reply["reply_markup"]) == [BACK]


# --- typed paths keep today's ends ---------------------------------------------

async def test_typed_request_cancel_keeps_old_text_and_no_buttons():
    ctx = _linked_ctx()
    upd = make_update(text="/cancel", user_id=USER_ID)
    await request_cancel(upd, ctx)
    reply = upd.effective_message.reply_calls[0]
    assert reply["text"] == "Cancelled. /request to start over."
    assert "reply_markup" not in reply


async def test_typed_search_failure_has_no_buttons():
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].search.side_effect = RuntimeError("boom")
    upd = make_update(text="dune", user_id=USER_ID)
    await request_title(upd, ctx)
    reply = upd.effective_message.reply_calls[0]
    assert reply["text"].startswith("Search failed.")
    assert "reply_markup" not in reply


# --- origin --------------------------------------------------------------------

async def test_typed_request_with_title_clears_hub_origin():
    ctx = _hub_ctx()
    ctx.args = ["dune"]
    ctx.bot_data["seerr"].search.return_value = _results()
    upd = make_update(text="/request dune", user_id=USER_ID)
    await request_start(upd, ctx)
    assert hub.hub_origin(ctx) is None


async def test_trending_pick_from_hub_list_sets_origin():
    ctx = _linked_ctx()
    ctx.bot_data["flow_convs"] = []
    upd = _from_hub(make_update(callback_data="rqfi:movie:550", user_id=USER_ID))
    state = await request_from_issue(upd, ctx)
    assert state == RQ_CONFIRM
    assert hub.hub_origin(ctx) == REQUESTS_HUB


async def test_trending_pick_from_typed_list_clears_origin():
    ctx = _hub_ctx()
    ctx.bot_data["flow_convs"] = []
    upd = make_update(callback_data="rqfi:movie:550", user_id=USER_ID)
    state = await request_from_issue(upd, ctx)
    assert state == RQ_CONFIRM
    assert hub.hub_origin(ctx) is None


# --- re-renders keep the back row only when it was there -----------------------

async def test_hub_requests_cancel_rerender_keeps_back():
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].list_requests.return_value = (_items()[1:], 1)
    upd = _from_hub(make_update(callback_data="rqlc:12", user_id=USER_ID))
    await rq_list_cancel(upd, ctx)
    ctx.bot_data["seerr"].delete_request.assert_awaited_once()
    assert _flat(upd.callback_query.edits[0]["reply_markup"])[-1] == BACK


async def test_typed_requests_cancel_rerender_has_no_back():
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].list_requests.return_value = (_items()[1:], 1)
    upd = make_update(callback_data="rqlc:12", user_id=USER_ID)
    await rq_list_cancel(upd, ctx)
    assert HUB_OPEN not in " ".join(_cbs(upd.callback_query.edits[0]["reply_markup"]))


async def test_hub_trending_page_keeps_back():
    def ctx_with_list():
        return _linked_ctx(user_data={
            "dv_results": _results(7), "dv_cat": "trending",
            "dv_version": 1, "dv_screen": 0})
    upd = _from_hub(make_update(callback_data="dvp:1:1", user_id=USER_ID))
    await dv_page(upd, ctx_with_list())
    assert _flat(upd.callback_query.edits[0]["reply_markup"])[-1] == BACK
    typed = make_update(callback_data="dvp:1:1", user_id=USER_ID)
    await dv_page(typed, ctx_with_list())
    assert HUB_OPEN not in " ".join(_cbs(typed.callback_query.edits[0]["reply_markup"]))


async def test_hub_subscription_delete_keeps_back():
    ctx = _linked_ctx()
    ctx.bot_data["store"].list_subscriptions.return_value = [
        (2, "tv", 1399, "Title B")]
    upd = _from_hub(make_update(callback_data="subdel:1", user_id=USER_ID))
    await sub_del(upd, ctx)
    ctx.bot_data["store"].remove_subscription.assert_awaited_once_with(1, USER_ID)
    assert _flat(upd.callback_query.edits[0]["reply_markup"])[-1] == BACK
    typed = make_update(callback_data="subdel:1", user_id=USER_ID)
    await sub_del(typed, ctx)
    assert HUB_OPEN not in " ".join(_cbs(typed.callback_query.edits[0]["reply_markup"]))


# --- Hub buttons end other flows, and wiring -----------------------------------

async def test_hub_buttons_end_other_flows():
    for handler, data in ((request_hub_new, REQUESTS_NEW),
                          (requests_hub_mine, REQUESTS_MINE),
                          (trending_hub, REQUESTS_TRENDING),
                          (subscriptions_hub, REQUESTS_SUBS)):
        ctx = _linked_ctx()
        conv = _fake_conv()
        ctx.bot_data["flow_convs"] = [conv]
        ctx.user_data["link_active_loop"] = True
        await handler(make_update(callback_data=data, user_id=USER_ID), ctx)
        assert conv.updates == [(ConversationHandler.END, (100, USER_ID))]
        assert "link_active_loop" not in ctx.user_data


def test_request_hub_new_is_request_conversation_entry():
    conv = _request_conversation()
    patterns = {ep.pattern.pattern for ep in conv.entry_points
                if getattr(ep, "pattern", None) is not None}
    assert f"^{REQUESTS_NEW}$" in patterns


def test_request_hub_list_buttons_registered():
    src = Path("bot/app.py").read_text(encoding="utf-8")
    import re
    last = src.index("CallbackQueryHandler(unmatched_callback)")
    for handler, prefix in (("requests_hub_mine", "REQUESTS_MINE"),
                            ("trending_hub", "REQUESTS_TRENDING"),
                            ("subscriptions_hub", "REQUESTS_SUBS")):
        found = re.search(
            r'CallbackQueryHandler\(' + handler + r',\s*pattern=fr"\^\{'
            + prefix + r'\}\$"\)', src)
        assert found, handler
        assert found.start() < last
