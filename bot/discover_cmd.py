"""/trending: text-forward discovery browsing (trending / popular /
upcoming), reusing the /request pick-list idiom. The Requests Hub's
"Trending" button opens the same list edited into the Hub message, with a
back row (trending_hub). Every result is
immediately requestable via the request conversation's jump entry point,
and the 🖼 detail cards are the same ones /request search shows.

The rule for these lists: no other user's identifiable activity. The TMDB
lists show none; "Popular here this week" (only with Tautulli linked) shows
titles only, never who watched or how many plays. Browse state lives in
user_data (dv_* keys) with its own version counter so superseded browse
messages toast instead of acting on a newer browse."""
from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from http_util import user_friendly_message
from seerr import MediaResult, SeerrClient
from tautulli import tmdb_id_from_metadata

from bot.callback_prefixes import (
    DV_CATEGORY,
    DV_INFO,
    DV_PAGE,
    MYSTATS,
    REQUESTS_TRENDING,
    RQ_FROM_TICKET,
)
from bot.digest import is_linked
from bot.hub import back_button, came_from_hub, end_markup, reply_end
from bot.media_card import STATUS_NOTES, send_detail_card
from bot.requests_hub import REQUESTS_HUB
from bot.shared import (
    KEYCAP_DIGITS,
    edit_or_send,
    force_end_flow_conversations,
    record_btn,
    require_seerr,
    send_typing,
)
from const import (
    KB_BUTTONS_PER_ROW,
    REQUEST_RESULTS_PER_PAGE,
    REQUEST_SEARCH_FETCH_LIMIT,
)

logger = logging.getLogger("usher")

CATEGORY_LABELS = {
    "trending": "🔥 Trending",
    "movies": "🎬 Movies",
    "tv": "📺 Shows",
    "upmovies": "🗓 Soon (Movies)",
    "uptv": "🗓 Soon (Shows)",
}

POPULAR_CATEGORY = "popular"
POPULAR_LABEL = "🏆 Popular here this week"
POPULAR_PER_STAT = 10

_STALE_BROWSE_TOAST = "That browse list is stale: run /trending again."


NOT_LINKED_MSG = "DM me /link first so I know which Plex account is yours."


