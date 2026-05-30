"""Plex /link flow + /unlink command."""
from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Optional

from telegram import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from http_util import user_friendly_message
from plex import PlexClient
from seerr import SeerrClient
from store import UserStore

from bot.callback_prefixes import LINK_CONSENT, LINK_HELP, LINK_PLATFORM
from bot.shared import (
    AWAIT_LINK_CONSENT,
    AWAIT_PLATFORM_CHOICE,
    _record_btn,
    _require_seerr,
)
from const import (
    LINK_FLOW_TIMEOUT_S,
    PLEX_POLL_FAILURE_WARN_THRESHOLD,
    PLEX_POLL_INTERVAL_S,
    PLEX_POLL_MAX_BACKOFF_S,
    PLEX_STRONG_PIN_MAX_ITERS,
    PLEX_WEAK_PIN_MAX_ITERS,
)

logger = logging.getLogger("hermes")

async def _link_timeout(update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Conversation_timeout handler. Clears link_active_loop so an abandoned
    link flow doesn't keep an orphaned poll alive in user_data."""
    ctx.user_data.pop("link_active_loop", None)
    return ConversationHandler.END


def _link_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("link", cmd_link)],
        states={
            AWAIT_LINK_CONSENT: [CallbackQueryHandler(cmd_link_consent, pattern=fr"^{LINK_CONSENT}:")],
            AWAIT_PLATFORM_CHOICE: [CallbackQueryHandler(cmd_link_platform, pattern=fr"^{LINK_PLATFORM}:")],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, _link_timeout)],
        },
        fallbacks=[CommandHandler("cancel", link_cancel)],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
        name="link",
        persistent=False,
        conversation_timeout=LINK_FLOW_TIMEOUT_S,  # 30 min covers the 28-min strong-PIN window
    )


async def cmd_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    if msg.chat.type != ChatType.PRIVATE:
        await msg.reply_text("Please DM me to link your account.")
        return ConversationHandler.END
    if await _require_seerr(update, ctx) is None:
        return ConversationHandler.END
    rows = [[
        InlineKeyboardButton("✅ Yes, continue", callback_data=f"{LINK_CONSENT}:yes"),
        InlineKeyboardButton("🛑 Cancel", callback_data=f"{LINK_CONSENT}:no"),
    ]]
    await msg.reply_text(
        "Sign in with Plex so issues you submit are tagged as you.\n\n"
        "You can use /unlink anytime to remove access.\n\n"
        "Continue?",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return AWAIT_LINK_CONSENT


async def cmd_link_consent(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    choice = q.data.split(":")[1]
    if choice == "no":
        await q.edit_message_text("Cancelled. /link to try again later.")
        return ConversationHandler.END
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💻 Desktop", callback_data=f"{LINK_PLATFORM}:desktop"),
        InlineKeyboardButton("📱 iOS / Android", callback_data=f"{LINK_PLATFORM}:mobile"),
    ]])
    await q.edit_message_text(
        "Where are you using Telegram?",
        reply_markup=kb,
    )
    return AWAIT_PLATFORM_CHOICE


async def _poll_with_cancel(plex: PlexClient, pin_id: int, max_iters: int,
                            ctx: ContextTypes.DEFAULT_TYPE, loop_id: str,
                            chat_id: Optional[int] = None) -> Optional[str]:
    """Poll Plex until a token is returned, this loop is superseded by a newer
    one (link_active_loop changes), or max_iters is exhausted. The loop_id
    pattern is race-free: a new loop overwrites link_active_loop, and any
    older loops bail on their next check. No reset is needed.

    On consecutive failures (Plex API down), backs off from 3s -> 6s -> 12s
    and DMs the user once after 5 in a row so they don't think the bot is
    silently broken.
    """
    consecutive_failures = 0
    warned_user = False
    for _ in range(max_iters):
        if ctx.user_data.get("link_active_loop") != loop_id:
            return None
        sleep_s = min(
            PLEX_POLL_MAX_BACKOFF_S,
            PLEX_POLL_INTERVAL_S * (2 ** min(consecutive_failures, 2)),
        )
        await asyncio.sleep(sleep_s)
        try:
            token = await plex.poll_pin(pin_id)
            consecutive_failures = 0
            if token:
                if ctx.user_data.get("link_active_loop") != loop_id:
                    return None
                return token
        except Exception:
            consecutive_failures += 1
            logger.exception("poll_pin failed (will retry; %d in a row)",
                             consecutive_failures)
            if (consecutive_failures == PLEX_POLL_FAILURE_WARN_THRESHOLD
                    and not warned_user and chat_id is not None):
                warned_user = True
                try:
                    await ctx.bot.send_message(
                        chat_id=chat_id,
                        text=("⚠️ Plex's API isn't responding right now. Still trying — "
                              "I'll let you know if a token comes through. If you've "
                              "already approved in Plex, sit tight."),
                    )
                except Exception:
                    logger.exception("couldn't send Plex-down warning to %s", chat_id)
    return None


