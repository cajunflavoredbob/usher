"""Automatic follows: a daily job, Tautulli only.

For each Linked user who is also a Tautulli user (matched by Plex username),
shows with 2 or more distinct episodes watched in the last 30 days get a
Follow with `auto` set, unless the user already has a row for that show
(active, or removed by them) or switched automatic follows off. The first
automatic Follow of a show sends the user one message with Unfollow; a
re-run finds the row and does nothing."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardMarkup

from bot.follows import follow_button
from bot.shared import watch_now_button
from tautulli import find_user_by_plex_username, ids_from_guids

logger = logging.getLogger("usher")

AUTO_FOLLOW_INTERVAL_S = 24 * 3600
AUTO_FOLLOW_FIRST_DELAY_S = 300
WINDOW_DAYS = 30
MIN_EPISODES = 2
HISTORY_LENGTH = 1000


async def _watched_shows(tautulli, user_id) -> dict:
    """{show rating key: (title, distinct episode count)} for episodes the
    Tautulli user watched in the window."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=WINDOW_DAYS)
    rows = await tautulli.get_history(
        user_id=user_id, media_type="episode", length=HISTORY_LENGTH,
        after=since.strftime("%Y-%m-%d"))
    cutoff = since.timestamp()
    shows: dict = {}
    for r in rows:
        show_key, ep_key = r.get("grandparent_rating_key"), r.get("rating_key")
        if show_key in (None, "") or ep_key in (None, ""):
            continue
        try:
            when = float(r.get("date") or r.get("started") or 0)
        except (TypeError, ValueError):
            when = 0
        if when and when < cutoff:
            continue
        entry = shows.setdefault(
            show_key, [r.get("grandparent_title") or "", set()])
        entry[1].add(str(ep_key))
    return {k: (t, len(eps)) for k, (t, eps) in shows.items()}


async def _notify(app, chat_id: int, tmdb_id: int, title: str) -> None:
    seerr = app.bot_data.get("seerr")
    watch = await watch_now_button(seerr, "tv", tmdb_id)
    row = ([watch] if watch else []) + [follow_button(tmdb_id, True)]
    await app.bot.send_message(
        chat_id=chat_id, parse_mode="HTML", disable_web_page_preview=True,
        text=(f"➕ You've been watching <b>{html.escape(title)}</b>, so I "
              "started following it. I'll ping you for each new episode. "
              "You can switch this off under Account, 🔔 Notices."),
        reply_markup=InlineKeyboardMarkup([row]))


async def run_auto_follow(app) -> None:
    """One pass over every Linked user. A failure for one user or show is
    logged and the pass goes on."""
    tautulli = app.bot_data.get("tautulli")
    if tautulli is None:
        return
    store = app.bot_data["store"]
    tell = app.bot_data["settings_store"].settings.tg_notify_follows
    show_ids: dict = {}
    for telegram_id in await store.list_linked_ids():
        try:
            if not (await store.get_prefs(telegram_id))["auto_follow"]:
                continue
            link = await store.get(telegram_id)
            tau_user = (await find_user_by_plex_username(
                tautulli, link.plex_username) if link else None)
            if not tau_user or tau_user.get("user_id") is None:
                continue
            shows = await _watched_shows(tautulli, tau_user["user_id"])
        except Exception:
            logger.warning("Auto-follow read failed (telegram_id=%d)",
                           telegram_id, exc_info=True)
            continue
        for show_key, (title, count) in shows.items():
            if count < MIN_EPISODES:
                continue
            try:
                if show_key not in show_ids:
                    show_ids[show_key] = ids_from_guids(
                        await tautulli.get_metadata(show_key))
                tmdb, tvdb = show_ids[show_key]
                if not tmdb or await store.has_follow_row(telegram_id, tmdb):
                    continue
                label = title or f"TMDb {tmdb}"
                if not await store.add_follow(telegram_id, tmdb, tvdb or 0,
                                              label, auto=True):
                    continue
                if tell:
                    await _notify(app, telegram_id, tmdb, label)
            except Exception:
                logger.warning("Auto-follow failed (telegram_id=%d show=%s)",
                               telegram_id, show_key, exc_info=True)


async def auto_follow_job(ctx) -> None:
    try:
        await run_auto_follow(ctx.application)
    except Exception:
        logger.exception("Auto-follow job failed")
