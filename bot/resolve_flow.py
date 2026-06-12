"""Post-autofix resolve follow-up conversation: ask if it fixed the issue,
optionally add a comment, optionally close the ticket."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from http_util import user_friendly_message
from seerr import SeerrClient

from bot.callback_prefixes import RESOLVE
from bot.shared import AWAIT_COMMENT, token_for
from const import RESOLVE_FLOW_TIMEOUT_S

logger = logging.getLogger("hermes")


async def _resolve_timeout(update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Conversation_timeout handler. Clears awaiting_comment_for so an
    abandoned 'add a comment' prompt can't swallow a later unrelated DM
    as a comment on a long-stale issue."""
    ctx.user_data.pop("awaiting_comment_for", None)
    return ConversationHandler.END


def _resolve_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(resolve_start, pattern=fr"^{RESOLVE}:")],
        states={
            AWAIT_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, resolve_comment)],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, _resolve_timeout)],
        },
        fallbacks=[CommandHandler("cancel", resolve_cancel)],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
        name="resolve",
        persistent=False,
        conversation_timeout=RESOLVE_FLOW_TIMEOUT_S,
    )


async def resolve_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    try:
        _, issue_id_s, choice = q.data.split(":")
        issue_id = int(issue_id_s)
    except (ValueError, AttributeError):
        await q.edit_message_text("Couldn't parse selection.")
        return ConversationHandler.END
    if choice == "skip":
        await q.edit_message_text("OK, leaving the issue open.")
        return ConversationHandler.END
    # Same identity gate as tickets.py: an unlinked (or decrypt-failed)
    # non-admin must never fall through to the admin API key. For admin,
    # token stays None on purpose (admin-key attribution).
    is_admin, token, decrypt_failed = await token_for(ctx, update.effective_user.id)
    if not is_admin and decrypt_failed:
        await q.edit_message_text(
            "Your Plex link can't be decrypted (the encryption key may have rotated). "
            "Run /unlink then /link to reconnect."
        )
        return ConversationHandler.END
    if not is_admin and token is None:
        await q.edit_message_text("DM me /link first so I can act on tickets as you.")
        return ConversationHandler.END
    if choice == "yes":
        seerr: SeerrClient = ctx.bot_data["seerr"]
        try:
            await seerr.resolve_issue(issue_id, as_plex_token=token)
        except Exception as exc:
            logger.exception("resolve_issue failed")
            await q.edit_message_text(f"Couldn't close issue #{issue_id}. {user_friendly_message(exc)}")
            return ConversationHandler.END
        await q.edit_message_text(f"✅ Issue #{issue_id} closed. Thanks!")
        return ConversationHandler.END
    # "no" -> ask for comment
    ctx.user_data["awaiting_comment_for"] = issue_id
    await q.edit_message_text(
        "Sorry it's still broken. What's still wrong? (Send a brief message; admin will see it on the issue.)"
    )
    return AWAIT_COMMENT


async def resolve_comment(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    issue_id = ctx.user_data.get("awaiting_comment_for")
    if not issue_id:
        return ConversationHandler.END
    comment = update.effective_message.text.strip()
    if not comment:
        await update.effective_message.reply_text("Empty message. Send a few words or /cancel.")
        return AWAIT_COMMENT
    seerr: SeerrClient = ctx.bot_data["seerr"]
    # Re-check identity at submit time: the link can vanish (/unlink, key
    # rotation) between the prompt and the reply, and the admin-key fallback
    # would misattribute the comment.
    is_admin, token, decrypt_failed = await token_for(ctx, update.effective_user.id)
    if not is_admin and (decrypt_failed or token is None):
        await update.effective_message.reply_text(
            "Your Plex link is gone or can't be decrypted. Run /link to reconnect, "
            "then add the comment from /tickets."
        )
        ctx.user_data.pop("awaiting_comment_for", None)
        return ConversationHandler.END
    try:
        await seerr.add_issue_comment(issue_id, comment, as_plex_token=token)
    except Exception as exc:
        logger.exception("add_issue_comment failed")
        await update.effective_message.reply_text(f"Couldn't add comment. {user_friendly_message(exc)}")
        ctx.user_data.pop("awaiting_comment_for", None)
        return ConversationHandler.END
    await update.effective_message.reply_text(
        f"💬 Added your comment to issue #{issue_id}. Admin will follow up."
    )
    ctx.user_data.pop("awaiting_comment_for", None)
    return ConversationHandler.END


async def resolve_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.pop("awaiting_comment_for", None)
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


