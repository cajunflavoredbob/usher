"""Tests for bot.tickets.tk_reply_text: ticket-reply conversation message
handler. Covers admin path (no token), user path (with token), decrypt_failed
message, post-await mismatch (the v0.11.6 CONC #9 fix), close_after."""
from __future__ import annotations

import pytest
from telegram.ext import ConversationHandler

from bot.tickets import tk_reply_text
from bot.shared import AWAIT_TICKET_REPLY
from tests._handler_harness import make_ctx, make_mapping, make_update


# --- happy: admin reply ---


async def test_admin_reply_posts_comment_no_token():
    upd = make_update(text="thanks, will look into it", user_id=999)
    ctx = make_ctx(admin_id=999, user_data={"tk_reply_id": 42, "tk_close_after": False})
    state = await tk_reply_text(upd, ctx)
    assert state == ConversationHandler.END
    # add_issue_comment called with as_plex_token=None (admin)
    ctx.bot_data["seerr"].add_issue_comment.assert_called_once()
    args, kwargs = ctx.bot_data["seerr"].add_issue_comment.call_args
    assert args[0] == 42
    assert args[1] == "thanks, will look into it"
    assert kwargs["as_plex_token"] is None
    # User-data cleared
    assert "tk_reply_id" not in ctx.user_data
    # User notified
    text = upd.message.reply_calls[0]["text"]
    assert "Replied to ticket #42" in text


# --- happy: user reply with token ---


async def test_user_reply_with_token():
    mapping = make_mapping(telegram_id=42, plex_token="user-token-abc")
    upd = make_update(text="this is my reply", user_id=42)
    ctx = make_ctx(admin_id=999, mapping=mapping,
                   user_data={"tk_reply_id": 7, "tk_close_after": False})
    state = await tk_reply_text(upd, ctx)
    assert state == ConversationHandler.END
    args, kwargs = ctx.bot_data["seerr"].add_issue_comment.call_args
    assert kwargs["as_plex_token"] == "user-token-abc"


# --- decrypt_failed branch ---


async def test_user_with_decrypt_failed_mapping():
    mapping = make_mapping(telegram_id=42, decrypt_failed=True)
    upd = make_update(text="hi", user_id=42)
    ctx = make_ctx(admin_id=999, mapping=mapping,
                   user_data={"tk_reply_id": 1, "tk_close_after": False})
    state = await tk_reply_text(upd, ctx)
    assert state == ConversationHandler.END
    text = upd.message.reply_calls[0]["text"]
    assert "can't be decrypted" in text
    assert "/unlink" in text and "/link" in text
    # No Seerr call
    ctx.bot_data["seerr"].add_issue_comment.assert_not_called()


# --- the CONC #9 post-await mismatch fix ---


async def test_close_after_suppressed_when_user_started_new_reply():
    """While add_issue_comment is awaiting, the user kicks off a new reply
    flow for a different issue. The comment landed on issue 42 (bound at
    entry), but the close-after side effect must NOT fire -- it would
    affect a stale flow."""
    upd = make_update(text="closing comment", user_id=999)
    ctx = make_ctx(admin_id=999, user_data={"tk_reply_id": 42, "tk_close_after": True})

    # Simulate the user starting a new reply during the comment-post await
    # by overwriting tk_reply_id while add_issue_comment is in flight.
    async def slow_add_comment(*args, **kwargs):
        ctx.user_data["tk_reply_id"] = 99  # user moved to a different issue
    ctx.bot_data["seerr"].add_issue_comment.side_effect = slow_add_comment

    state = await tk_reply_text(upd, ctx)
    assert state == ConversationHandler.END
    # Comment was posted on issue 42 (the locally-bound issue_id).
    args, _ = ctx.bot_data["seerr"].add_issue_comment.call_args
    assert args[0] == 42
    # But resolve_issue was NOT called (close-after suppressed).
    ctx.bot_data["seerr"].resolve_issue.assert_not_called()
    # User got an honest "you've started a new reply" message.
    text = upd.message.reply_calls[0]["text"]
    assert "Reply posted on #42" in text
    assert "new reply" in text


# --- close_after happy path ---


async def test_close_after_happy_path_calls_resolve():
    upd = make_update(text="closing", user_id=999)
    ctx = make_ctx(admin_id=999, user_data={"tk_reply_id": 42, "tk_close_after": True})
    state = await tk_reply_text(upd, ctx)
    assert state == ConversationHandler.END
    ctx.bot_data["seerr"].add_issue_comment.assert_called_once()
    ctx.bot_data["seerr"].resolve_issue.assert_called_once_with(42, as_plex_token=None)
    text = upd.message.reply_calls[0]["text"]
    assert "Replied and ✅ closed ticket #42" in text


# --- guards ---


async def test_empty_message_re_prompts():
    upd = make_update(text="   ", user_id=999)  # whitespace only
    ctx = make_ctx(admin_id=999, user_data={"tk_reply_id": 42, "tk_close_after": False})
    state = await tk_reply_text(upd, ctx)
    assert state == AWAIT_TICKET_REPLY  # stay in the state
    assert "Empty message" in upd.message.reply_calls[0]["text"]
    ctx.bot_data["seerr"].add_issue_comment.assert_not_called()


async def test_no_tk_reply_id_ends_quietly():
    """If somehow we reach this handler without tk_reply_id (e.g., race with
    the TIMEOUT handler clearing user_data), end the conversation cleanly."""
    upd = make_update(text="x", user_id=999)
    ctx = make_ctx(admin_id=999, user_data={})  # no tk_reply_id
    state = await tk_reply_text(upd, ctx)
    assert state == ConversationHandler.END
    ctx.bot_data["seerr"].add_issue_comment.assert_not_called()
