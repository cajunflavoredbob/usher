"""/mystats: a Linked user's own Tautulli watch stats (plays and watch time
for 7 and 30 days, most watched titles). The Trending list's "My stats"
button opens the same text, edited into the Hub message when the list came
from the Requests Hub (keeping its back row). Without Tautulli linked the
command says so and the button never shows."""
from __future__ import annotations

import logging
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from http_util import user_friendly_message
from tautulli import TautulliClient, find_user_by_plex_username

from bot.callback_prefixes import MYSTATS
from bot.hub import came_from_hub, end_markup
from bot.requests_hub import REQUESTS_HUB
from bot.shared import DECRYPT_FAILED_MSG, edit_or_send, record_btn, send_typing

logger = logging.getLogger("usher")

TOP_TITLES = 5
NOT_LINKED_MSG = "DM me /link first so I know which Plex account is yours."
UNKNOWN_USER_MSG = ("Tautulli doesn't know you yet. Watch something on Plex "
                    "and check back.")
NOT_CONFIGURED_MSG = "Watch stats aren't available on this server."


def format_watch_time(seconds) -> str:
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        total = 0
    minutes = total // 60
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _plays(n) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        n = 0
    return f"{n} play" + ("" if n == 1 else "s")


def _top_lines(blocks: list, stat_id: str) -> list[str]:
    for b in blocks:
        if b.get("stat_id") == stat_id:
            rows = [r for r in (b.get("rows") or []) if isinstance(r, dict)
                    and r.get("title")]
            return [f"{i}. {r['title']} ({_plays(r.get('total_plays'))})"
                    for i, r in enumerate(rows[:TOP_TITLES], 1)]
    return []


async def build_mystats(ctx: ContextTypes.DEFAULT_TYPE, user_id: int
                        ) -> tuple[str, bool]:
    """(text, failed) for Telegram user `user_id`: a plain message for every
    non-happy case; `failed` is True when a Tautulli call failed (the Hub
    button then offers Try again)."""
    client: Optional[TautulliClient] = ctx.bot_data.get("tautulli")
    if client is None:
        return NOT_CONFIGURED_MSG, False
    link = await ctx.bot_data["store"].get(user_id)
    if link and link.plex_token_decrypt_failed:
        return DECRYPT_FAILED_MSG, False
    if not link or not (link.plex_username or "").strip():
        return NOT_LINKED_MSG, False
    try:
        user = await find_user_by_plex_username(client, link.plex_username)
        if user is None or user.get("user_id") is None:
            return UNKNOWN_USER_MSG, False
        tid = user["user_id"]
        windows = await client.get_user_watch_time_stats(tid, (7, 30))
        blocks: list = []
        titles_failed = False
        for stat_id in ("top_movies", "top_tv"):
            try:
                blocks += await client.get_home_stats(
                    user_id=tid, time_range=30, stats_count=TOP_TITLES,
                    stat_id=stat_id)
            except Exception:
                logger.warning("Tautulli %s failed", stat_id, exc_info=True)
                titles_failed = True
    except Exception as exc:
        logger.exception("mystats failed")
        return (f"Couldn't fetch your stats. {user_friendly_message(exc)}",
                True)

    lines = ["📊 Your watch stats", ""]
    by_days = {str(w.get("query_days")): w for w in windows}
    for days in ("7", "30"):
        w = by_days.get(days) or {}
        lines.append(f"Last {days} days: {_plays(w.get('total_plays'))}, "
                     f"{format_watch_time(w.get('total_time'))} watched")
    movies = _top_lines(blocks, "top_movies")
    shows = _top_lines(blocks, "top_tv")
    if movies:
        lines += ["", "Most watched movies (30 days):"] + movies
    if shows:
        lines += ["", "Most watched shows (30 days):"] + shows
    if titles_failed:
        lines += ["", "Couldn't load your most watched titles. Try again."]
    elif not movies and not shows:
        lines += ["", "No movies or shows watched in the last 30 days."]
    return "\n".join(lines), titles_failed


async def cmd_mystats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, ctx)
    text, _ = await build_mystats(ctx, update.effective_user.id)
    await update.effective_message.reply_text(text)


async def mystats_button(update: Update,
                         ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Trending list "My stats": into the Hub message with its back row when
    the list came from the Requests Hub, else a new message."""
    q = update.callback_query
    await q.answer()
    await send_typing(update, ctx)
    user_id = update.effective_user.id
    text, failed = await build_mystats(ctx, user_id)
    if came_from_hub(q.message, REQUESTS_HUB):
        sent = await edit_or_send(
            q, text,
            reply_markup=end_markup(REQUESTS_HUB, MYSTATS if failed else None))
        record_btn(ctx.application, user_id, sent)
    else:
        await q.message.reply_text(text)
