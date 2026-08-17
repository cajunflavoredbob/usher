"""/trending: text-forward discovery browsing (trending / popular /
upcoming), reusing the /request pick-list idiom. Every result is
immediately requestable via the request conversation's jump entry point,
and the 🖼 detail cards are the same ones /request search shows.

Deliberately TMDB-only lists: nothing here surfaces other users' requests
or activity. Browse state lives in user_data (dv_* keys) with its own
version counter so superseded browse messages toast instead of acting on a
newer browse."""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from http_util import user_friendly_message
from seerr import MediaResult, SeerrClient

from bot.callback_prefixes import (
    DV_CATEGORY,
    DV_INFO,
    DV_PAGE,
    RQ_FROM_ISSUE,
)
from bot.media_card import STATUS_NOTES, send_detail_card
from bot.shared import KEYCAP_DIGITS, record_btn, require_seerr, send_typing
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

_STALE_BROWSE_TOAST = "That browse list is stale — run /trending again."


def _current_version(ctx: ContextTypes.DEFAULT_TYPE) -> int:
    return ctx.user_data.get("dv_version") or 0


def _result_line(index: int, r: MediaResult) -> str:
    type_emoji = "🎬" if r.media_type == "movie" else "📺"
    line = f"{index}. {type_emoji} {r.title}"
    if r.year:
        line += f" ({r.year})"
    note = STATUS_NOTES.get(r.status or 0)
    if note:
        line += f" — {note}"
    return line


def _render(ctx: ContextTypes.DEFAULT_TYPE) -> tuple[str, InlineKeyboardMarkup]:
    results: list = ctx.user_data["dv_results"]
    category: str = ctx.user_data["dv_cat"]
    version: int = _current_version(ctx)
    screen: int = ctx.user_data.get("dv_screen", 0)
    last_screen = (len(results) - 1) // REQUEST_RESULTS_PER_PAGE
    screen = max(0, min(screen, last_screen))
    start = screen * REQUEST_RESULTS_PER_PAGE
    chunk = results[start:start + REQUEST_RESULTS_PER_PAGE]

    lines = [f"{CATEGORY_LABELS[category]} — tap a number to request:", ""]
    for offset, r in enumerate(chunk):
        lines.append(_result_line(start + offset + 1, r))
    lines.append("")
    lines.append("🖼 = poster + details")

    rows: list[list[InlineKeyboardButton]] = []
    btn_row: list[InlineKeyboardButton] = []
    for offset, r in enumerate(chunk):
        n = start + offset
        keycap = KEYCAP_DIGITS[n] if n < len(KEYCAP_DIGITS) else str(n + 1)
        # Picking enters the request conversation via its jump entry point
        # (same one the /issue dead-end uses): gate, seasons, 4K, confirm
        # all come along for free.
        btn_row.append(InlineKeyboardButton(
            keycap,
            callback_data=f"{RQ_FROM_ISSUE}:{r.media_type}:{r.tmdb_id}"))
        if len(btn_row) == KB_BUTTONS_PER_ROW:
            rows.append(btn_row)
            btn_row = []
    if btn_row:
        rows.append(btn_row)
    rows.append([
        InlineKeyboardButton(
            f"🖼 {start + offset + 1}",
            callback_data=f"{DV_INFO}:{version}:{r.media_type}:{r.tmdb_id}")
        for offset, r in enumerate(chunk)
    ])
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
               for key, label in CATEGORY_LABELS.items() if key != category]
    rows.append(cat_row[:3])
    if cat_row[3:]:
        rows.append(cat_row[3:])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def _load_and_render(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                           reply_method, category: str, *,
                           user_id: int) -> None:
    seerr: SeerrClient = ctx.bot_data["seerr"]
    try:
        results = await seerr.discover(category,
                                       limit=REQUEST_SEARCH_FETCH_LIMIT)
    except Exception as exc:
        logger.exception("discover fetch failed")
        await reply_method(f"Couldn't fetch the list. {user_friendly_message(exc)}")
        return
    if not results:
        await reply_method("Nothing in that list right now. Try another category.")
        return
    ctx.user_data["dv_version"] = _current_version(ctx) + 1
    ctx.user_data["dv_results"] = results
    ctx.user_data["dv_cat"] = category
    ctx.user_data["dv_screen"] = 0
    text, markup = _render(ctx)
    sent = await reply_method(text, reply_markup=markup)
    record_btn(ctx.application, user_id, sent)


async def cmd_trending(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/trending [movies|tv|upcoming] — defaults to the mixed trending list."""
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
    try:
        category = q.data.split(":")[1]
    except IndexError:
        await q.answer()
        return
    if category not in CATEGORY_LABELS:
        await q.answer()
        return
    await q.answer()
    await send_typing(update, ctx)
    await _load_and_render(update, ctx, q.edit_message_text, category,
                           user_id=update.effective_user.id)


async def dv_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
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
    text, markup = _render(ctx)
    sent = await q.edit_message_text(text, reply_markup=markup)
    record_btn(ctx.application, update.effective_user.id, sent)


async def dv_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
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
