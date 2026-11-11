"""Tests for the Tickets Hub (/tickets): Report a problem (the /issue flow
started from the Hub), the ticket list, and the detail card opened from it."""
from __future__ import annotations

import re
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ConversationHandler

from bot import hub
from bot.callback_prefixes import (HUB_OPEN, TICKETS_LIST, TICKETS_REPORT,
                                   TK_BACK, TK_BACK_LIST, TK_OPEN)
from bot.issue_flow import (_issue_conversation, _issue_timeout,
                            _submit_issue, issue_cancel, issue_hub_report,
                            issue_start, issue_title)
from bot.shared import TITLE
from bot.tickets import (_apply_fix, _tk_reply_timeout, cmd_tickets,
                         tickets_hub_list, tk_back, tk_close_direct,
                         tk_close_menu, tk_close_with_comment_start, tk_fix,
                         tk_open, tk_reply_cancel, tk_reply_start,
                         tk_reply_text)
from bot.tickets_hub import TICKETS_HUB, render_tickets_hub
from fix_result import FixResult
from seerr import CreatedIssue, IssueListItem

from tests._handler_harness import make_ctx, make_mapping, make_update

USER_ID = 42
ADMIN_ID = 999
BACK = ("⬅️ Tickets", f"{HUB_OPEN}:tickets")
LIST_BACK = ("⬅️ Tickets", TICKETS_LIST)
RETRY_REPORT = ("🔁 Try again", TICKETS_REPORT)
FIX_FAILED = FixResult.failed("No grab to mark failed.")


def _flat(markup):
    return [(b.text, b.callback_data)
            for row in markup.inline_keyboard for b in row]


def _issue(id: int = 1, **kwargs) -> IssueListItem:
    defaults = dict(
        id=id, issue_type=1, status=1, created_at="2026-05-29T12:00:00Z",
        tmdb_id=12345, media_type="movie", problem_season=None,
        problem_episode=None, created_by="someone",
    )
    defaults.update(kwargs)
    return IssueListItem(**defaults)


def _linked_ctx(**kwargs):
    return make_ctx(admin_id=ADMIN_ID,
                    mapping=make_mapping(telegram_id=USER_ID), **kwargs)


def _hub_ctx(**kwargs):
    ctx = _linked_ctx(**kwargs)
    hub.set_hub_origin(ctx, TICKETS_HUB)
    return ctx


def _from_hub(upd):
    """Mark the tapped message as the Hub's list (it carries the back button)."""
    upd.callback_query.message.reply_markup = hub.end_markup(TICKETS_HUB)
    return upd


def _from_list_detail(upd):
    """Mark the tapped message as a detail card opened from the list."""
    upd.callback_query.message.reply_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("💬 Reply", callback_data="tkr:5")],
         [InlineKeyboardButton("⬅️ Tickets", callback_data=TICKETS_LIST)]])
    return upd


def _submit_ctx(hub_name=None):
    ctx = _linked_ctx(user_data={
        "media": {"type": "movie", "tmdb_id": 1, "seerr_media_id": 10,
                  "title": "Some Movie", "year": "2026"},
        "issue_type": 1, "description": "no sound"})
    if hub_name:
        hub.set_hub_origin(ctx, hub_name)
    ctx.bot_data["seerr"].create_issue = _async_return(
        CreatedIssue(id=77, url="http://seerr.example/issues/77"))
    return ctx


def _async_return(value=None, exc=None):
    from unittest.mock import AsyncMock
    if exc is not None:
        return AsyncMock(side_effect=exc)
    return AsyncMock(return_value=value)


# --- the Hub itself ------------------------------------------------------------

async def test_tickets_shows_two_button_hub():
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=None)  # no gate for the Hub
    upd = make_update(text="/tickets", user_id=USER_ID)
    await cmd_tickets(upd, ctx)
    reply = upd.effective_message.reply_calls[0]
    assert reply["text"].startswith("🎫 <b>Tickets</b>")
    assert reply["parse_mode"] == "HTML"
    assert _flat(reply["reply_markup"]) == [
        ("📝 Report a problem", TICKETS_REPORT), ("📋 My tickets", TICKETS_LIST)]
    assert [len(r) for r in reply["reply_markup"].inline_keyboard] == [2]
    ctx.bot_data["seerr"].list_issues.assert_not_awaited()