async def _finalize_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                         auth_token: str) -> None:
    """Shared post-auth path: Seerr login + persist mapping + success/failure reply."""
    plex: PlexClient = ctx.bot_data["plex"]
    seerr: SeerrClient = ctx.bot_data["seerr"]
    store: UserStore = ctx.bot_data["store"]
    chat_id = update.effective_chat.id
    tg_id = update.effective_user.id

    try:
        seerr_id, display, _ = await seerr.login_with_plex(auth_token)
    except Exception as exc:
        logger.exception("seerr login_with_plex failed")
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=(
                "✓ Plex authorized you, but Seerr rejected the sign-in.\n"
                f"{user_friendly_message(exc)}\n\n"
                "Your Plex account probably isn't shared in Seerr yet. "
                "Ask the admin to invite you."
            ),
        )
        return

    try:
        plex_user = await plex.get_user(auth_token)
    except Exception:
        logger.exception("plex get_user failed")
        plex_user = None

    await store.link_with_plex(
        telegram_id=tg_id,
        seerr_id=seerr_id,
        seerr_display=display,
        plex_token=auth_token,
        plex_uuid=plex_user.uuid if plex_user else "",
        plex_username=plex_user.username if plex_user else display,
    )

    await ctx.bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ Linked as *{display}*.\n\n"
            "You can now /issue and /tickets."
        ),
        parse_mode="Markdown",
    )


async def cmd_link_platform(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the platform-choice tap: issues a strong PIN and starts polling."""
    q = update.callback_query
    await q.answer()
    platform = q.data.split(":")[1]  # "desktop" or "mobile"
    plex: PlexClient = ctx.bot_data["plex"]
    try:
        pin = await plex.request_pin(strong=True)
    except Exception as exc:
        logger.exception("plex request_pin failed")
        await q.edit_message_text(f"Couldn't start Plex auth. {user_friendly_message(exc)}")
        return ConversationHandler.END

    loop_id = secrets.token_hex(8)
    ctx.user_data["link_active_loop"] = loop_id

    if platform == "desktop":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Open Plex authorization", url=pin.auth_url)],
            [InlineKeyboardButton("❌ Having trouble?", callback_data=LINK_HELP)],
        ])
        text = "Authorize Hermes in Plex:\n\nSign in and tap Allow."
    else:  # mobile
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Copy auth link",
                                  copy_text=CopyTextButton(text=pin.auth_url))],
            [InlineKeyboardButton("❌ Didn't work?", callback_data=LINK_HELP)],
        ])
        text = "Tap to copy the auth link, then paste it into a browser."

    await q.edit_message_text(text, reply_markup=kb)

    # Strong PIN window: under the 30-min lifetime; see const.py.
    auth_token = await _poll_with_cancel(plex, pin.id,
                                         max_iters=PLEX_STRONG_PIN_MAX_ITERS,
                                         ctx=ctx, loop_id=loop_id,
                                         chat_id=update.effective_chat.id)
    if auth_token is None:
        if ctx.user_data.get("link_active_loop") != loop_id:
            # A newer loop (didnt_work fallback) has taken ownership; exit silently
            return ConversationHandler.END
        await q.edit_message_text("⏱️ Plex auth timed out. /link to try again.")
        return ConversationHandler.END

    await _finalize_link(update, ctx, auth_token)
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    return ConversationHandler.END


async def cmd_link_didnt_work(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles 'Having trouble?' / 'Didn't work?' button. Fires OUTSIDE the
    conversation so it can interrupt an in-progress poll. Issues a weak PIN
    and starts a fresh poll loop."""
    q = update.callback_query
    await q.answer()

    plex: PlexClient = ctx.bot_data["plex"]
    try:
        pin = await plex.request_pin(strong=False)
    except Exception as exc:
        logger.exception("plex request_pin failed (fallback)")
        await q.edit_message_text(f"Couldn't get a fresh code. {user_friendly_message(exc)}")
        return

    # Claim a new loop ID -- any prior poll will see this and exit on its
    # next check. No race because there's no reset-after-set sequence.
    loop_id = secrets.token_hex(8)
    ctx.user_data["link_active_loop"] = loop_id

    code = pin.code.upper()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Copy plex.tv/link",
                             copy_text=CopyTextButton(text="https://plex.tv/link")),
    ]])
    # Tell the user to tap the copy button. Don't mention plex.tv/link in the
    # body text -- Telegram would auto-link it AND show a preview card. The
    # trailing line gives breathing room above the inline button.
    await q.edit_message_text(
        f"Enter this code in the page from the button below:\n\n"
        f"*{code}*\n\n"
        f"Code expires in 15 minutes.",
        reply_markup=kb,
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )

    # Weak PIN window: under the 15-min lifetime; see const.py.
    auth_token = await _poll_with_cancel(plex, pin.id,
                                         max_iters=PLEX_WEAK_PIN_MAX_ITERS,
                                         ctx=ctx, loop_id=loop_id,
                                         chat_id=update.effective_chat.id)
    if auth_token is None:
        if ctx.user_data.get("link_active_loop") != loop_id:
            return  # superseded by another loop
        # Clear the loop key explicitly so the parent /link conversation
        # doesn't think it's still polling (audit ERR #19).
        ctx.user_data.pop("link_active_loop", None)
        await q.edit_message_text("⏱️ Plex auth timed out. /link to try again.")
        return

    await _finalize_link(update, ctx, auth_token)
    # Successful link -- clear the loop key so a later /link is a fresh start.
    ctx.user_data.pop("link_active_loop", None)
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


async def link_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Cancelled. /link to try again later.")
    return ConversationHandler.END


async def cmd_unlink(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    store: UserStore = ctx.bot_data["store"]
    removed = await store.unlink(update.effective_user.id)
    if removed:
        await update.effective_message.reply_text(
            "🔓 Unlinked. I've removed your Plex token from my storage.\n\n"
            "For extra safety, you can also remove 'Hermes' from your Plex "
            "authorized devices at app.plex.tv → Settings → Authorized Devices."
        )
    else:
        await update.effective_message.reply_text("You weren't linked.")


