"""Admin Broadcast: the Admin types a message in the Admin Hub, sees a preview
and sends it to every Linked user except the Admin. See the Broadcast entry in
GLOSSARY.md.

The text goes out as plain text under a "From the Admin" header, so markup in
it arrives literally. Sending is a job (`run_broadcast`): it walks the
broadcast's recipient rows in the store, one message every
BROADCAST_SEND_INTERVAL_S, and marks each as sent or failed as it goes, so a
restart resumes (`resume_broadcasts`) without repeating anyone (a kill between one send and its
mark can repeat that one DM). A user who
blocked the bot counts as failed. The preview message is edited into the
report, with the Admin Hub's back button."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, ConversationHandler, MessageHandler,
                          TypeHandler, filters)

from store import UserStore

from bot.admin_hub import ADMIN_HUB, refuse_outside_admin_dm
from bot.callback_prefixes import (BROADCAST_CANCEL, BROADCAST_SEND,
                                   BROADCAST_START)
from bot.hub import end_markup, set_hub_origin
from bot.shared import (BC_CONFIRM, BC_TEXT, edit_or_send,
                        force_end_flow_conversations, record_btn)
from const import (BROADCAST_FLOW_TIMEOUT_S, BROADCAST_MAX_CHARS,
                   BROADCAST_SEND_INTERVAL_S)

logger = logging.getLogger("usher")

HEADER = "📣 From the Admin"
_TEXT_KEY = "bc_text"
_BC_KEYS = (_TEXT_KEY,)
_RETRY_AFTER_TRIES = 3


def broadcast_body(text: str) -> str:
    return f"{HEADER}\n\n{text}"


def _preview_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📤 Send", callback_data=BROADCAST_SEND),
        InlineKeyboardButton("🛑 Cancel", callback_data=BROADCAST_CANCEL),
    ]])


def _clear(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    for key in _BC_KEYS:
        ctx.user_data.pop(key, None)


def _broadcast_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(broadcast_start,
                                 pattern=fr"^{BROADCAST_START}$"),
        ],
        states={
            BC_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               broadcast_text),
                MessageHandler(~filters.COMMAND, _expect_text),
            ],
            BC_CONFIRM: [
                CallbackQueryHandler(broadcast_send,
                                     pattern=fr"^{BROADCAST_SEND}$"),
                # Typing again replaces the draft.
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               broadcast_text),
            ],
            ConversationHandler.TIMEOUT: [TypeHandler(Update, _timeout)],
        },
        fallbacks=[
            CommandHandler("cancel", broadcast_cancel),
            CallbackQueryHandler(broadcast_cancel,
                                 pattern=fr"^{BROADCAST_CANCEL}$"),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
        name="broadcast",
        persistent=False,
        conversation_timeout=BROADCAST_FLOW_TIMEOUT_S,
    )


async def broadcast_start(update: Update,
                          ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Admin Hub "Broadcast": edit the Hub message into the text prompt.
    Admin and DM only."""
    if await refuse_outside_admin_dm(update, ctx):
        return ConversationHandler.END
    q = update.callback_query
    await q.answer()
    force_end_flow_conversations(ctx, update)
    set_hub_origin(ctx, ADMIN_HUB)
    _clear(ctx)
    await edit_or_send(
        q, "📣 What should I tell every Linked user? Type the message "
           "(plain text), or /cancel.")
    return BC_TEXT


async def _expect_text(update: Update,
                       ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "I can only send text: type the message, or /cancel.")
    return BC_TEXT