async def test_admin_hub_says_open_tickets():
    ctx = make_ctx(admin_id=ADMIN_ID)
    upd = make_update(text="/tickets", user_id=ADMIN_ID)
    await cmd_tickets(upd, ctx)
    reply = upd.effective_message.reply_calls[0]
    assert _flat(reply["reply_markup"]) == [
        ("📝 Report a problem", TICKETS_REPORT), ("📋 Open tickets", TICKETS_LIST)]


async def test_hub_and_back_labels_never_say_issue():
    for uid in (USER_ID, ADMIN_ID):
        ctx = make_ctx(admin_id=ADMIN_ID)
        view = await render_tickets_hub(make_update(text="/tickets", user_id=uid), ctx)
        for b in view.buttons:
            assert "issue" not in b.text.lower()
    assert "issue" not in hub.back_button(TICKETS_HUB).text.lower()
    from bot.tickets_hub import list_back_button
    assert "issue" not in list_back_button().text.lower()


async def test_hub_report_season_fetch_failure_offers_try_again_and_back():
    from bot.issue_flow import issue_pick_media
    from seerr import MediaResult
    ctx = _hub_ctx(user_data={
        "search_results": {"version": 1, "by_key": {
            ("tv", 9): MediaResult("tv", 9, "Show", "2026", 55)}}})
    hub.set_hub_origin(ctx, TICKETS_HUB)
    ctx.bot_data["seerr"].get_tv_seasons.side_effect = RuntimeError("boom")
    upd = make_update(callback_data="media:1:tv:9", user_id=USER_ID)
    state = await issue_pick_media(upd, ctx)
    assert state == ConversationHandler.END
    edit = upd.callback_query.edits[0]
    assert edit["text"].startswith("Couldn't fetch seasons.")
    assert _flat(edit["reply_markup"]) == [RETRY_REPORT, BACK]


async def test_tickets_back_button_shows_hub():
    ctx = make_ctx(admin_id=ADMIN_ID)
    upd = make_update(callback_data=f"{HUB_OPEN}:tickets", user_id=USER_ID)
    await hub.hub_open_callback(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert edit["text"].startswith("🎫 <b>Tickets</b>")
    assert _flat(edit["reply_markup"])[0] == ("📝 Report a problem", TICKETS_REPORT)


# --- Report a problem ------------------------------------------------------------

async def test_hub_report_edits_hub_into_title_prompt():
    ctx = _linked_ctx()
    upd = make_update(callback_data=TICKETS_REPORT, user_id=USER_ID)
    state = await issue_hub_report(upd, ctx)
    assert state == TITLE
    assert hub.hub_origin(ctx) == TICKETS_HUB
    edit = upd.callback_query.edits[0]
    assert edit["text"] == "What movie or show is the issue with? (Reply with the title.)"
    assert upd.callback_query.message.reply_calls == []


async def test_typed_issue_still_prompts_for_title():
    ctx = _linked_ctx()
    upd = make_update(text="/issue", user_id=USER_ID)
    state = await issue_start(upd, ctx)
    assert state == TITLE
    reply = upd.effective_message.reply_calls[0]
    assert reply["text"] == "What movie or show is the issue with? (Reply with the title.)"
    assert "reply_markup" not in reply


async def test_typed_issue_clears_hub_origin():
    ctx = _hub_ctx()
    await issue_start(make_update(text="/issue", user_id=USER_ID), ctx)
    assert hub.hub_origin(ctx) is None


async def test_hub_report_unlinked_ends_with_back_only():
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=None)
    upd = make_update(callback_data=TICKETS_REPORT, user_id=USER_ID)
    state = await issue_hub_report(upd, ctx)
    assert state == ConversationHandler.END
    edit = upd.callback_query.edits[0]
    assert edit["text"].startswith("DM me /link first")
    assert _flat(edit["reply_markup"]) == [BACK]


async def test_hub_report_without_seerr_keeps_setup_text_with_back():
    ctx = _linked_ctx(bot_data_overrides={"seerr": None})
    upd = make_update(callback_data=TICKETS_REPORT, user_id=USER_ID)
    state = await issue_hub_report(upd, ctx)
    assert state == ConversationHandler.END
    reply = upd.callback_query.message.reply_calls[0]
    assert "isn't set up yet" in reply["text"]
    assert _flat(reply["reply_markup"]) == [BACK]


