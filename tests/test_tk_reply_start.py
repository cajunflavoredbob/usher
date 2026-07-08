"""Tests for bot.tickets.tk_reply_start / tk_close_with_comment_start: the
Reply-button entry points. The v0.11.22 fix: clicking Reply must strip the
inline buttons but KEEP the original issue-announcement text, and prompt for
the reply in a *separate* message (previously edit_message_text overwrote the
whole announcement)."""
from __future__ import annotations

from bot.tickets import tk_reply_start, tk_close_with_comment_start
from bot.shared import AWAIT_TICKET_REPLY
from tests._handler_harness import make_ctx, make_update


async def test_reply_start_keeps_text_strips_buttons_prompts_separately():
    upd = make_update(callback_data="tkr:42", user_id=999)
    ctx = make_ctx(admin_id=999)

    state = await tk_reply_start(upd, ctx)

    assert state == AWAIT_TICKET_REPLY
    q = upd.callback_query
    # Original announcement text was NOT overwritten (no edit_message_text).
    assert q.edits == []
    # Buttons were stripped via edit_message_reply_markup(None).
    assert q.markup_edits == [None]
    # The prompt went out as a NEW message.
    assert len(q.message.reply_calls) == 1
    assert "Send the reply text for ticket #42" in q.message.reply_calls[0]["text"]
    # Flow state recorded.
    assert ctx.user_data["tk_reply_id"] == 42
    assert ctx.user_data["tk_close_after"] is False


async def test_close_with_comment_start_keeps_text_strips_buttons():
    upd = make_update(callback_data="tkcc:42", user_id=999)
    ctx = make_ctx(admin_id=999)

    state = await tk_close_with_comment_start(upd, ctx)

    assert state == AWAIT_TICKET_REPLY
    q = upd.callback_query
    assert q.edits == []
    assert q.markup_edits == [None]
    assert len(q.message.reply_calls) == 1
    assert "Send the closing comment for ticket #42" in q.message.reply_calls[0]["text"]
    assert ctx.user_data["tk_close_after"] is True