async def _may_browse(ctx: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Trending is for the Admin and Linked users only."""
    return (user_id == ctx.bot_data.get("admin_id")
            or await is_linked(ctx, user_id))


async def _popular_allowed(ctx: ContextTypes.DEFAULT_TYPE,
                           user_id: int) -> bool:
    """Popular shows the server's watch activity: the Admin and Linked users
    only, and only with Tautulli linked."""
    return bool(ctx.bot_data.get("tautulli")
                and await _may_browse(ctx, user_id))


def _labels(ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    """The categories on offer: Popular here this week only for a viewer
    `_popular_allowed` cleared (kept in user_data by the caller)."""
    if ctx.user_data.get("dv_popular_ok"):
        return {**CATEGORY_LABELS, POPULAR_CATEGORY: POPULAR_LABEL}
    return CATEGORY_LABELS


async def _popular_results(client) -> list[MediaResult]:
    """The server's most watched movies then shows over 7 days, as titles
    only. Each is matched to a TMDb id through its Plex guids; one with no
    TMDb guid (or whose lookup fails) gets tmdb_id 0 and so no buttons. Only
    the title and year are kept from Tautulli's rows: no user names, no
    `users_watched`, no play counts."""
    out: list[MediaResult] = []
    for stat_id, media_type in (("popular_movies", "movie"),
                                ("popular_tv", "tv")):
        blocks = await client.get_home_stats(
            stat_id=stat_id, time_range=7, stats_count=POPULAR_PER_STAT)
        rows = []
        for b in blocks:
            if b.get("stat_id") == stat_id:
                rows = [r for r in (b.get("rows") or [])
                        if isinstance(r, dict) and r.get("title")]
                break
        rows = rows[:POPULAR_PER_STAT]

        async def tmdb_for(row) -> int:
            if row.get("rating_key") is None:
                return 0
            try:
                meta = await client.get_metadata(row["rating_key"])
            except Exception:
                logger.warning("Tautulli get_metadata failed", exc_info=True)
                return 0
            return tmdb_id_from_metadata(meta) or 0

        ids = await asyncio.gather(*(tmdb_for(r) for r in rows))
        for row, tmdb_id in zip(rows, ids):
            out.append(MediaResult(media_type, tmdb_id, str(row["title"]),
                                   str(row.get("year") or ""), None))
    return out


def _current_version(ctx: ContextTypes.DEFAULT_TYPE) -> int:
    return ctx.user_data.get("dv_version") or 0


def _result_line(index: int, r: MediaResult) -> str:
    type_emoji = "🎬" if r.media_type == "movie" else "📺"
    line = f"{index}. {type_emoji} {r.title}"
    if r.year:
        line += f" ({r.year})"
    note = STATUS_NOTES.get(r.status or 0)
    if note:
        line += f" - {note}"
    return line


def _render(ctx: ContextTypes.DEFAULT_TYPE,
            hub=None) -> tuple[str, InlineKeyboardMarkup]:
    results: list = ctx.user_data["dv_results"]
    category: str = ctx.user_data["dv_cat"]
    version: int = _current_version(ctx)
    screen: int = ctx.user_data.get("dv_screen", 0)
    last_screen = (len(results) - 1) // REQUEST_RESULTS_PER_PAGE
    screen = max(0, min(screen, last_screen))
    start = screen * REQUEST_RESULTS_PER_PAGE
    chunk = results[start:start + REQUEST_RESULTS_PER_PAGE]

    lines = [f"{_labels(ctx).get(category, POPULAR_LABEL)} - tap a number to request:", ""]
    for offset, r in enumerate(chunk):
        lines.append(_result_line(start + offset + 1, r))
    lines.append("")
    lines.append("🖼 = poster + details")

    rows: list[list[InlineKeyboardButton]] = []
    btn_row: list[InlineKeyboardButton] = []
    for offset, r in enumerate(chunk):
        if not r.tmdb_id:
            continue  # a Popular title with no TMDb guid: listed, no buttons
        n = start + offset
        keycap = KEYCAP_DIGITS[n] if n < len(KEYCAP_DIGITS) else str(n + 1)
        # Picking enters the request conversation via its jump entry point
        # (same one the /issue dead-end uses): gate, seasons, 4K, confirm
        # all come along for free.
        btn_row.append(InlineKeyboardButton(
            keycap,
            callback_data=f"{RQ_FROM_TICKET}:{r.media_type}:{r.tmdb_id}"))
        if len(btn_row) == KB_BUTTONS_PER_ROW:
            rows.append(btn_row)
            btn_row = []
    if btn_row:
        rows.append(btn_row)
    info_row = [
        InlineKeyboardButton(
            f"🖼 {start + offset + 1}",
            callback_data=f"{DV_INFO}:{version}:{r.media_type}:{r.tmdb_id}")
        for offset, r in enumerate(chunk) if r.tmdb_id
    ]
    if info_row:
        rows.append(info_row)
    if last_screen > 0:
        nav: list[InlineKeyboardButton] = []
        if screen > 0:
            nav.append(InlineKeyboardButton(
                f"⬅️ Page {screen}",
                callback_data=f"{DV_PAGE}:{version}:{screen - 1}"))
        if screen < last_screen:
            nav.append(InlineKeyboardButton(
                f"Page {screen + 2} ➡️",
                callback_data=f"{DV_PAGE}:{version}:{screen + 1}"))
        rows.append(nav)
    # Category switcher: every list is one tap away.
    cat_row = [InlineKeyboardButton(label, callback_data=f"{DV_CATEGORY}:{key}")
               for key, label in _labels(ctx).items() if key != category]
    rows.append(cat_row[:3])
    if cat_row[3:]:
        rows.append(cat_row[3:])
    if ctx.user_data.get("dv_stats_ok"):
        rows.append([InlineKeyboardButton("📊 My stats",
                                          callback_data=MYSTATS)])
    if hub:
        rows.append([back_button(hub)])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def _load_and_render(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                           reply_method, category: str, *,
                           user_id: int, hub=None) -> None:
    seerr: SeerrClient = ctx.bot_data["seerr"]
    ctx.user_data["dv_popular_ok"] = await _popular_allowed(ctx, user_id)
    # /mystats needs the user's own Link, so the Admin without one gets no button.
    ctx.user_data["dv_stats_ok"] = bool(
        ctx.bot_data.get("tautulli") and await is_linked(ctx, user_id))
    if category == POPULAR_CATEGORY and not ctx.user_data["dv_popular_ok"]:
        category = "trending"
    try:
        if category == POPULAR_CATEGORY:
            results = await _popular_results(ctx.bot_data["tautulli"])
        else:
            results = await seerr.discover(category,
                                           limit=REQUEST_SEARCH_FETCH_LIMIT)
    except Exception as exc:
        logger.exception("discover fetch failed")
        text = f"Couldn't fetch the list. {user_friendly_message(exc)}"
        await reply_end(ctx, reply_method, text, hub, REQUESTS_TRENDING,
                        user_id=user_id)
        return
    if not results:
        text = "Nothing in that list right now. Try another category."
        await reply_end(ctx, reply_method, text, hub, user_id=user_id)
        return
    ctx.user_data["dv_version"] = _current_version(ctx) + 1
    ctx.user_data["dv_results"] = results
    ctx.user_data["dv_cat"] = category
    ctx.user_data["dv_screen"] = 0
    text, markup = _render(ctx, hub)
    sent = await reply_method(text, reply_markup=markup)
    record_btn(ctx.application, user_id, sent)


async def trending_hub(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Requests Hub "Trending" button: the mixed trending list, edited into
    the tapped Hub message."""
    q = update.callback_query
    await q.answer()
    force_end_flow_conversations(ctx, update)
    if not await _may_browse(ctx, update.effective_user.id):
        sent = await edit_or_send(q, NOT_LINKED_MSG,
                                  reply_markup=end_markup(REQUESTS_HUB))
        record_btn(ctx.application, update.effective_user.id, sent)
        return
    if await require_seerr(update, ctx,
                           reply_markup=end_markup(REQUESTS_HUB)) is None:
        return
    await send_typing(update, ctx)

    async def edit_into_hub(text, **kwargs):
        return await edit_or_send(q, text, **kwargs)

    await _load_and_render(update, ctx, edit_into_hub, "trending",
                           user_id=update.effective_user.id, hub=REQUESTS_HUB)


async def cmd_trending(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/trending [movies|tv|upcoming] — defaults to the mixed trending list."""
    if not await _may_browse(ctx, update.effective_user.id):
        await update.effective_message.reply_text(NOT_LINKED_MSG)
        return
    if await require_seerr(update, ctx) is None:
        return
    await send_typing(update, ctx)
    arg = (ctx.args[0].lower() if getattr(ctx, "args", None) else "")
    category = {"movies": "movies", "tv": "tv", "shows": "tv",
                "upcoming": "upmovies"}.get(arg, "trending")
    await _load_and_render(update, ctx, update.effective_message.reply_text,
                           category, user_id=update.effective_user.id)


async def dv_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not await _may_browse(ctx, update.effective_user.id):
        await q.answer(NOT_LINKED_MSG, show_alert=True)
        return
    try:
        category = q.data.split(":")[1]
    except IndexError:
        await q.answer()
        return
    ctx.user_data["dv_popular_ok"] = await _popular_allowed(
        ctx, update.effective_user.id)
    if category not in _labels(ctx):
        await q.answer()
        return
    await q.answer()
    await send_typing(update, ctx)
    hub = REQUESTS_HUB if came_from_hub(q.message, REQUESTS_HUB) else None
    await _load_and_render(update, ctx, q.edit_message_text, category,
                           user_id=update.effective_user.id, hub=hub)


async def dv_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not await _may_browse(ctx, update.effective_user.id):
        await q.answer(NOT_LINKED_MSG, show_alert=True)
        return
    try:
        _, version_s, screen_s = q.data.split(":")
        version = int(version_s)
        screen = int(screen_s)
    except (ValueError, IndexError):
        await q.answer()
        return
    if version != _current_version(ctx) or not ctx.user_data.get("dv_results"):
        await q.answer(_STALE_BROWSE_TOAST, show_alert=True)
        return
    await q.answer()
    ctx.user_data["dv_screen"] = screen
    hub = REQUESTS_HUB if came_from_hub(q.message, REQUESTS_HUB) else None
    text, markup = _render(ctx, hub)
    sent = await q.edit_message_text(text, reply_markup=markup)
    record_btn(ctx.application, update.effective_user.id, sent)


async def dv_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not await _may_browse(ctx, update.effective_user.id):
        await q.answer(NOT_LINKED_MSG, show_alert=True)
        return
    try:
        _, version_s, media_type, tmdb_id_s = q.data.split(":")
        version = int(version_s)
        tmdb_id = int(tmdb_id_s)
    except (ValueError, IndexError):
        await q.answer()
        return
    if version != _current_version(ctx):
        await q.answer(_STALE_BROWSE_TOAST, show_alert=True)
        return
    r = next((x for x in ctx.user_data.get("dv_results") or []
              if x.media_type == media_type and x.tmdb_id == tmdb_id), None)
    if r is None:
        await q.answer()
        return
    await q.answer()
    await send_detail_card(ctx, update.effective_chat.id, r)
