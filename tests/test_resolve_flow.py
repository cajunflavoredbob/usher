"""Tests for bot.resolve_flow: the identity gate (an unlinked or
decrypt-failed non-admin must never fall through to the admin API key),
admin attribution, and conversation hygiene (timeout)."""
from __future__ import annotations

from telegram.ext import ConversationHandler

from bot.resolve_flow import (
    _resolve_conversation,
    _resolve_timeout,
    resolve_comment,
    resolve_start,
)
from const import RESOLVE_FLOW_TIMEOUT_S
from tests._handler_harness import make_ctx, make_mapping, make_update


# --- resolve_start: identity gate --------------------------------------------


async def test_unlinked_user_cannot_close_with_admin_key():
    upd = make_update(callback_data="resolve:42:yes", user_id=42)
    ctx = make_ctx(admin_id=999, mapping=None)  # unlinked
    await resolve_start(upd, ctx)
    ctx.bot_data["seerr"].resolve_issue.assert_not_called()
    assert "/link" in upd.callback_query.edits[0]["text"]


async def test_decrypt_failed_user_told_to_relink():
    upd = make_update(callback_data="resolve:42:yes", user_id=42)
    ctx = make_ctx(admin_id=999, mapping=make_mapping(decrypt_failed=True))
    await resolve_start(upd, ctx)
    ctx.bot_data["seerr"].resolve_issue.assert_not_called()
    assert "/unlink then /link" in upd.callback_query.edits[0]["text"]


async def test_linked_user_resolves_with_own_token():
    upd = make_update(callback_data="resolve:42:yes", user_id=42)
    ctx = make_ctx(admin_id=999, mapping=make_mapping(plex_token="plex-abc"))
    await resolve_start(upd, ctx)
    ctx.bot_data["seerr"].resolve_issue.assert_called_once_with(
        42, as_plex_token="plex-abc")
    assert "closed" in upd.callback_query.edits[0]["text"]


async def test_admin_resolves_with_admin_key():
    upd = make_update(callback_data="resolve:42:yes", user_id=999)
    ctx = make_ctx(admin_id=999)
    await resolve_start(upd, ctx)
    ctx.bot_data["seerr"].resolve_issue.assert_called_once_with(
        42, as_plex_token=None)


async def test_skip_needs_no_link():
    """'No, leave it' touches nothing in Seerr, so no link is required."""
    upd = make_update(callback_data="resolve:42:skip", user_id=42)
    ctx = make_ctx(admin_id=999, mapping=None)
    await resolve_start(upd, ctx)
    assert "leaving the issue open" in upd.callback_query.edits[0]["text"]
    ctx.bot_data["seerr"].resolve_issue.assert_not_called()


async def test_unlinked_user_cannot_enter_comment_state():
    upd = make_update(callback_data="resolve:42:no", user_id=42)
    ctx = make_ctx(admin_id=999, mapping=None)
    state = await resolve_start(upd, ctx)
    assert state == ConversationHandler.END
    assert "awaiting_comment_for" not in ctx.user_data


# --- resolve_comment: identity re-check at submit time -----------------------


async def test_comment_refused_when_link_vanished_mid_conversation():
    """User tapped 'add a comment' while linked, then /unlink'd (or the key
    rotated) before sending the text: must not post via the admin key."""
    upd = make_update(text="it is still broken", user_id=42)
    ctx = make_ctx(admin_id=999, mapping=None,
                   user_data={"awaiting_comment_for": 42})
    state = await resolve_comment(upd, ctx)
    assert state == ConversationHandler.END
    ctx.bot_data["seerr"].add_issue_comment.assert_not_called()
    assert "awaiting_comment_for" not in ctx.user_data
    assert "/link" in upd.effective_message.reply_calls[0]["text"]


async def test_comment_posts_with_user_token():
    upd = make_update(text="audio still off", user_id=42)
    ctx = make_ctx(admin_id=999, mapping=make_mapping(plex_token="plex-abc"),
                   user_data={"awaiting_comment_for": 42})
    await resolve_comment(upd, ctx)
    ctx.bot_data["seerr"].add_issue_comment.assert_called_once_with(
        42, "audio still off", as_plex_token="plex-abc")


# --- conversation hygiene -----------------------------------------------------


def test_conversation_has_timeout():
    conv = _resolve_conversation()
    assert conv.conversation_timeout == RESOLVE_FLOW_TIMEOUT_S
    assert ConversationHandler.TIMEOUT in conv.states


async def test_timeout_handler_clears_marker():
    ctx = make_ctx(user_data={"awaiting_comment_for": 42})
    state = await _resolve_timeout(None, ctx)
    assert state == ConversationHandler.END
    assert "awaiting_comment_for" not in ctx.user_data
