"""Once an admin Fix starts, the Ticket card shows the Ticket
again with its buttons and the Fix's working state, result and download
progress go on a separate new Progress card. A Fix that can't start leaves
its reason and "⬅️ Ticket" on the Ticket card."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.callback_prefixes import TK_BACK, TK_BACK_DM, TK_BACK_LIST
from bot.shared import new_ticket_dm_kb
from bot.tickets import apply_fix, tk_back, tk_close_menu, tk_fix
from fix_result import FixResult
from seerr import IssueListItem
from tests._handler_harness import make_ctx, make_update

ADMIN_ID = 999
CARD_ID = 777
TICKET_BACK = ("⬅️ Ticket", f"{TK_BACK}:5")
TICKET_BUTTONS = [("💬 Reply", "tkr:5"), ("🔧 Fix", "tkf:5"), ("✅ Close", "tkc:5")]


def _flat(markup):
    return [(b.text, b.callback_data)
            for row in markup.inline_keyboard for b in row]


def _ticket(**kwargs) -> IssueListItem:
    defaults = dict(
        id=5, issue_type=1, status=1, created_at="2026-05-29T12:00:00Z",
        tmdb_id=12345, media_type="movie", problem_season=None,
        problem_episode=None, created_by="someone",
    )
    defaults.update(kwargs)
    return IssueListItem(**defaults)


def _ctx(result=None, *, mark_failed=False, **ticket_kwargs):
    ctx = make_ctx(admin_id=ADMIN_ID)
    ctx.bot_data["seerr"].get_issue.return_value = _ticket(**ticket_kwargs)
    ctx.bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=CARD_ID))
    ctx.bot.delete_message = AsyncMock()
    if result is None:
        result = FixResult.success(
            "Deleted file and triggered re-search.",
            steps_done=["delete", "search"], poll_info={"movie_id": 555})
    radarr = ctx.bot_data["radarr"]
    (radarr.mark_failed if mark_failed else radarr.auto_fix).return_value = result
    return ctx


async def _fix(ctx, data="tkfd:5"):
    upd = make_update(callback_data=data, user_id=ADMIN_ID)
    strategy = "mark_failed" if data.startswith("tkfm") else "redownload"
    await apply_fix(upd, ctx, strategy=strategy)
    return upd


def _card_texts(ctx):
    """Every text the Progress card showed: the send, then each edit of it."""
    texts = [c.kwargs["text"] for c in ctx.bot.send_message.call_args_list]
    texts += [c.kwargs["text"] for c in ctx.bot.edit_message_text.call_args_list
              if c.kwargs.get("message_id") == CARD_ID]
    return texts


# --- a Fix that starts ---------------------------------------------------------------

async def _assert_restored_with_progress_card(ctx, upd, action):
    ticket = upd.callback_query.edits[-1]
    assert ticket["text"].startswith("<b>Ticket #5</b>")
    assert _flat(ticket["reply_markup"]) == TICKET_BUTTONS
    ctx.bot.send_message.assert_awaited_once()
    sent = ctx.bot.send_message.call_args.kwargs
    assert sent["text"] == f"⏳ {action} for #5 - working..."
    assert sent.get("reply_markup") is None
    texts = _card_texts(ctx)
    assert texts[-1].startswith(f"🔧 {action} for #5.")
    assert "I'll DM when the new file finishes downloading." in texts[-1]
    assert not any(e["text"].startswith(f"🔧 {action}")
                   for e in upd.callback_query.edits)


async def test_redownload_restores_ticket_card_and_sends_progress_card():
    ctx = _ctx()
    upd = await _fix(ctx)
    await _assert_restored_with_progress_card(ctx, upd, "Redownload")


async def test_mark_failed_restores_ticket_card_and_sends_progress_card():
    ctx = _ctx(FixResult.success(
        "Blocklisted current release, deleted file, and triggered re-search.",
        steps_done=["blocklist", "delete", "search"], poll_info={"movie_id": 555}),
        mark_failed=True)
    upd = await _fix(ctx, "tkfm:5")
    await _assert_restored_with_progress_card(ctx, upd, "Mark Failed")


async def test_first_edit_strips_the_fix_choices():
    ctx = _ctx()
    upd = await _fix(ctx)
    first = upd.callback_query.edits[0]
    assert first["text"] == "⏳ Starting Redownload for #5..."
    assert first.get("reply_markup") is None


async def test_progress_card_id_is_stored_for_the_poller():
    ctx = _ctx()
    ctx.bot_data["store"].add_pending_autofix = AsyncMock(return_value=41)
    await _fix(ctx)
    store = ctx.bot_data["store"]
    store.set_autofix_message.assert_awaited_once()
    call = store.set_autofix_message.call_args
    assert call.args[:2] == (41, CARD_ID)
    assert call.kwargs.get("reply_markup", "") == ""


async def test_enqueue_failure_is_reported_on_progress_card():
    ctx = _ctx()
    ctx.bot_data["store"].add_pending_autofix = AsyncMock(
        side_effect=RuntimeError("db"))
    upd = await _fix(ctx)
    assert "couldn't enqueue completion notification" in _card_texts(ctx)[-1]
    assert _flat(upd.callback_query.edits[-1]["reply_markup"]) == TICKET_BUTTONS
    ctx.bot_data["store"].set_autofix_message.assert_not_awaited()


async def test_progress_card_send_failure_sends_result_later():
    ctx = _ctx()
    ctx.bot.send_message = AsyncMock(side_effect=[
        RuntimeError("flood"), SimpleNamespace(message_id=CARD_ID)])
    ctx.bot_data["store"].add_pending_autofix = AsyncMock(return_value=41)
    await _fix(ctx)
    assert ctx.bot.send_message.await_count == 2
    last = ctx.bot.send_message.call_args.kwargs["text"]
    assert last.startswith("🔧 Redownload for #5.")
    assert ctx.bot_data["store"].set_autofix_message.call_args.args[:2] == (41, CARD_ID)


# --- a Fix that can't start or didn't run ---------------------------------------------

async def test_fix_that_did_not_run_leaves_reason_and_ticket_back():
    ctx = _ctx(FixResult.failed("Movie isn't in Radarr."))
    upd = await _fix(ctx)
    edit = upd.callback_query.edits[-1]
    assert edit["text"] == "⚠️ Redownload for #5 didn't run: Movie isn't in Radarr."
    assert _flat(edit["reply_markup"]) == [TICKET_BACK]


async def test_fix_that_did_not_run_deletes_progress_card():
    ctx = _ctx(FixResult.failed("Movie isn't in Radarr."))
    await _fix(ctx)
    ctx.bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=CARD_ID)
    ctx.bot_data["store"].add_pending_autofix.assert_not_called()


async def test_mark_failed_that_did_not_run_leaves_reason_and_ticket_back():
    ctx = _ctx(FixResult.failed("No grab to mark failed."), mark_failed=True)
    upd = await _fix(ctx, "tkfm:5")
    edit = upd.callback_query.edits[-1]
    assert edit["text"].startswith("⚠️ Mark Failed for #5 didn't run")
    assert _flat(edit["reply_markup"]) == [TICKET_BACK]
    ctx.bot.delete_message.assert_awaited_once()


async def test_whole_season_fix_leaves_sonarr_reason_and_ticket_back():
    ctx = _ctx(media_type="tv", problem_season=1, problem_episode=None)
    upd = await _fix(ctx)
    edit = upd.callback_query.edits[-1]
    assert "fix it in Sonarr directly" in edit["text"]
    assert _flat(edit["reply_markup"]) == [TICKET_BACK]
    ctx.bot.send_message.assert_not_awaited()


async def test_busy_fix_leaves_reason_and_ticket_back():
    ctx = _ctx()
    ctx.bot_data["inflight_actions"] = {"movie:12345"}
    upd = await _fix(ctx)
    edit = upd.callback_query.edits[-1]
    assert "is already running" in edit["text"]
    assert _flat(edit["reply_markup"]) == [TICKET_BACK]
    ctx.bot.send_message.assert_not_awaited()


async def test_fetch_failure_leaves_reason_and_ticket_back():
    ctx = _ctx()
    ctx.bot_data["seerr"].get_issue.side_effect = RuntimeError("boom")
    upd = await _fix(ctx)
    edit = upd.callback_query.edits[-1]
    assert edit["text"].startswith("Couldn't fetch ticket #5.")
    assert _flat(edit["reply_markup"]) == [TICKET_BACK]
    ctx.bot.send_message.assert_not_awaited()


async def test_ticket_back_shows_ticket_with_buttons():
    ctx = _ctx()
    upd = make_update(callback_data=f"{TK_BACK}:5", user_id=ADMIN_ID)
    await tk_back(upd, ctx)
    edit = upd.callback_query.edits[-1]
    assert edit["text"].startswith("<b>Ticket #5</b>")
    assert _flat(edit["reply_markup"]) == TICKET_BUTTONS
    upd = make_update(callback_data=f"{TK_BACK_LIST}:5", user_id=ADMIN_ID)
    await tk_back(upd, ctx)
    assert _flat(upd.callback_query.edits[-1]["reply_markup"])[-1] == (
        "⬅️ Tickets", "tkhl")


async def test_admin_fix_enqueues_its_action_name_for_the_poller():
    ctx = _ctx()
    ctx.bot_data["store"].add_pending_autofix = AsyncMock(return_value=41)
    await _fix(ctx)
    assert ctx.bot_data["store"].add_pending_autofix.call_args.kwargs["action_name"] == "Redownload"


# --- a Fix on a new-Ticket DM keeps its History row ------------------------------

DM_BUTTONS = TICKET_BUTTONS + [("📜 History", "tkopen:5")]


def _from_dm(upd, markup=None):
    """The tapped message is the new-Ticket DM (or a sub-menu of it)."""
    upd.callback_query.message.reply_markup = markup or new_ticket_dm_kb(5)
    return upd


async def test_new_ticket_dm_keeps_history_after_a_fix_starts():
    ctx = _ctx()
    upd = make_update(callback_data="tkfd:5", user_id=ADMIN_ID)
    _from_dm(upd, InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back", callback_data=f"{TK_BACK_DM}:5")]]))
    await apply_fix(upd, ctx, strategy="redownload")
    assert _flat(upd.callback_query.edits[-1]["reply_markup"]) == DM_BUTTONS


async def test_new_ticket_dm_submenus_back_shows_dm_buttons_with_history():
    ctx = _ctx()
    for handler, data in ((tk_fix, "tkf:5"), (tk_close_menu, "tkc:5")):
        upd = _from_dm(make_update(callback_data=data, user_id=ADMIN_ID))
        await handler(upd, ctx)
        assert ("⬅️ Back", f"{TK_BACK_DM}:5") in _flat(
            upd.callback_query.edits[0]["reply_markup"])
    upd = make_update(callback_data=f"{TK_BACK_DM}:5", user_id=ADMIN_ID)
    await tk_back(upd, ctx)
    assert _flat(upd.callback_query.edits[-1]["reply_markup"]) == DM_BUTTONS


async def test_new_ticket_dm_fix_that_cannot_start_backs_to_dm_buttons():
    ctx = _ctx()
    ctx.bot_data["seerr"].get_issue.side_effect = RuntimeError("boom")
    upd = _from_dm(make_update(callback_data="tkfd:5", user_id=ADMIN_ID),
                   InlineKeyboardMarkup(
                       [[InlineKeyboardButton("⬅️ Back", callback_data=f"{TK_BACK_DM}:5")]]))
    await apply_fix(upd, ctx, strategy="redownload")
    assert _flat(upd.callback_query.edits[-1]["reply_markup"]) == [
        ("⬅️ Ticket", f"{TK_BACK_DM}:5")]


async def test_ticket_view_card_still_has_no_history_after_a_fix():
    ctx = _ctx()
    upd = make_update(callback_data="tkfd:5", user_id=ADMIN_ID)
    upd.callback_query.message.reply_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back", callback_data=f"{TK_BACK}:5")]])
    await apply_fix(upd, ctx, strategy="redownload")
    assert _flat(upd.callback_query.edits[-1]["reply_markup"]) == TICKET_BUTTONS
