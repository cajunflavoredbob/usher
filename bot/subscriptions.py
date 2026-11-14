"""Availability subscriptions: 🔔 Notify-me buttons on detail cards, the
/subscriptions list (also opened by the Requests Hub's "Subscriptions"
button, which edits it into the Hub message with a back row), and the
MEDIA_AVAILABLE fan-out consumer.

Strictly per-user: a user only ever sees their own list, unsubscribe only
works on their own rows, and nothing anywhere shows who else (or how many
others) subscribed to a title. Subscriptions are one-shot: consumed on the first
availability event so re-adds and auto-fix replacements can't re-fire."""
from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from seerr import SeerrClient
from store import UserStore

from bot.callback_prefixes import SUBSCRIPTION_DEL
from bot.hub import back_button, came_from_hub, end_markup, reply_end
from bot.requests_hub import REQUESTS_HUB
from bot.shared import (
    DECRYPT_FAILED_MSG,
    edit_or_send,
    force_end_flow_conversations,
    record_btn,
    require_seerr,
    token_for,
    watch_now_markup,
)

logger = logging.getLogger("usher")

# Leaves room under Telegram's 100-button keyboard limit for the back row,
# and keeps 60 long titles under the 4096-character message limit.
SUBSCRIPTIONS_SHOWN_MAX = 60


async def subscription_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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
    store: UserStore = ctx.bot_data["store"]
    # Answer BEFORE the Seerr title lookup: its retry chain can outlive the
    # callback query, and a late answer raises out of the handler. The row
    # is added first with a placeholder label, then given the real title.
    added = await store.add_subscription(user_id, media_type, tmdb_id,
                                         f"TMDb {tmdb_id}")
    if not added:
        await q.answer("Already on your list. See /subscriptions.")
        return
    await q.answer("🔔 Added to your Subscriptions.")
    seerr: SeerrClient = ctx.bot_data["seerr"]
    try:
        title, year = await seerr.get_media_title(media_type, tmdb_id)
    except Exception:
        return
    label = f"{title} ({year})" if year else title
    await store.set_subscription_label(user_id, media_type, tmdb_id, label)


async def cmd_subscriptions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await require_seerr(update, ctx) is None:
        return
    await _render_subscriptions(update, ctx, update.effective_message.reply_text,
                                user_id=update.effective_user.id)


async def subscriptions_hub(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Requests Hub "Subscriptions" button: the /subscriptions list, edited
    into the tapped Hub message."""
    q = update.callback_query
    await q.answer()
    force_end_flow_conversations(ctx, update)
    if await require_seerr(update, ctx,
                           reply_markup=end_markup(REQUESTS_HUB)) is None:
        return

    async def edit_into_hub(text, **kwargs):
        return await edit_or_send(q, text, **kwargs)

    await _render_subscriptions(update, ctx, edit_into_hub,
                                user_id=update.effective_user.id, hub=REQUESTS_HUB)


async def _render_subscriptions(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                                reply_method, *, user_id: int, hub=None) -> None:
    """With `hub` set the empty text carries the end keyboard and the list
    gets a last row holding the back button."""
    store: UserStore = ctx.bot_data["store"]
    subscriptions = await store.list_subscriptions(user_id)
    if not subscriptions:
        text = ("You have no Subscriptions. Tap 🔔 on a title's detail card "
                "(🖼 in /request or /trending) to get a DM when it becomes "
                "available.")
        await reply_end(ctx, reply_method, text, hub, user_id=user_id)
        return
    total = len(subscriptions)
    # Telegram caps a keyboard at 100 buttons and a message at 4096
    # characters; past that the send fails and the list can never be
    # trimmed. Show the first page and say how many more there are.
    subscriptions = subscriptions[:SUBSCRIPTIONS_SHOWN_MAX]
    lines = [f"🔔 Your Subscriptions ({total}):", ""]
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for n, (subscription_id, media_type, _tmdb_id, title) in enumerate(
            subscriptions, start=1):
        type_emoji = "🎬" if media_type == "movie" else "📺"
        lines.append(f"{n}. {type_emoji} {html.escape(title)}")
        row.append(InlineKeyboardButton(
            f"❌ {n}", callback_data=f"{SUBSCRIPTION_DEL}:{subscription_id}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if hub:
        rows.append([back_button(hub)])
    lines.append("")
    if total > len(subscriptions):
        lines.append(f"Showing the first {len(subscriptions)}. Remove some "
                     "to see the rest.")
    lines.append("❌ = unsubscribe")
    sent = await reply_method(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    record_btn(ctx.application, user_id, sent)


async def subscription_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    try:
        subscription_id = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        return
    user_id = update.effective_user.id
    store: UserStore = ctx.bot_data["store"]
    # Owner-scoped delete: a forged id belonging to someone else is a no-op.
    await store.remove_subscription(subscription_id, user_id)
    # Keep the back row when the tapped list carried it (opened from the Hub).
    hub = REQUESTS_HUB if came_from_hub(q.message, REQUESTS_HUB) else None
    await _render_subscriptions(update, ctx, q.edit_message_text, user_id=user_id,
                                hub=hub)


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
    markup = None
    if any(c not in already_notified for c in subscribers):
        markup = await watch_now_markup(app.bot_data.get("seerr"),
                                        media_type, tmdb_id)
    for chat_id in subscribers:
        if chat_id in already_notified:
            continue
        try:
            await app.bot.send_message(
                chat_id=chat_id, text=body, parse_mode="HTML",
                disable_web_page_preview=True, reply_markup=markup)
            logger.info("Notified subscriber telegram_id=%d of availability",
                        chat_id)
        except Exception as exc:
            logger.exception("Failed to DM subscriber telegram_id=%d", chat_id, exc_info=exc)
