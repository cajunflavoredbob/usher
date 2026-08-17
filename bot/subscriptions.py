"""Availability subscriptions: 🔔 Notify-me buttons on detail cards, the
/subscriptions list, and the MEDIA_AVAILABLE fan-out consumer.

Strictly per-user: a user only ever sees their own list, unsubscribe only
works on their own rows, and nothing anywhere shows who else (or how many
others) watch a title. Subscriptions are one-shot: consumed on the first
availability event so re-adds and auto-fix replacements can't re-fire."""
from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from seerr import SeerrClient
from store import UserStore

from bot.callback_prefixes import SUB_DEL
from bot.shared import (
    DECRYPT_FAILED_MSG,
    record_btn,
    require_seerr,
    send_typing,
    token_for,
)

logger = logging.getLogger("usher")


async def sub_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """🔔 tap on a detail card. Linked users (and the admin) only: the card
    itself is visible to anyone, but standing notifications are an
    account-holder feature and an unlinked drive-by shouldn't grow the
    table."""
    q = update.callback_query
    try:
        _, media_type, tmdb_id_s = q.data.split(":")
        tmdb_id = int(tmdb_id_s)
    except (ValueError, IndexError):
        await q.answer()
        return
    user_id = update.effective_user.id
    is_admin, token, decrypt_failed = await token_for(ctx, user_id)
    if not is_admin and not token:
        await q.answer(DECRYPT_FAILED_MSG if decrypt_failed
                       else "DM me /link first so I know who to notify.",
                       show_alert=True)
        return
    seerr: SeerrClient = ctx.bot_data["seerr"]
    try:
        title, year = await seerr.get_media_title(media_type, tmdb_id)
        label = f"{title} ({year})" if year else title
    except Exception:
        label = f"TMDb {tmdb_id}"
    store: UserStore = ctx.bot_data["store"]
    added = await store.add_subscription(user_id, media_type, tmdb_id, label)
    if added:
        await q.answer(f"🔔 You'll be DMed when it's available.")
    else:
        await q.answer("Already on your list — see /subscriptions.")


async def cmd_subscriptions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await require_seerr(update, ctx) is None:
        return
    await _render_subs(update, ctx, update.effective_message.reply_text,
                       user_id=update.effective_user.id)


async def _render_subs(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                       reply_method, *, user_id: int) -> None:
    store: UserStore = ctx.bot_data["store"]
    subs = await store.list_subscriptions(user_id)
    if not subs:
        await reply_method(
            "You're not watching anything. Tap 🔔 on a title's detail card "
            "(🖼 in /request or /trending) to get a DM when it becomes "
            "available.")
        return
    lines = [f"🔔 Your availability watches ({len(subs)}):", ""]
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for n, (sub_id, media_type, _tmdb_id, title) in enumerate(subs, start=1):
        type_emoji = "🎬" if media_type == "movie" else "📺"
        lines.append(f"{n}. {type_emoji} {html.escape(title)}")
        row.append(InlineKeyboardButton(
            f"❌ {n}", callback_data=f"{SUB_DEL}:{sub_id}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    lines.append("")
    lines.append("❌ = stop watching")
    sent = await reply_method(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    record_btn(ctx.application, user_id, sent)


async def sub_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    try:
        sub_id = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        return
    user_id = update.effective_user.id
    store: UserStore = ctx.bot_data["store"]
    # Owner-scoped delete: a forged id belonging to someone else is a no-op.
    await store.remove_subscription(sub_id, user_id)
    await _render_subs(update, ctx, q.edit_message_text, user_id=user_id)


async def fan_out_availability(app, media_type: str, tmdb_id: int,
                               title_line: str,
                               already_notified: set[int]) -> None:
    """DM every subscriber of a now-available title (one-shot: rows are
    consumed atomically). Called from the MEDIA_AVAILABLE webhook handler;
    `already_notified` suppresses a double DM to the requester."""
    if not app.bot_data["settings_store"].settings.tg_notify_subscriptions:
        # Silenced class: leave the rows unconsumed so re-enabling the
        # toggle lets a later availability event deliver them.
        return
    store: UserStore = app.bot_data["store"]
    try:
        subscribers = await store.pop_subscribers(media_type, tmdb_id)
    except Exception:
        logger.exception("subscription fan-out lookup failed")
        return
    body = "🔔 Now available in Plex!"
    if title_line:
        body += f"\n{html.escape(title_line)}"
    for chat_id in subscribers:
        if chat_id in already_notified:
            continue
        try:
            await app.bot.send_message(
                chat_id=chat_id, text=body, parse_mode="HTML",
                disable_web_page_preview=True)
            logger.info("Notified subscriber telegram_id=%d of availability",
                        chat_id)
        except Exception:
            logger.exception("Failed to DM subscriber telegram_id=%d", chat_id)
