"""Detail-card rendering for a MediaResult: caption + poster + keyboard.

Shared by the /request search flow and the /trending browse so the card
looks identical everywhere. Cards are info-first messages sent alongside a
pick list (never edits of it) and are deliberately NOT recorded in the
button-gate history; their Dismiss callback is gate-exempt."""
from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from seerr import (
    MEDIA_STATUS_AVAILABLE,
    MEDIA_STATUS_BLOCKLISTED,
    MEDIA_STATUS_PARTIALLY_AVAILABLE,
    MEDIA_STATUS_PENDING,
    MEDIA_STATUS_PROCESSING,
    MediaResult,
)

from bot.callback_prefixes import DV_SIMILAR, RQ_INFO_DISMISS, SUBSCRIPTION_ADD
from bot.follows import follow_button, follow_state
from bot.remaining import remaining_button, requestable_seasons
from bot.shared import record_card_owner
from bot.watchlist import watchlist_allowed, watchlist_button
from const import DETAIL_OVERVIEW_MAX_CHARS

logger = logging.getLogger("usher")

TMDB_POSTER_BASE = "https://image.tmdb.org/t/p/w342"

# Availability annotations, shared wording with the pick lists.
STATUS_NOTES = {
    MEDIA_STATUS_PENDING: "⏳ requested",
    MEDIA_STATUS_PROCESSING: "⏳ downloading",
    MEDIA_STATUS_PARTIALLY_AVAILABLE: "🌗 partly available",
    MEDIA_STATUS_AVAILABLE: "✅ available",
    MEDIA_STATUS_BLOCKLISTED: "🚫 not requestable",
}


def build_card_caption(r: MediaResult) -> str:
    """HTML caption: title (year), facts line, truncated synopsis."""
    type_label = "Movie" if r.media_type == "movie" else "Series"
    header = f"<b>{html.escape(r.title)}</b>"
    if r.year:
        header += f" ({r.year})"
    facts = [type_label]
    if r.vote_average:
        facts.insert(0, f"★ {r.vote_average:.1f}")
    note = STATUS_NOTES.get(r.status or 0)
    if note:
        facts.append(note)
    lines = [header, " · ".join(facts)]
    overview = (r.overview or "").strip()
    if overview:
        if len(overview) > DETAIL_OVERVIEW_MAX_CHARS:
            cut = overview.rfind(" ", 0, DETAIL_OVERVIEW_MAX_CHARS)
            overview = overview[:cut if cut > 0 else DETAIL_OVERVIEW_MAX_CHARS] + "…"
        lines.append(f"<i>{html.escape(overview)}</i>")
    return "\n".join(lines)


def card_keyboard(r: MediaResult,
                  following: "bool | None" = None,
                  remaining: bool = False,
                  watchlist: bool = False) -> InlineKeyboardMarkup:
    """Dismiss always; 🔔 Notify-me only when availability could still
    change (an already-available or blocklisted title would never fire);
    Follow or Unfollow for a show when `following` is given (None = the
    viewer may not follow, so no button); Request remaining seasons for a
    show when `remaining` says a season is left to request; Plex watchlist
    when `watchlist` says the viewer has a Link; More like this always."""
    rows = []
    if (r.status or 0) not in (MEDIA_STATUS_AVAILABLE, MEDIA_STATUS_BLOCKLISTED):
        rows.append([InlineKeyboardButton(
            "🔔 Notify me when available",
            callback_data=f"{SUBSCRIPTION_ADD}:{r.media_type}:{r.tmdb_id}")])
    if r.media_type == "tv" and following is not None:
        rows.append([follow_button(r.tmdb_id, following)])
    if r.media_type == "tv" and remaining:
        rows.append([remaining_button(r.tmdb_id)])
    if watchlist:
        rows.append([watchlist_button(r.media_type, r.tmdb_id)])
    rows.append([InlineKeyboardButton(
        "🔁 More like this",
        callback_data=f"{DV_SIMILAR}:{r.media_type}:{r.tmdb_id}")])
    rows.append([InlineKeyboardButton("🗑 Dismiss", callback_data=RQ_INFO_DISMISS)])
    return InlineKeyboardMarkup(rows)


async def send_detail_card(ctx, chat_id: int, r: MediaResult,
                           owner_id: "int | None" = None) -> None:
    """Send the card as a photo when a poster exists, text otherwise (a dead
    poster URL degrades to the text card, never a dead tap). `owner_id` is
    the user the card is for: in a group the button gate only lets that user
    tap it (cards are not in the button history)."""
    caption = build_card_caption(r)
    following = None
    remaining = False
    if r.media_type == "tv":
        following = await follow_state(ctx.bot_data, chat_id, r.tmdb_id)
        # Only a Linked user (or the Admin) may request, so only they get it.
        if following is not None:
            left = await requestable_seasons(ctx.bot_data["seerr"], r.tmdb_id)
            remaining = bool(left)
    watchlist = await watchlist_allowed(
        ctx.bot_data, owner_id if owner_id is not None else chat_id)
    kb = card_keyboard(r, following, remaining, watchlist)
    if r.poster_path:
        try:
            sent = await ctx.bot.send_photo(
                chat_id=chat_id,
                photo=f"{TMDB_POSTER_BASE}{r.poster_path}",
                caption=caption,
                parse_mode="HTML",
                reply_markup=kb,
            )
            record_card_owner(ctx.application, owner_id, sent)
            return
        except Exception:
            logger.debug("poster send failed; falling back to text card",
                         exc_info=True)
    sent = await ctx.bot.send_message(
        chat_id=chat_id,
        text=caption,
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=True,
    )
    record_card_owner(ctx.application, owner_id, sent)
