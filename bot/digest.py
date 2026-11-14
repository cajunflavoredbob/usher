"""The daily Digest ("New on the server") and the Account Hub's Notices card.

A job wakes every few minutes; in the hour the Admin chose (the server's
local time) it sends each opted-in Linked user the previous local day's rows
from the new_content table (the same table the Follow pings read, filled by
bot.new_content), grouped as movies, then shows with their new episode
count. A day with nothing new sends nothing, and the feature switch off sends
nothing. The day is marked sent before the DMs go out, so a restart inside
the hour never sends it twice (a crash mid-way can drop the rest of that
day's DMs rather than repeat them).

The Notices card holds the two per-user switches: the Digest opt-in and
automatic follows."""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.callback_prefixes import DIGEST_INFO, NOTICE_TOGGLE
from bot.hub import back_button
from bot.media_card import send_detail_card
from bot.shared import edit_or_send, record_btn, watch_now_button

logger = logging.getLogger("usher")

DIGEST_INTERVAL_S = 10 * 60
DIGEST_FIRST_DELAY_S = 120
DIGEST_SENT_MARK = "digest_sent"
# Entries listed (and given a button) in one Digest; the rest are counted.
DIGEST_ENTRIES_MAX = 30
_DB_TIME = "%Y-%m-%d %H:%M:%S"


