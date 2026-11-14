"""The Requests Hub's "Coming this week": episodes airing (Sonarr) and
movies releasing (Radarr) in the next 7 days, sorted by day, 10 to a page,
with an "Only what I follow" filter for users who can Follow.

The callback is `rqhw` from the Hub, then `rqhw:<only>:<page>` for the
filter toggle and Prev/Next. A service that is not set up is left out; with
neither a clear message is shown. A service that errors is left out too and
the list says so."""
from __future__ import annotations

import html
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.callback_prefixes import REQUESTS_WEEK
from bot.follows import follow_state
from bot.hub import back_button
from bot.requests_hub import REQUESTS_HUB
from bot.shared import edit_or_send, force_end_flow_conversations, record_btn

logger = logging.getLogger("usher")

WEEK_DAYS = 7
WEEK_PAGE_SIZE = 10

_MOVIE_DATES = (("inCinemas", "in cinemas"), ("digitalRelease", "digital"),
                ("physicalRelease", "on disc"))


def _day_of_episode(raw) -> Optional[date]:
    """Local calendar day of an `airDateUtc` timestamp."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.date()


def _day_of_release(raw) -> Optional[date]:
    """A movie release is a date: take it as written, no zone shift."""
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def collect_entries(episodes: list[dict], movies: list[dict],
                    start: date, end: date) -> list[dict]:
    """Calendar rows inside [start, end], sorted by day then title:
    {day, kind, title, detail, tmdb_id}. A movie with several release
    dates in the window appears once per date."""
    out: list[dict] = []
    for ep in episodes:
        day = _day_of_episode(ep.get("airDateUtc")) or \
            _day_of_release(ep.get("airDate"))
        if day is None or not start <= day <= end:
            continue
        series = ep.get("series") or {}
        season, number = ep.get("seasonNumber"), ep.get("episodeNumber")
        detail = (f"S{season:02d}E{number:02d}"
                  if isinstance(season, int) and isinstance(number, int)
                  else "")
        out.append({"day": day, "kind": "tv",
                    "title": series.get("title") or "?", "detail": detail,
                    "tmdb_id": series.get("tmdbId") or 0,
                    "tvdb_id": series.get("tvdbId") or 0})
    for mv in movies:
        for key, label in _MOVIE_DATES:
            day = _day_of_release(mv.get(key)) if mv.get(key) else None
            if day is None or not start <= day <= end:
                continue
            out.append({"day": day, "kind": "movie",
                        "title": mv.get("title") or "?", "detail": label,
                        "tmdb_id": mv.get("tmdbId") or 0})
    out.sort(key=lambda e: (e["day"], e["title"].lower(), e["detail"]))
    return out


def _render_text(entries: list[dict], page: int, only: bool,
                 failed: list[str]) -> str:
    total = len(entries)
    chunk = entries[page * WEEK_PAGE_SIZE:(page + 1) * WEEK_PAGE_SIZE]
    head = "🗓 <b>Coming this week</b>"
    if only:
        head += " (only what you follow)"
    lines = [head, ""]
    if not chunk:
        lines.append("Nothing on the calendar for the next 7 days."
                     if not only else
                     "None of the shows you follow air in the next 7 days.")
    last_day = None
    for e in chunk:
        if e["day"] != last_day:
            if last_day is not None:
                lines.append("")
            lines.append(f"<b>{e['day'].strftime('%a %b %d').replace(' 0', ' ')}"
                         "</b>")
            last_day = e["day"]
        icon = "📺" if e["kind"] == "tv" else "🎬"
        detail = f" ({html.escape(e['detail'])})" if e["detail"] else ""
        lines.append(f"{icon} {html.escape(e['title'])}{detail}")
    if total > WEEK_PAGE_SIZE:
        pages = (total - 1) // WEEK_PAGE_SIZE + 1
        lines.append("")
        lines.append(f"Page {page + 1} of {pages}")
    if failed:
        lines.append("")
        lines.append(f"⚠️ Couldn't read {' or '.join(failed)}, "
                     "so that part is missing.")
    return "\n".join(lines)


def _keyboard(page: int, pages: int, only: bool,
              can_filter: bool) -> InlineKeyboardMarkup:
    flag = 1 if only else 0
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "◀️ Prev", callback_data=f"{REQUESTS_WEEK}:{flag}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(
            "Next ▶️", callback_data=f"{REQUESTS_WEEK}:{flag}:{page + 1}"))
    if nav:
        rows.append(nav)
    if can_filter:
        rows.append([InlineKeyboardButton(
            "📺 Show everything" if only else "📺 Only what I follow",
            callback_data=f"{REQUESTS_WEEK}:{0 if only else 1}:0")])
    rows.append([back_button(REQUESTS_HUB)])
    return InlineKeyboardMarkup(rows)


async def coming_week_hub(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Requests Hub "Coming this week" button, its filter toggle and its
    Prev/Next: the list, edited into the Hub message."""
    q = update.callback_query
    await q.answer()
    force_end_flow_conversations(ctx, update)
    only, page = False, 0
    parts = (q.data or "").split(":")
    if len(parts) == 3:
        try:
            only, page = parts[1] == "1", max(0, int(parts[2]))
        except ValueError:
            only, page = False, 0
    user_id = update.effective_user.id
    bot_data = ctx.bot_data
    sonarr, radarr = bot_data.get("sonarr"), bot_data.get("radarr")
    if sonarr is None and radarr is None:
        text = ("Sonarr and Radarr aren't set up, so I have no calendar to "
                "show. Ask the Admin to link them in the web admin panel.")
        sent = await edit_or_send(
            q, text, reply_markup=InlineKeyboardMarkup(
                [[back_button(REQUESTS_HUB)]]))
        record_btn(ctx.application, user_id, sent)
        return

    start = date.today()
    # Today and the six days after it. The services read `end` as midnight
    # at the start of that date, so ask for the day after the last one.
    end = start + timedelta(days=WEEK_DAYS - 1)
    ask_end = (end + timedelta(days=1)).isoformat()
    episodes: list[dict] = []
    movies: list[dict] = []
    failed: list[str] = []
    for name, client, sink in (("Sonarr", sonarr, episodes),
                               ("Radarr", radarr, movies)):
        if client is None:
            continue
        try:
            sink.extend(await client.get_calendar(start.isoformat(), ask_end))
        except Exception:
            logger.warning("%s calendar read failed", name, exc_info=True)
            failed.append(name)
    if failed and len(failed) == sum(c is not None for c in (sonarr, radarr)):
        text = ("I couldn't read the calendar from "
                f"{' or '.join(failed)} just now. Try again in a moment.")
        sent = await edit_or_send(
            q, text, reply_markup=InlineKeyboardMarkup(
                [[back_button(REQUESTS_HUB)]]))
        record_btn(ctx.application, user_id, sent)
        return

    can_filter = (await follow_state(bot_data, user_id, 0)) is not None
    only = only and can_filter
    entries = collect_entries(episodes, movies, start, end)
    if only:
        try:
            ids = await bot_data["store"].list_follow_ids(user_id)
        except Exception:
            logger.warning("Follows lookup failed", exc_info=True)
            ids = []
        # Sonarr v3 series carry no TMDb id, so match on TVDb as well.
        tmdb = {t for t, _ in ids if t}
        tvdb = {v for _, v in ids if v}
        entries = [e for e in entries if e["kind"] == "tv"
                   and (e["tmdb_id"] in tmdb or e["tvdb_id"] in tvdb)]
    pages = max(1, (len(entries) - 1) // WEEK_PAGE_SIZE + 1)
    page = min(page, pages - 1)
    sent = await edit_or_send(
        q, _render_text(entries, page, only, failed),
        reply_markup=_keyboard(page, pages, only, can_filter),
        parse_mode="HTML", disable_web_page_preview=True)
    record_btn(ctx.application, user_id, sent)
