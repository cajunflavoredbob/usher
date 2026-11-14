"""Follows: a standing ask to be told about each new episode of a show (a
Subscription is one-shot, see bot.subscriptions).

Holds the Follow / Unfollow buttons, their tap handlers, the Requests Hub's
"Follows" list, and the helper that builds the keyboard of an availability
notice (Watch now plus Follow for a show). Strictly per user, like
Subscriptions. Linked users and the Admin only."""
from __future__ import annotations

import html
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.callback_prefixes import FOLLOW_ADD, FOLLOW_DEL, FOLLOW_REMOVE
from bot.hub import back_button, end_markup, reply_end
from bot.requests_hub import REQUESTS_HUB
from bot.shared import (
    DECRYPT_FAILED_MSG,
    edit_or_send,
    force_end_flow_conversations,
    record_btn,
    require_seerr,
    token_for,
)

logger = logging.getLogger("usher")

# Same cap and reasoning as the Subscriptions list.
FOLLOWS_SHOWN_MAX = 60


def follow_button(tmdb_id: int, following: bool) -> InlineKeyboardButton:
    if following:
        return InlineKeyboardButton("🔕 Unfollow",
                                    callback_data=f"{FOLLOW_DEL}:{tmdb_id}")
    return InlineKeyboardButton("➕ Follow",
                                callback_data=f"{FOLLOW_ADD}:{tmdb_id}")


async def follow_state(bot_data: dict, telegram_id: int,
                       tmdb_id: int) -> Optional[bool]:
    """Whether `telegram_id` follows the show: None when they may not (not
    Linked and not the Admin), so no button is offered."""
    try:
        if telegram_id != bot_data.get("admin_id"):
            link = await bot_data["store"].get(telegram_id)
            if link is None or not link.plex_token:
                return None
        return bool(await bot_data["store"].is_following(telegram_id, tmdb_id))
    except Exception:
        logger.warning("Follow state lookup failed", exc_info=True)
        return None


async def notice_markup(bot_data: dict, media_type: str, tmdb_id: int,
                        chat_id: int, watch_button) -> Optional[InlineKeyboardMarkup]:
    """Keyboard of an availability notice: Watch now when `watch_button` is
    given, and Follow or Unfollow beside it for a show."""
    row = [watch_button] if watch_button else []
    if media_type == "tv" and tmdb_id:
        state = await follow_state(bot_data, chat_id, tmdb_id)
        if state is not None:
            row.append(follow_button(tmdb_id, state))
    return InlineKeyboardMarkup([row]) if row else None


async def _swap_button(q, tmdb_id: int, following: bool) -> None:
    """Replace the tapped message's Follow or Unfollow button for this show
    with the opposite one, leaving every other button alone."""
    markup = getattr(q.message, "reply_markup", None)
    if markup is None:
        return
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for btn in row:
            data = getattr(btn, "callback_data", None)
            if data in (f"{FOLLOW_ADD}:{tmdb_id}", f"{FOLLOW_DEL}:{tmdb_id}"):
                btn = follow_button(tmdb_id, following)
            new_row.append(btn)
        rows.append(new_row)
    try:
        await q.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup(rows))
    except Exception:
        logger.debug("Follow button swap failed", exc_info=True)


def _tmdb_from(q) -> Optional[int]:
    try:
        return int(q.data.split(":")[1])
    except (ValueError, IndexError, AttributeError):
        return None


async def follow_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Follow tap on a detail card or availability notice."""
    q = update.callback_query
    tmdb_id = _tmdb_from(q)
    if tmdb_id is None:
        await q.answer()
        return
    user_id = update.effective_user.id
    is_admin, token, decrypt_failed = await token_for(ctx, user_id)
    if not is_admin and not token:
        await q.answer(DECRYPT_FAILED_MSG if decrypt_failed
                       else "DM me /link first so I know who to ping.",
                       show_alert=True)
        return
    store = ctx.bot_data["store"]
    # Answer before the lookups: the Seerr retry chain can outlive the
    # callback query. The row goes in first with a placeholder title.
    added = await store.add_follow(user_id, tmdb_id, 0, f"TMDb {tmdb_id}")
    await q.answer("🔔 Following. I'll ping you for each new episode."
                   if added else "Already following.")
    await _swap_button(q, tmdb_id, True)
    if not added:
        return
    seerr = ctx.bot_data["seerr"]
    try:
        title, year = await seerr.get_media_title("tv", tmdb_id)
        _seasons, tvdb_id = await seerr.get_tv_seasons(tmdb_id)
    except Exception:
        logger.warning("Follow title lookup failed (tmdb=%s)", tmdb_id,
                       exc_info=True)
        return
    label = f"{title} ({year})" if year else title
    await store.set_follow_info(user_id, tmdb_id, tvdb_id or 0, label)


async def follow_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Unfollow tap on a detail card, notice or Follow ping."""
    q = update.callback_query
    tmdb_id = _tmdb_from(q)
    if tmdb_id is None:
        await q.answer()
        return
    removed = await ctx.bot_data["store"].remove_follow(
        update.effective_user.id, tmdb_id)
    await q.answer("🔕 Unfollowed." if removed else "You weren't following it.")
    await _swap_button(q, tmdb_id, False)


async def follows_hub(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Requests Hub "Follows" button: the list, edited into the Hub
    message with a back row."""
    q = update.callback_query
    await q.answer()
    force_end_flow_conversations(ctx, update)
    if await require_seerr(update, ctx,
                           reply_markup=end_markup(REQUESTS_HUB)) is None:
        return

    async def edit_into_hub(text, **kwargs):
        return await edit_or_send(q, text, **kwargs)

    await _render_follows(update, ctx, edit_into_hub,
                          user_id=update.effective_user.id)


async def _render_follows(update, ctx, reply_method, *, user_id: int) -> None:
    store = ctx.bot_data["store"]
    follows = await store.list_follows(user_id)
    if not follows:
        text = ("You aren't following any shows. Tap ➕ Follow on a show's "
                "detail card to get a DM for each new episode.")
        await reply_end(ctx, reply_method, text, REQUESTS_HUB, user_id=user_id)
        return
    total = len(follows)
    follows = follows[:FOLLOWS_SHOWN_MAX]
    lines = [f"🔔 Shows you follow ({total}):", ""]
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for n, (follow_id, _tmdb_id, title) in enumerate(follows, start=1):
        lines.append(f"{n}. 📺 {html.escape(title)}")
        row.append(InlineKeyboardButton(
            f"🔕 {n}", callback_data=f"{FOLLOW_REMOVE}:{follow_id}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([back_button(REQUESTS_HUB)])
    lines.append("")
    if total > len(follows):
        lines.append(f"Showing the first {len(follows)}. Unfollow some "
                     "to see the rest.")
    lines.append("🔕 = unfollow")
    sent = await reply_method(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    record_btn(ctx.application, user_id, sent)


async def follow_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """🔕 n on the Follows list: Unfollow that row, then re-render."""
    q = update.callback_query
    await q.answer()
    try:
        follow_id = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        return
    user_id = update.effective_user.id
    # Owner-scoped: a forged id belonging to someone else is a no-op.
    await ctx.bot_data["store"].remove_follow_by_id(follow_id, user_id)
    await _render_follows(update, ctx, q.edit_message_text, user_id=user_id)
