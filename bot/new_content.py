"""The new-content poller and Follow pings.

One job, every 15 minutes. It reads what Radarr and Sonarr imported since
its last stored mark (`GET /history/since`, `downloadFolderImported`), records
each import once in the new_content table, then sends the Follow pings: new
episodes are grouped per user and show into one DM with Watch now, and an
episode is pinged once however many polls or restarts pass (the row's pinged
flag is set as soon as its pings were handled). A Follow added after an
episode was imported never gets that episode."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardMarkup

from bot.follows import follow_button
from bot.shared import watch_now_button

logger = logging.getLogger("usher")

NEW_CONTENT_INTERVAL_S = 15 * 60
NEW_CONTENT_FIRST_DELAY_S = 90
RADARR_MARK = "radarr_history"
SONARR_MARK = "sonarr_history"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _latest(records: list[dict], current: str) -> str:
    dates = [r["date"] for r in records
             if isinstance(r, dict) and isinstance(r.get("date"), str)]
    return max([current, *dates])


async def _poll_radarr(store, radarr) -> None:
    since = await store.get_poller_mark(RADARR_MARK)
    if since is None:
        # First run: start from now rather than replaying old imports.
        await store.set_poller_mark(RADARR_MARK, _now_iso())
        return
    records = await radarr.get_history_imports_since(since)
    for rec in records:
        movie = rec.get("movie") or {}
        tmdb_id = movie.get("tmdbId") or 0
        if not tmdb_id or rec.get("id") is None:
            continue
        await store.add_new_content(
            f"radarr:{rec['id']}", "movie", tmdb_id,
            movie.get("title") or "?")
    await store.set_poller_mark(RADARR_MARK, _latest(records, since))


async def _poll_sonarr(store, sonarr) -> None:
    since = await store.get_poller_mark(SONARR_MARK)
    if since is None:
        await store.set_poller_mark(SONARR_MARK, _now_iso())
        return
    records = await sonarr.get_history_imports_since(since)
    series_cache: dict[int, object] = {}
    for rec in records:
        series_id = rec.get("seriesId")
        if series_id is None or rec.get("id") is None:
            continue
        if series_id not in series_cache:
            series_cache[series_id] = await sonarr.get_series_ids(series_id)
        series = series_cache[series_id]
        if not series or not series["tmdb_id"]:
            continue
        ep = rec.get("episode") or {}
        season, number = ep.get("seasonNumber"), ep.get("episodeNumber")
        if season is None or number is None:
            if rec.get("episodeId") is None:
                continue
            nums = await sonarr.get_episode_numbers(rec["episodeId"])
            if nums is None:
                continue
            season, number = nums
        await store.add_new_content(
            f"sonarr:{rec['id']}", "episode", series["tmdb_id"],
            series["title"], season, number, tvdb_id=series["tvdb_id"])
    await store.set_poller_mark(SONARR_MARK, _latest(records, since))


def _episode_list(episodes: list[tuple[int, int]]) -> str:
    labels = [f"S{s:02d}E{e:02d}" for s, e in sorted(episodes)]
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " and " + labels[-1]


async def send_follow_pings(app) -> None:
    """DM each follower one message per show for its not-yet-pinged episodes,
    then mark those episodes pinged. With the Follow pings switch off nothing
    is sent and the episodes are marked anyway (missed events are not
    replayed)."""
    store = app.bot_data["store"]
    rows = await store.list_unpinged_episodes()
    if not rows:
        return
    enabled = app.bot_data["settings_store"].settings.tg_notify_follows
    shows: dict[int, dict] = {}
    for _id, tmdb_id, title, season, episode in rows:
        show = shows.setdefault(tmdb_id, {"title": title, "eps": []})
        show["eps"].append((season, episode))
    if enabled:
        seerr = app.bot_data.get("seerr")
        for tmdb_id, show in shows.items():
            try:
                followers = await store.list_followers(tmdb_id)
                watch = (await watch_now_button(seerr, "tv", tmdb_id)
                         if followers else None)
            except Exception:
                logger.exception("Follow ping lookup failed (tmdb=%s)", tmdb_id)
                continue
            text = (f"📺 New on the server: <b>{html.escape(show['title'])}</b>"
                    f"\n{_episode_list(show['eps'])}")
            for chat_id in followers:
                row = ([watch] if watch else []) + [follow_button(tmdb_id, True)]
                try:
                    await app.bot.send_message(
                        chat_id=chat_id, text=text, parse_mode="HTML",
                        disable_web_page_preview=True,
                        reply_markup=InlineKeyboardMarkup([row]))
                except Exception:
                    logger.exception("Follow ping to telegram_id=%d failed",
                                     chat_id)
    await store.mark_pinged([r[0] for r in rows])


async def poll_new_content(ctx) -> None:
    """The 15-minute job: record new imports, then send Follow pings. A
    source that fails keeps its mark and is read again next time."""
    app = ctx.application
    store = app.bot_data["store"]
    for key, client, poll in (("radarr", app.bot_data.get("radarr"), _poll_radarr),
                              ("sonarr", app.bot_data.get("sonarr"), _poll_sonarr)):
        if client is None:
            continue
        try:
            await poll(store, client)
        except Exception:
            logger.warning("New-content poll of %s failed", key, exc_info=True)
    try:
        await send_follow_pings(app)
    except Exception:
        logger.exception("Follow pings failed")