def _day_window(now: datetime) -> tuple[str, str]:
    """The previous local calendar day as UTC database times [start, end)."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=1)
    return (start.astimezone(timezone.utc).strftime(_DB_TIME),
            today.astimezone(timezone.utc).strftime(_DB_TIME))


def _group(rows: list[tuple]) -> tuple[list[tuple], list[tuple]]:
    """([(tmdb_id, title)] movies, [(tmdb_id, title, episode_count)] shows),
    each in first-seen order, one entry per title."""
    movies: dict[int, str] = {}
    shows: dict[int, list] = {}
    for media_type, tmdb_id, title, _season, _episode in rows:
        if media_type == "movie":
            movies.setdefault(tmdb_id, title)
        else:
            shows.setdefault(tmdb_id, [title, 0])[1] += 1
    return ([(i, t) for i, t in movies.items()],
            [(i, v[0], v[1]) for i, v in shows.items()])


def _label(prefix: str, title: str) -> str:
    title = title if len(title) <= 40 else title[:39] + "…"
    return f"{prefix} {title}"


async def _build(seerr, rows: list[tuple]
                 ) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    movies, shows = _group(rows)
    entries = ([("movie", i, t, None) for i, t in movies]
               + [("tv", i, t, n) for i, t, n in shows])
    if not entries:
        return None
    shown, extra = entries[:DIGEST_ENTRIES_MAX], entries[DIGEST_ENTRIES_MAX:]
    lines = ["🆕 <b>New on the server</b> yesterday"]
    section = None
    keyboard = []
    for media_type, tmdb_id, title, count in shown:
        if section != media_type:
            section = media_type
            lines.append("\n🎬 <b>Movies</b>" if media_type == "movie"
                         else "\n📺 <b>Shows</b>")
        if count is None:
            lines.append(f"• {html.escape(title)}")
        else:
            word = "episode" if count == 1 else "episodes"
            lines.append(f"• {html.escape(title)}: {count} new {word}")
        watch = await watch_now_button(seerr, media_type, tmdb_id)
        if watch is not None:
            keyboard.append([InlineKeyboardButton(
                _label("▶️", title), url=watch.url)])
        else:
            keyboard.append([InlineKeyboardButton(
                _label("ℹ️", title),
                callback_data=f"{DIGEST_INFO}:{media_type}:{tmdb_id}")])
    if extra:
        lines.append(f"\n…and {len(extra)} more.")
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def send_digest(app, now: Optional[datetime] = None) -> int:
    """Send the previous day's Digest to every opted-in Linked user. Returns
    how many DMs went out; an empty day sends nothing."""
    now = now or datetime.now().astimezone()
    store = app.bot_data["store"]
    start, end = _day_window(now)
    rows = await store.list_new_content_between(start, end)
    if not rows:
        return 0
    users = await store.list_digest_users()
    if not users:
        return 0
    built = await _build(app.bot_data.get("seerr"), rows)
    if built is None:
        return 0
    text, markup = built
    sent = 0
    for chat_id in users:
        try:
            await app.bot.send_message(
                chat_id=chat_id, text=text, parse_mode="HTML",
                disable_web_page_preview=True, reply_markup=markup)
            sent += 1
        except Exception:
            logger.exception("Digest to telegram_id=%d failed", chat_id)
        await asyncio.sleep(0.04)
    return sent


async def run_digest_if_due(app, now: Optional[datetime] = None) -> int:
    """Send the Digest when the feature is on and `now` is inside the
    Admin's hour and today's has not gone out yet."""
    now = now or datetime.now().astimezone()
    cfg = app.bot_data["settings_store"].settings
    if not (cfg.digest_enabled and cfg.tg_notify_digest):
        return 0
    if now.hour != cfg.digest_hour:
        return 0
    store = app.bot_data["store"]
    today = now.date().isoformat()
    if await store.get_poller_mark(DIGEST_SENT_MARK) == today:
        return 0
    await store.set_poller_mark(DIGEST_SENT_MARK, today)
    return await send_digest(app, now)


async def digest_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await run_digest_if_due(ctx.application)
    except Exception:
        logger.exception("Digest job failed")


# Account Hub "Notices" card

def _onoff(value: bool) -> str:
    return "on" if value else "off"


async def _notices_card(store, telegram_id: int):
    prefs = await store.get_prefs(telegram_id)
    text = (
        "🔔 <b>Notices</b>\n\n"
        f"Daily digest: <b>{_onoff(prefs['digest'])}</b>. A daily message "
        "listing what was added to the server.\n"
        f"Automatic follows: <b>{_onoff(prefs['auto_follow'])}</b>. Follow "
        "shows you have been watching, so you hear about new episodes.")
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"📰 Daily digest: {_onoff(prefs['digest'])}",
            callback_data=f"{NOTICE_TOGGLE}:digest")],
        [InlineKeyboardButton(
            f"➕ Automatic follows: {_onoff(prefs['auto_follow'])}",
            callback_data=f"{NOTICE_TOGGLE}:auto")],
        [back_button("account")],
    ])
    return text, markup


async def is_linked(ctx, telegram_id: int) -> bool:
    link = await ctx.bot_data["store"].get(telegram_id)
    return link is not None and bool(link.plex_token)


async def _show_card(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = update.effective_user.id
    if not await is_linked(ctx, uid):
        sent = await edit_or_send(
            q, "Link your Plex account first.",
            reply_markup=InlineKeyboardMarkup([[back_button("account")]]))
        record_btn(ctx.application, uid, sent)
        return
    text, markup = await _notices_card(ctx.bot_data["store"], uid)
    sent = await edit_or_send(q, text, reply_markup=markup, parse_mode="HTML")
    record_btn(ctx.application, uid, sent)


async def account_notices(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Account Hub "Notices" tap: open the card."""
    await update.callback_query.answer()
    await _show_card(update, ctx)


async def notice_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """A Notices toggle tap: flip the switch, redraw the card."""
    q = update.callback_query
    await q.answer()
    name = {"digest": "digest", "auto": "auto_follow"}.get(
        q.data.split(":", 1)[1])
    uid = update.effective_user.id
    if name is not None and await is_linked(ctx, uid):
        store = ctx.bot_data["store"]
        prefs = await store.get_prefs(uid)
        await store.set_pref(uid, name, not prefs[name])
    await _show_card(update, ctx)


async def digest_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """A Digest entry's detail-card button."""
    q = update.callback_query
    await q.answer()
    try:
        _, media_type, tmdb_id_s = q.data.split(":")
        tmdb_id = int(tmdb_id_s)
    except ValueError:
        return
    if media_type not in ("movie", "tv"):
        return
    seerr = ctx.bot_data.get("seerr")
    try:
        r = await seerr.get_media_result(media_type, tmdb_id) if seerr else None
    except Exception:
        logger.warning("Digest detail lookup failed", exc_info=True)
        r = None
    if r is None:
        await ctx.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Couldn't load that title right now.")
        return
    await send_detail_card(ctx, update.effective_chat.id, r,
                           update.effective_user.id)