async def test_hub_report_search_failure_offers_try_again_and_back():
    ctx = _hub_ctx()
    ctx.bot_data["seerr"].search.side_effect = RuntimeError("boom")
    upd = make_update(text="dune", user_id=USER_ID)
    state = await issue_title(upd, ctx)
    assert state == ConversationHandler.END
    reply = upd.effective_message.reply_calls[0]
    assert reply["text"].startswith("Search failed.")
    assert _flat(reply["reply_markup"]) == [RETRY_REPORT, BACK]


async def test_typed_issue_search_failure_has_no_buttons():
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].search.side_effect = RuntimeError("boom")
    upd = make_update(text="dune", user_id=USER_ID)
    await issue_title(upd, ctx)
    reply = upd.effective_message.reply_calls[0]
    assert reply["text"].startswith("Search failed.")
    assert reply.get("reply_markup") is None


async def test_hub_report_timeout_offers_try_again_and_back():
    ctx = _hub_ctx()
    upd = make_update(text="x", user_id=USER_ID)
    state = await _issue_timeout(upd, ctx)
    assert state == ConversationHandler.END
    kwargs = ctx.bot.send_message.call_args.kwargs
    assert kwargs["text"] == "⏱️ The report timed out."
    assert _flat(kwargs["reply_markup"]) == [RETRY_REPORT, BACK]


async def test_hub_report_cancel_ends_with_back():
    ctx = _hub_ctx()
    upd = make_update(callback_data="cancel", user_id=USER_ID)
    state = await issue_cancel(upd, ctx)
    assert state == ConversationHandler.END
    edit = upd.callback_query.edits[0]
    assert edit["text"] == "Cancelled."
    assert _flat(edit["reply_markup"]) == [BACK]


