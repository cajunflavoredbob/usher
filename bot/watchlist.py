"""The "Plex watchlist" button on detail cards: a viewer with a Link adds the
title to their own Plex watchlist. The title is found on Plex Discover by its
tmdb:// guid (PlexClient.find_discover_key), then added with the viewer's own
stored Plex token. A title Plex has no match for is never guessed at."""
from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from http_util import user_friendly_message
from plex import PlexTokenRejectedError

from bot.callback_prefixes import PLEX_WATCHLIST_ADD
from bot.shared import DECRYPT_FAILED_MSG, prompt_plex_relink

logger = logging.getLogger("usher")

WATCHLIST_LABEL = "➕ Plex watchlist"
NOT_LINKED_TEXT = ("DM me /link first, so I can add titles to your own Plex "
                   "watchlist.")
_MEDIA_TYPES = ("movie", "tv")


def watchlist_button(media_type: str, tmdb_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        WATCHLIST_LABEL,
        callback_data=f"{PLEX_WATCHLIST_ADD}:{media_type}:{tmdb_id}")


async def watchlist_allowed(bot_data: dict, telegram_id: "int | None") -> bool:
    """Whether the viewer has a usable Link (the Admin too: the watchlist is
    their own, so they need their own Plex sign-in)."""
    if telegram_id is None:
        return False
    try:
        link = await bot_data["store"].get(telegram_id)
    except Exception:
        logger.warning("Watchlist link lookup failed", exc_info=True)
        return False
    return link is not None and bool(link.plex_token)


def _parse(data: str) -> "tuple[str, int] | None":
    try:
        _, media_type, tmdb_id_s = data.split(":")
        tmdb_id = int(tmdb_id_s)
    except (ValueError, AttributeError):
        return None
    if media_type not in _MEDIA_TYPES:
        return None
    return media_type, tmdb_id


async def watchlist_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """pwl:<media_type>:<tmdb_id>: answers go under the card as new
    messages (a card may be a photo, so it is never edited)."""
    q = update.callback_query
    parsed = _parse(q.data)
    await q.answer()
    if parsed is None:
        return
    media_type, tmdb_id = parsed
    link = await ctx.bot_data["store"].get(update.effective_user.id)
    if link is None or not link.plex_token:
        await q.message.reply_text(
            DECRYPT_FAILED_MSG if link is not None
            and link.plex_token_decrypt_failed else NOT_LINKED_TEXT)
        return
    busy: set = ctx.user_data.setdefault("watchlist_busy", set())
    if parsed in busy:
        return  # a second tap while the first is in flight
    busy.add(parsed)
    try:
        await _add(update, ctx, link.plex_token, media_type, tmdb_id)
    finally:
        busy.discard(parsed)


async def _add(update: Update, ctx: ContextTypes.DEFAULT_TYPE, token: str,
               media_type: str, tmdb_id: int) -> None:
    q = update.callback_query
    try:
        title, year = await ctx.bot_data["seerr"].get_media_title(
            media_type, tmdb_id)
    except Exception as exc:
        logger.warning("watchlist title lookup failed", exc_info=True)
        await q.message.reply_text(
            f"Couldn't add to your Plex watchlist. {user_friendly_message(exc)}")
        return
    label = html.escape(f"{title} ({year})" if year else title)
    plex = ctx.bot_data["plex"]
    try:
        key = await plex.find_discover_key(token, media_type, tmdb_id,
                                           title, year)
        if key is not None:
            await plex.add_to_watchlist(token, key)
    except PlexTokenRejectedError:
        await prompt_plex_relink(update, ctx, reply=True)
        return
    except Exception as exc:
        logger.warning("Plex watchlist add failed", exc_info=True)
        await q.message.reply_text(
            f"Couldn't add to your Plex watchlist. {user_friendly_message(exc)}")
        return
    if key is None:
        await q.message.reply_text(
            f"I couldn't find <b>{label}</b> on Plex's side, so nothing was "
            "added. You can add it in Plex: search for it in a Plex app and "
            "tap Watchlist.", parse_mode="HTML")
        return
    await q.message.reply_text(
        f"✅ Added <b>{label}</b> to your Plex watchlist.", parse_mode="HTML")
