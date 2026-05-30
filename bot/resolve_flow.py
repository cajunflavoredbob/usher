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
from store import UserStore

from bot.callback_prefixes import RESOLVE
from bot.shared import AWAIT_COMMENT

logger = logging.getLogger("hermes")

def _resolve_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(resolve_start, pattern=fr"^{RESOLVE}:")],
        states={
            AWAIT_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, resolve_comment)],
        },
        fallbacks=[CommandHandler("cancel", resolve_cancel)],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
        name="resolve",
        persistent=False,
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
    if choice == "yes":
        seerr: SeerrClient = ctx.bot_data["seerr"]
        store: UserStore = ctx.bot_data["store"]
        mapping = await store.get(update.effective_user.id)
        token = mapping.plex_token if (mapping and mapping.plex_token) else None
        try:
            await seerr.resolve_issue(issue_id, as_plex_token=token)
        except Exception as exc:
            logger.exception("resolve_issue failed")
            await q.edit_message_text(f"Couldn't close issue #{issue_id}. {user_friendly_message(exc)}")
            return ConversationHandler.END
        await q.edit_message_text(f"✅ Issue #{issue_id} closed. Thanks!")
        return ConversationHandler.END
    if choice == "skip":
        await q.edit_message_text("OK, leaving the issue open.")
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
    store: UserStore = ctx.bot_data["store"]
    seerr: SeerrClient = ctx.bot_data["seerr"]
    mapping = await store.get(update.effective_user.id)
    token = mapping.plex_token if (mapping and mapping.plex_token) else None
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


