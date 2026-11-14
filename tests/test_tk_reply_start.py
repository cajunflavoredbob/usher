"""Tests for bot.tickets.tk_reply_start / tk_close_with_comment_start: the
Reply-button entry points. The v0.11.22 fix: clicking Reply must strip the
inline buttons but KEEP the original Ticket-announcement text, and prompt for
the reply in a *separate* message (previously edit_message_text overwrote the
whole announcement)."""
from __future__ import annotations

import asyncio

from telegram.ext import ConversationHandler

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


async def _second_tap_does_nothing(start, data, other_data):
    """Park the first tap inside its button strip, tap again, then release."""
    gate = asyncio.Event()
    first = make_update(callback_data=data, user_id=999)
    strip = first.callback_query.edit_message_reply_markup

    async def slow_strip(reply_markup=None, **kwargs):
        await gate.wait()
        await strip(reply_markup=reply_markup, **kwargs)
    first.callback_query.edit_message_reply_markup = slow_strip
    ctx = make_ctx(admin_id=999)

    task = asyncio.create_task(start(first, ctx))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    second = make_update(callback_data=data, user_id=999)
    assert await start(second, ctx) == ConversationHandler.END
    assert second.callback_query.message.reply_calls == []
    assert second.callback_query.markup_edits == []
    # A tap on the sibling button for the same ticket is held off too, so it
    # cannot overwrite tk_close_after while the first start is mid-flight.
    sibling = make_update(callback_data=other_data, user_id=999)
    sibling_start = (tk_close_with_comment_start if start is tk_reply_start
                     else tk_reply_start)
    assert await sibling_start(sibling, ctx) == ConversationHandler.END
    assert sibling.callback_query.message.reply_calls == []

    gate.set()
    assert await task == AWAIT_TICKET_REPLY
    assert len(first.callback_query.message.reply_calls) == 1
    assert ctx.user_data["tk_reply_id"] == 42
    # The slot is free again once the start handler returns.
    assert ctx.bot_data.get("inflight_actions") == set()
    return ctx


async def test_reply_start_second_concurrent_tap_does_nothing():
    ctx = await _second_tap_does_nothing(tk_reply_start, "tkr:42", "tkcc:42")
    assert ctx.user_data["tk_close_after"] is False


async def test_close_with_comment_start_second_concurrent_tap_does_nothing():
    ctx = await _second_tap_does_nothing(tk_close_with_comment_start, "tkcc:42", "tkr:42")
    assert ctx.user_data["tk_close_after"] is True


async def test_reply_start_releases_slot_after_early_exit():
    upd = make_update(callback_data="tkr:42", user_id=1)
    ctx = make_ctx(admin_id=999)  # user 1 is not admin and not linked
    assert await tk_reply_start(upd, ctx) == ConversationHandler.END
    assert ctx.bot_data.get("inflight_actions") == set()


async def test_close_with_comment_start_releases_slot_after_early_exit():
    upd = make_update(callback_data="tkcc:42", user_id=1)
    ctx = make_ctx(admin_id=999)  # user 1 is not admin
    assert await tk_close_with_comment_start(upd, ctx) == ConversationHandler.END
    # The admin check now runs before the slot is taken; either way no
    # slot may be left held.
    assert not ctx.bot_data.get("inflight_actions")
    # And the refusal is the one answer the query gets, so it shows.
    assert upd.callback_query.answers == [("Admin only.", False)]