async def test_typed_issue_cancel_keeps_old_text_and_no_buttons():
    ctx = _linked_ctx()
    upd = make_update(callback_data="cancel", user_id=USER_ID)
    await issue_cancel(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert edit["text"] == "Cancelled. /issue to start over."
    assert edit.get("reply_markup") is None


async def test_hub_report_submit_ends_with_back():
    ctx = _submit_ctx(TICKETS_HUB)
    upd = make_update(text="no sound", user_id=USER_ID)
    state = await _submit_issue(upd, ctx, autofix=False)
    assert state == ConversationHandler.END
    reply = upd.effective_message.reply_calls[0]
    assert reply["text"].startswith("✅ Reported as ticket #77")
    assert _flat(reply["reply_markup"]) == [BACK]


async def test_hub_report_submit_failure_offers_try_again_and_back():
    ctx = _submit_ctx(TICKETS_HUB)
    ctx.bot_data["seerr"].create_issue = _async_return(exc=RuntimeError("boom"))
    upd = make_update(text="no sound", user_id=USER_ID)
    await _submit_issue(upd, ctx, autofix=False)
    reply = upd.effective_message.reply_calls[0]
    assert reply["text"].startswith("Couldn't submit the ticket.")
    assert _flat(reply["reply_markup"]) == [RETRY_REPORT, BACK]


async def test_typed_issue_submit_has_no_buttons():
    ctx = _submit_ctx()
    upd = make_update(text="no sound", user_id=USER_ID)
    await _submit_issue(upd, ctx, autofix=False)
    reply = upd.effective_message.reply_calls[0]
    assert reply["text"].startswith("✅ Reported as ticket #77")
    assert reply.get("reply_markup") is None


async def test_other_hub_origin_gives_no_tickets_back():
    ctx = _submit_ctx("account")
    upd = make_update(text="no sound", user_id=USER_ID)
    await _submit_issue(upd, ctx, autofix=False)
    reply = upd.effective_message.reply_calls[0]
    assert reply.get("reply_markup") is None


def test_hub_report_is_issue_conversation_entry():
    conv = _issue_conversation()
    patterns = {ep.pattern.pattern for ep in conv.entry_points
                if getattr(ep, "pattern", None) is not None}
    assert f"^{TICKETS_REPORT}$" in patterns


# --- the list --------------------------------------------------------------------

async def test_hub_list_shows_user_tickets():
    ctx = make_ctx(admin_id=ADMIN_ID,
                   mapping=make_mapping(telegram_id=USER_ID, plex_token="user-token"))
    ctx.bot_data["seerr"].list_issues.return_value = ([_issue(id=7)], 1)
    upd = make_update(callback_data=TICKETS_LIST, user_id=USER_ID)
    await tickets_hub_list(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert "Your open tickets" in edit["text"] and "#7" in edit["text"]
    assert _flat(edit["reply_markup"]) == [("#7", f"{TK_OPEN}:7"), BACK]
    assert list(edit["reply_markup"].inline_keyboard[-1]) == [hub.back_button(TICKETS_HUB)]
    assert ctx.bot_data["seerr"].list_issues.call_args.kwargs["as_plex_token"] == "user-token"
    assert upd.callback_query.message.reply_calls == []


async def test_hub_list_shows_all_tickets_for_admin():
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["seerr"].list_issues.return_value = ([_issue(id=1), _issue(id=2)], 2)
    upd = make_update(callback_data=TICKETS_LIST, user_id=ADMIN_ID)
    await tickets_hub_list(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert "All open tickets" in edit["text"]
    assert ctx.bot_data["seerr"].list_issues.call_args.kwargs["as_plex_token"] is None


async def test_hub_list_fetch_failure_offers_try_again_and_back():
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["seerr"].list_issues.side_effect = RuntimeError("boom")
    upd = make_update(callback_data=TICKETS_LIST, user_id=ADMIN_ID)
    await tickets_hub_list(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert edit["text"].startswith("Couldn't fetch tickets.")
    assert _flat(edit["reply_markup"]) == [("🔁 Try again", TICKETS_LIST), BACK]


async def test_hub_list_empty_ends_with_back_only():
    ctx = make_ctx(admin_id=ADMIN_ID)
    upd = make_update(callback_data=TICKETS_LIST, user_id=ADMIN_ID)
    await tickets_hub_list(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert "No open tickets across all users" in edit["text"]
    assert _flat(edit["reply_markup"]) == [BACK]


async def test_hub_list_unlinked_ends_with_back_only():
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=None)
    upd = make_update(callback_data=TICKETS_LIST, user_id=USER_ID)
    await tickets_hub_list(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert "/link" in edit["text"]
    assert _flat(edit["reply_markup"]) == [BACK]
    ctx.bot_data["seerr"].list_issues.assert_not_called()


async def test_hub_list_ends_other_flows():
    from types import SimpleNamespace
    key = (100, ADMIN_ID)
    updates = []
    conv = SimpleNamespace(_get_key=lambda u: key, _conversations={key: 1},
                           _update_state=lambda s, k: updates.append((s, k)),
                           timeout_jobs={})
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.application.bot_data["flow_convs"] = [conv]
    await tickets_hub_list(make_update(callback_data=TICKETS_LIST, user_id=ADMIN_ID), ctx)
    assert updates == [(ConversationHandler.END, key)]


def test_tickets_list_button_registered():
    src = Path("bot/app.py").read_text(encoding="utf-8")
    last = src.index("CallbackQueryHandler(unmatched_callback)")
    found = re.search(
        r'CallbackQueryHandler\(tickets_hub_list,\s*pattern=fr"\^\{TICKETS_LIST\}\$"\)', src)
    assert found and found.start() < last
    assert re.search(
        r'CallbackQueryHandler\(tk_back,\s*pattern=fr"\^\(\{TK_BACK\}\|\{TK_BACK_LIST\}\):', src)


# --- the detail card ---------------------------------------------------------------

async def test_detail_from_list_has_tickets_back_to_list():
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["seerr"].get_issue.return_value = _issue(id=5)
    upd = _from_hub(make_update(callback_data=f"{TK_OPEN}:5", user_id=ADMIN_ID))
    await tk_open(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert edit["text"].startswith("<b>Ticket #5</b>")
    flat = _flat(edit["reply_markup"])
    assert flat[-1] == LIST_BACK
    assert ("💬 Reply", "tkr:5") in flat and ("🔧 Fix", "tkf:5") in flat
    assert upd.callback_query.message.reply_calls == []


async def test_detail_from_dm_has_no_tickets_back():
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["seerr"].get_issue.return_value = _issue(id=5)
    upd = make_update(callback_data=f"{TK_OPEN}:5", user_id=ADMIN_ID)
    await tk_open(upd, ctx)
    reply = upd.callback_query.message.reply_calls[0]
    assert reply["text"].startswith("<b>Ticket #5</b>")
    assert TICKETS_LIST not in [cb for _, cb in _flat(reply["reply_markup"])]
    assert upd.callback_query.edits == []


async def test_tickets_back_from_detail_shows_list():
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["seerr"].list_issues.return_value = ([_issue(id=5)], 1)
    upd = _from_list_detail(make_update(callback_data=TICKETS_LIST, user_id=ADMIN_ID))
    await tickets_hub_list(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert "All open tickets" in edit["text"]
    assert _flat(edit["reply_markup"]) == [("#5", f"{TK_OPEN}:5"), BACK]


async def test_failed_detail_from_list_offers_try_again():
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["seerr"].get_issue.side_effect = RuntimeError("boom")
    upd = _from_hub(make_update(callback_data=f"{TK_OPEN}:5", user_id=ADMIN_ID))
    await tk_open(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert "Couldn't load the ticket details" in edit["text"]
    assert _flat(edit["reply_markup"]) == [("🔁 Try again", f"{TK_OPEN}:5"), LIST_BACK]


async def test_open_from_message_with_list_back_edits_in_place():
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["seerr"].get_issue.return_value = _issue(id=5)
    upd = _from_list_detail(make_update(callback_data=f"{TK_OPEN}:5", user_id=ADMIN_ID))
    await tk_open(upd, ctx)
    assert _flat(upd.callback_query.edits[0]["reply_markup"])[-1] == LIST_BACK
    assert upd.callback_query.message.reply_calls == []


async def test_submenu_back_from_list_detail_keeps_tickets_back():
    ctx = make_ctx(admin_id=ADMIN_ID)
    for handler, data in ((tk_close_menu, "tkc:5"), (tk_fix, "tkf:5")):
        upd = _from_list_detail(make_update(callback_data=data, user_id=ADMIN_ID))
        await handler(upd, ctx)
        flat = _flat(upd.callback_query.edits[0]["reply_markup"])
        assert ("⬅️ Back", f"{TK_BACK_LIST}:5") in flat
    ctx.bot_data["seerr"].get_issue.return_value = _issue(id=5)
    upd = make_update(callback_data=f"{TK_BACK_LIST}:5", user_id=ADMIN_ID)
    await tk_back(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert edit["text"].startswith("<b>Ticket #5</b>")
    assert _flat(edit["reply_markup"])[-1] == LIST_BACK


async def test_submenu_back_from_dm_detail_has_no_tickets_back():
    ctx = make_ctx(admin_id=ADMIN_ID)
    upd = make_update(callback_data="tkc:5", user_id=ADMIN_ID)
    await tk_close_menu(upd, ctx)
    assert ("⬅️ Back", f"{TK_BACK}:5") in _flat(upd.callback_query.edits[0]["reply_markup"])
    ctx.bot_data["seerr"].get_issue.return_value = _issue(id=5)
    upd = make_update(callback_data=f"{TK_BACK}:5", user_id=ADMIN_ID)
    await tk_back(upd, ctx)
    flat = _flat(upd.callback_query.edits[0]["reply_markup"])
    assert TICKETS_LIST not in [cb for _, cb in flat]


# --- menu, /start and registration -------------------------------------------------

def test_menu_drops_issue():
    from bot.app import _ADMIN_BOT_COMMANDS, _BOT_COMMANDS
    for menu in (_BOT_COMMANDS, _ADMIN_BOT_COMMANDS):
        assert "issue" not in [c for c, _ in menu]
    assert ("tickets", "report a problem or check on your tickets") in _BOT_COMMANDS


def test_typed_issue_still_registered():
    conv = _issue_conversation()
    commands = {c for ep in conv.entry_points for c in getattr(ep, "commands", ())}
    assert "issue" in commands


NEW_TICKETS_LINE = "  /tickets — report a problem or check on your tickets"
NEW_NOTE = ("(/link, /unlink, /issue, /requests, /trending and /subscriptions "
            "still work when typed.)")


async def test_start_lists_tickets_hub_and_typed_note():
    from unittest.mock import AsyncMock
    import bot.app as app_mod
    app_mod_check = app_mod._check_connections
    try:
        app_mod._check_connections = AsyncMock(return_value={})
        for uid in (USER_ID, ADMIN_ID):
            ctx = make_ctx(admin_id=ADMIN_ID)
            upd = make_update(text="/start", user_id=uid)
            await app_mod.cmd_start(upd, ctx)
            text = upd.effective_message.reply_calls[0]["text"]
            assert NEW_TICKETS_LINE in text
            assert NEW_NOTE in text
            assert "/issue —" not in text
    finally:
        app_mod._check_connections = app_mod_check


# --- the relink prompt (revoked Plex token) ---------------------------------------

RELINK = ("🔗 Unlink & sign in again", "relink")


async def test_hub_report_relink_prompt_has_back():
    from seerr import PlexTokenInvalidError
    ctx = _submit_ctx(TICKETS_HUB)
    ctx.bot_data["seerr"].create_issue = _async_return(exc=PlexTokenInvalidError())
    upd = make_update(text="no sound", user_id=USER_ID)
    await _submit_issue(upd, ctx, autofix=False)
    reply = upd.effective_message.reply_calls[0]
    assert reply["text"].startswith("⚠️ Plex says your sign-in is no longer valid.")
    assert _flat(reply["reply_markup"]) == [RELINK, BACK]


async def test_typed_report_relink_prompt_has_relink_only():
    from seerr import PlexTokenInvalidError
    ctx = _submit_ctx()
    ctx.bot_data["seerr"].create_issue = _async_return(exc=PlexTokenInvalidError())
    upd = make_update(text="no sound", user_id=USER_ID)
    await _submit_issue(upd, ctx, autofix=False)
    assert _flat(upd.effective_message.reply_calls[0]["reply_markup"]) == [RELINK]


async def test_hub_list_relink_prompt_has_back():
    from seerr import PlexTokenInvalidError
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].list_issues.side_effect = PlexTokenInvalidError()
    upd = make_update(callback_data=TICKETS_LIST, user_id=USER_ID)
    await tickets_hub_list(upd, ctx)
    assert _flat(upd.callback_query.edits[-1]["reply_markup"]) == [RELINK, BACK]


async def test_detail_from_list_relink_prompt_has_list_back():
    from seerr import PlexTokenInvalidError
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].get_issue.side_effect = PlexTokenInvalidError()
    upd = _from_hub(make_update(callback_data=f"{TK_OPEN}:5", user_id=USER_ID))
    await tk_open(upd, ctx)
    assert _flat(upd.callback_query.edits[-1]["reply_markup"]) == [RELINK, LIST_BACK]
    upd = make_update(callback_data=f"{TK_BACK_LIST}:5", user_id=USER_ID)
    await tk_back(upd, ctx)
    assert _flat(upd.callback_query.edits[-1]["reply_markup"]) == [RELINK, LIST_BACK]


async def test_detail_from_dm_relink_prompt_has_relink_only():
    from seerr import PlexTokenInvalidError
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].get_issue.side_effect = PlexTokenInvalidError()
    upd = make_update(callback_data=f"{TK_OPEN}:5", user_id=USER_ID)
    await tk_open(upd, ctx)
    assert _flat(upd.callback_query.edits[-1]["reply_markup"]) == [RELINK]


# --- Reply, Close and Fix results of a detail opened from the list ----------------

def _from_list_submenu(upd):
    """Mark the tapped message as a Close or Fix sub-menu of a detail card
    opened from the list (its Back keeps the list-back row)."""
    upd.callback_query.message.reply_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back", callback_data=f"{TK_BACK_LIST}:5")]])
    return upd


async def test_close_from_list_ends_with_tickets_back():
    ctx = make_ctx(admin_id=ADMIN_ID)
    upd = _from_list_submenu(make_update(callback_data="tkcd:5", user_id=ADMIN_ID))
    await tk_close_direct(upd, ctx)
    edit = upd.callback_query.edits[-1]
    assert edit["text"] == "✅ Closed ticket #5."
    assert _flat(edit["reply_markup"]) == [LIST_BACK]


async def test_close_from_list_failure_offers_try_again_and_tickets_back():
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["seerr"].resolve_issue.side_effect = RuntimeError("boom")
    upd = _from_list_submenu(make_update(callback_data="tkcd:5", user_id=ADMIN_ID))
    await tk_close_direct(upd, ctx)
    edit = upd.callback_query.edits[-1]
    assert edit["text"].startswith("Couldn't close #5.")
    assert _flat(edit["reply_markup"]) == [("🔁 Try again", "tkcd:5"), LIST_BACK]


async def test_close_from_dm_has_no_buttons():
    ctx = make_ctx(admin_id=ADMIN_ID)
    upd = make_update(callback_data="tkcd:5", user_id=ADMIN_ID)
    await tk_close_direct(upd, ctx)
    assert upd.callback_query.edits[-1].get("reply_markup") is None


async def test_fix_from_list_ends_with_tickets_back():
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["seerr"].get_issue.return_value = _issue(id=5)
    ctx.bot_data["radarr"].auto_fix.return_value = FixResult.success(
        "Deleted file and triggered re-search.", steps_done=["delete", "search"],
        poll_info={"movie_id": 555})
    upd = _from_list_submenu(make_update(callback_data="tkfd:5", user_id=ADMIN_ID))
    await _apply_fix(upd, ctx, strategy="redownload")
    edit = upd.callback_query.edits[-1]
    assert edit["text"].startswith("🔧 Redownload for #5.")
    assert _flat(edit["reply_markup"]) == [LIST_BACK]


async def test_fix_from_list_that_did_not_run_offers_try_again_and_tickets_back():
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["seerr"].get_issue.return_value = _issue(id=5)
    ctx.bot_data["radarr"].mark_failed.return_value = FIX_FAILED
    upd = _from_list_submenu(make_update(callback_data="tkfm:5", user_id=ADMIN_ID))
    await _apply_fix(upd, ctx, strategy="mark_failed")
    edit = upd.callback_query.edits[-1]
    assert edit["text"].startswith("⚠️ Mark Failed for #5 didn't run")
    assert _flat(edit["reply_markup"]) == [("🔁 Try again", "tkfm:5"), LIST_BACK]


async def test_fix_from_list_fetch_failure_offers_try_again_and_tickets_back():
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["seerr"].get_issue.side_effect = RuntimeError("boom")
    upd = _from_list_submenu(make_update(callback_data="tkfd:5", user_id=ADMIN_ID))
    await _apply_fix(upd, ctx, strategy="redownload")
    edit = upd.callback_query.edits[-1]
    assert edit["text"].startswith("Couldn't fetch ticket #5.")
    assert _flat(edit["reply_markup"]) == [("🔁 Try again", "tkfd:5"), LIST_BACK]


async def test_fix_from_list_on_a_whole_season_ends_with_tickets_back_only():
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["seerr"].get_issue.return_value = _issue(
        id=5, media_type="tv", problem_season=1, problem_episode=None)
    upd = _from_list_submenu(make_update(callback_data="tkfd:5", user_id=ADMIN_ID))
    await _apply_fix(upd, ctx, strategy="redownload")
    edit = upd.callback_query.edits[-1]
    assert "only works on individual episodes or movies" in edit["text"]
    assert _flat(edit["reply_markup"]) == [LIST_BACK]


async def test_fix_from_dm_has_no_buttons():
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["seerr"].get_issue.side_effect = RuntimeError("boom")
    upd = make_update(callback_data="tkfd:5", user_id=ADMIN_ID)
    await _apply_fix(upd, ctx, strategy="redownload")
    assert upd.callback_query.edits[-1].get("reply_markup") is None


async def test_try_again_on_a_result_keeps_tickets_back():
    ctx = make_ctx(admin_id=ADMIN_ID)
    upd = make_update(callback_data="tkcd:5", user_id=ADMIN_ID)
    upd.callback_query.message.reply_markup = InlineKeyboardMarkup(
        [[hub.try_again_button("tkcd:5"),
          InlineKeyboardButton("⬅️ Tickets", callback_data=TICKETS_LIST)]])
    await tk_close_direct(upd, ctx)
    assert _flat(upd.callback_query.edits[-1]["reply_markup"]) == [LIST_BACK]


async def _start_reply_from_list(ctx, data="tkr:5", user_id=ADMIN_ID):
    upd = make_update(callback_data=data, user_id=user_id)
    if data.startswith("tkr"):
        await tk_reply_start(_from_list_detail(upd), ctx)
    else:
        await tk_close_with_comment_start(_from_list_submenu(upd), ctx)


async def test_reply_from_list_ends_with_tickets_back():
    ctx = make_ctx(admin_id=ADMIN_ID)
    await _start_reply_from_list(ctx)
    upd = make_update(text="on it", user_id=ADMIN_ID)
    await tk_reply_text(upd, ctx)
    reply = upd.effective_message.reply_calls[-1]
    assert reply["text"] == "💬 Replied to ticket #5."
    assert _flat(reply["reply_markup"]) == [LIST_BACK]


async def test_close_with_comment_from_list_ends_with_tickets_back():
    ctx = make_ctx(admin_id=ADMIN_ID)
    await _start_reply_from_list(ctx, "tkcc:5")
    upd = make_update(text="fixed", user_id=ADMIN_ID)
    await tk_reply_text(upd, ctx)
    reply = upd.effective_message.reply_calls[-1]
    assert reply["text"] == "💬 Replied and ✅ closed ticket #5."
    assert _flat(reply["reply_markup"]) == [LIST_BACK]


async def test_reply_from_list_failure_offers_try_again_and_tickets_back():
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["seerr"].add_issue_comment.side_effect = RuntimeError("boom")
    await _start_reply_from_list(ctx)
    upd = make_update(text="on it", user_id=ADMIN_ID)
    await tk_reply_text(upd, ctx)
    reply = upd.effective_message.reply_calls[-1]
    assert reply["text"].startswith("Couldn't post comment on #5.")
    assert _flat(reply["reply_markup"]) == [("🔁 Try again", "tkr:5"), LIST_BACK]


async def test_close_failure_after_comment_offers_close_again_and_tickets_back():
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["seerr"].resolve_issue.side_effect = RuntimeError("boom")
    await _start_reply_from_list(ctx, "tkcc:5")
    upd = make_update(text="fixed", user_id=ADMIN_ID)
    await tk_reply_text(upd, ctx)
    reply = upd.effective_message.reply_calls[-1]
    assert reply["text"].startswith("💬 Comment posted on #5, but couldn't close.")
    assert _flat(reply["reply_markup"]) == [("🔁 Try again", "tkcd:5"), LIST_BACK]


async def test_reply_from_list_relink_prompt_has_tickets_back():
    from seerr import PlexTokenInvalidError
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].add_issue_comment.side_effect = PlexTokenInvalidError()
    await _start_reply_from_list(ctx, user_id=USER_ID)
    upd = make_update(text="on it", user_id=USER_ID)
    await tk_reply_text(upd, ctx)
    assert _flat(upd.effective_message.reply_calls[-1]["reply_markup"]) == [
        RELINK, LIST_BACK]


async def test_reply_from_list_cancel_and_timeout_end_with_tickets_back():
    ctx = make_ctx(admin_id=ADMIN_ID)
    await _start_reply_from_list(ctx)
    upd = make_update(text="/cancel", user_id=ADMIN_ID)
    await tk_reply_cancel(upd, ctx)
    reply = upd.effective_message.reply_calls[-1]
    assert reply["text"] == "Cancelled."
    assert _flat(reply["reply_markup"]) == [LIST_BACK]
    await _start_reply_from_list(ctx)
    await _tk_reply_timeout(make_update(text="x", user_id=ADMIN_ID), ctx)
    kwargs = ctx.bot.send_message.call_args.kwargs
    assert _flat(kwargs["reply_markup"]) == [("🔁 Try again", "tkr:5"), LIST_BACK]
    assert "tk_reply_from_list" not in ctx.user_data


async def test_reply_from_dm_ends_without_buttons():
    ctx = make_ctx(admin_id=ADMIN_ID)
    await tk_reply_start(make_update(callback_data="tkr:5", user_id=ADMIN_ID), ctx)
    upd = make_update(text="on it", user_id=ADMIN_ID)
    await tk_reply_text(upd, ctx)
    assert upd.effective_message.reply_calls[-1].get("reply_markup") is None