async def broadcast_text(update: Update,
                         ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """The typed text: show the preview with Send and Cancel."""
    if await refuse_outside_admin_dm(update, ctx):
        return ConversationHandler.END
    text = update.effective_message.text.strip()
    if not text:
        return await _expect_text(update, ctx)
    if len(text) > BROADCAST_MAX_CHARS:
        await update.effective_message.reply_text(
            f"That is {len(text)} characters; the limit is "
            f"{BROADCAST_MAX_CHARS}. Shorten it and send it again, or "
            "/cancel.")
        return BC_TEXT
    store: UserStore = ctx.bot_data["store"]
    admin_id = ctx.bot_data.get("admin_id")
    count = len([t for t in await store.list_linked_ids() if t != admin_id])
    ctx.user_data[_TEXT_KEY] = text
    sent = await update.effective_message.reply_text(
        f"Preview. This goes to {count} Linked user"
        f"{'' if count == 1 else 's'} (not you):\n\n{broadcast_body(text)}",
        reply_markup=_preview_markup())
    record_btn(ctx.application, update.effective_user.id, sent)
    return BC_CONFIRM


async def broadcast_cancel(update: Update,
                           ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel on the preview, or /cancel at any step: nothing is sent."""
    q = update.callback_query
    if q is not None:
        if await refuse_outside_admin_dm(update, ctx):
            return ConversationHandler.END
        await q.answer()
    _clear(ctx)
    markup = end_markup(ADMIN_HUB)
    if q is not None:
        sent = await edit_or_send(q, "Cancelled. Nothing was sent.",
                                  reply_markup=markup)
    else:
        sent = await update.effective_message.reply_text(
            "Cancelled. Nothing was sent.", reply_markup=markup)
    record_btn(ctx.application, update.effective_user.id, sent)
    return ConversationHandler.END


async def _timeout(update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    _clear(ctx)
    try:
        if update is not None and update.effective_chat is not None:
            sent = await ctx.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⏱️ The broadcast timed out. Nothing was sent.",
                reply_markup=end_markup(ADMIN_HUB, BROADCAST_START))
            record_btn(ctx.application, update.effective_user.id, sent)
    except Exception as exc:
        logger.debug("broadcast-timeout notice failed (non-fatal)",
                     exc_info=exc)
    return ConversationHandler.END


async def broadcast_send(update: Update,
                         ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Send tap: store the broadcast and its recipients, start the job and
    turn the preview into a "Sending" note the job's report later replaces."""
    if await refuse_outside_admin_dm(update, ctx):
        return ConversationHandler.END
    q = update.callback_query
    await q.answer()
    text = ctx.user_data.pop(_TEXT_KEY, None)
    markup = end_markup(ADMIN_HUB)
    if not text:
        sent = await edit_or_send(q, "That broadcast has ended. Start again "
                                     "from the Admin Hub.", reply_markup=markup)
        record_btn(ctx.application, update.effective_user.id, sent)
        return ConversationHandler.END
    store: UserStore = ctx.bot_data["store"]
    admin_id = ctx.bot_data.get("admin_id")
    recipients = [t for t in await store.list_linked_ids() if t != admin_id]
    if not recipients:
        sent = await edit_or_send(q, "No Linked users to send to.",
                                  reply_markup=markup)
        record_btn(ctx.application, update.effective_user.id, sent)
        return ConversationHandler.END
    shown = await edit_or_send(
        q, f"📣 Sending to {len(recipients)} Linked user"
           f"{'' if len(recipients) == 1 else 's'}…")
    # The report edits this message: take its id from what the edit (or its
    # fallback reply) returned.
    message_id = getattr(shown, "message_id", None)
    if message_id is None and q.message is not None:
        message_id = q.message.message_id
    broadcast_id = await store.create_broadcast(
        text, update.effective_chat.id, message_id, recipients)
    start_broadcast(ctx.application, broadcast_id)
    return ConversationHandler.END


def start_broadcast(app: Application, broadcast_id: int) -> None:
    """Run the job in the background. The task is kept in bot_data so it is
    not garbage collected and shutdown can cancel it."""
    tasks: set = app.bot_data.setdefault("broadcast_tasks", set())
    task = asyncio.ensure_future(run_broadcast(app, broadcast_id))
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def resume_broadcasts(app: Application) -> None:
    """At startup: restart every broadcast that was still sending. Recipients
    already marked sent or failed are skipped."""
    store: UserStore = app.bot_data["store"]
    try:
        ids = await store.unfinished_broadcast_ids()
    except Exception:
        logger.exception("couldn't list unfinished broadcasts")
        return
    for broadcast_id in ids:
        logger.info("Resuming broadcast %d", broadcast_id)
        start_broadcast(app, broadcast_id)


async def cancel_broadcast_tasks(app: Application) -> None:
    tasks = list(app.bot_data.get("broadcast_tasks") or ())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _send_one(bot, chat_id: int, body: str) -> bool:
    """One plain-text DM. True when it was delivered; False for any Telegram
    refusal (a user who blocked the bot is Forbidden). A flood-control wait is
    honoured and retried."""
    for _ in range(_RETRY_AFTER_TRIES):
        try:
            await bot.send_message(chat_id=chat_id, text=body)
            return True
        except RetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
        except TelegramError as exc:
            logger.info("broadcast DM to %d failed: %s", chat_id, exc)
            return False
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("broadcast DM to %d failed", chat_id)
            return False
    return False


async def run_broadcast(app: Application, broadcast_id: int) -> None:
    """Send to every pending recipient, spaced BROADCAST_SEND_INTERVAL_S
    apart, marking each as it goes; then report. One run per broadcast."""
    running: set = app.bot_data.setdefault("broadcasts_running", set())
    if broadcast_id in running:
        return
    running.add(broadcast_id)
    try:
        store: UserStore = app.bot_data["store"]
        broadcast = await store.get_broadcast(broadcast_id)
        if broadcast is None or broadcast.status != "sending":
            return
        body = broadcast_body(broadcast.text)
        while True:
            batch = await store.pending_broadcast_recipients(broadcast_id)
            if not batch:
                break
            for telegram_id in batch:
                ok = await _send_one(app.bot, telegram_id, body)
                await store.mark_broadcast_recipient(
                    broadcast_id, telegram_id, "sent" if ok else "failed")
                await asyncio.sleep(BROADCAST_SEND_INTERVAL_S)
        await _report(app, broadcast)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("broadcast %d stopped on an error", broadcast_id)
    finally:
        running.discard(broadcast_id)


def report_text(sent: int, failed: int) -> str:
    lines = ["📣 Broadcast done.", f"Sent: {sent}", f"Couldn't reach: {failed}"]
    if failed:
        lines.append("(They may have blocked the bot.)")
    return "\n".join(lines)


async def _report(app: Application, broadcast) -> None:
    """Edit the preview message into the counts, with the Admin Hub's back
    button; send a new message when it can't be edited."""
    store: UserStore = app.bot_data["store"]
    if not await store.finish_broadcast(broadcast.id):
        return
    counts = await store.broadcast_counts(broadcast.id)
    text = report_text(counts["sent"], counts["failed"])
    markup = end_markup(ADMIN_HUB)
    sent: Optional[object] = None
    try:
        if broadcast.message_id is not None:
            sent = await app.bot.edit_message_text(
                chat_id=broadcast.chat_id, message_id=broadcast.message_id,
                text=text, reply_markup=markup)
    except TelegramError as exc:
        logger.info("couldn't edit the broadcast preview: %s", exc)
    try:
        if sent is None:
            sent = await app.bot.send_message(
                chat_id=broadcast.chat_id, text=text, reply_markup=markup)
        record_btn(app, broadcast.chat_id, sent)
    except TelegramError as exc:
        logger.warning("couldn't send the broadcast report: %s", exc)
