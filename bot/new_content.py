"""The new-content poller and Follow pings.

One job, every 15 minutes. It reads what Radarr and Sonarr imported since
its last stored mark (`GET /history/since`, `downloadFolderImported`; or
Tautulli's recently added list when Tautulli is linked), records
each import once in the new_content table, then sends the Follow pings: new
episodes are grouped per user and show into one DM with Watch now, and an
episode is pinged once however many polls or restarts pass (the row's pinged
flag is set as soon as its pings were handled). A Follow added after an
episode was imported never gets that episode."""
from __future__ import annotations

import html
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from telegram import InlineKeyboardMarkup

from bot.follows import follow_button
from bot.shared import watch_now_button
from tautulli import ids_from_guids

logger = logging.getLogger("usher")

NEW_CONTENT_INTERVAL_S = 15 * 60
NEW_CONTENT_FIRST_DELAY_S = 90
RADARR_MARK = "radarr_history"
SONARR_MARK = "sonarr_history"
TAUTULLI_MARK = "tautulli_added"
TAUTULLI_PAGE = 50
TAUTULLI_READ_PAGES = 40
TAUTULLI_MAX_ITEMS = 250
TAUTULLI_RETRY_S = 6 * 3600


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


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _added_at(row: dict) -> int:
    return _int(row.get("added_at")) or 0


def _iso_to_epoch(value) -> Optional[int]:
    try:
        return int(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
                   .replace(tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError):
        return None


async def _first_tautulli_mark(store) -> int:
    """Where a first Tautulli poll starts: the older of the Radarr and Sonarr
    marks (what they last read), so nothing that arrived since is lost; the
    store's de-duplication keeps an item from being recorded twice. With no
    arr mark yet, now."""
    stamps = [_iso_to_epoch(await store.get_poller_mark(k))
              for k in (RADARR_MARK, SONARR_MARK)]
    stamps = [t for t in stamps if t]
    return min(stamps) if stamps else int(time.time())


async def _read_fresh(tautulli, since: int) -> list[dict]:
    """Recently added rows newer than `since`, newest first, paged until a
    page runs out of fresh rows (at most TAUTULLI_READ_PAGES pages)."""
    items: list[dict] = []
    for page in range(TAUTULLI_READ_PAGES):
        rows = await tautulli.get_recently_added(
            count=TAUTULLI_PAGE, start=page * TAUTULLI_PAGE)
        fresh = [r for r in rows if _added_at(r) > since]
        items.extend(fresh)
        if len(fresh) < TAUTULLI_PAGE:
            break
    return items


async def _expand_episodes(tautulli, item: dict) -> list[tuple[dict, object]]:
    """The episodes under a season or show row as (child, show key) pairs.
    Tautulli's get_children_metadata lists a season's episodes, or a show's
    seasons (each then expanded in turn)."""
    kind, key = item.get("media_type"), item["rating_key"]
    if kind == "season":
        show_key = item.get("parent_rating_key")
        return [(c, c.get("grandparent_rating_key") or show_key)
                for c in await tautulli.get_children_metadata(key, "season")
                if c.get("media_type", "episode") == "episode"]
    out = []
    for season in await tautulli.get_children_metadata(key, "show"):
        if season.get("media_type", "season") != "season":
            continue
        kids = await tautulli.get_children_metadata(
            season["rating_key"], "season")
        for c in kids:
            if c.get("media_type", "episode") == "episode":
                out.append((c, c.get("grandparent_rating_key") or key))
    return out


async def _poll_tautulli(store, tautulli) -> None:
    """Record what Tautulli lists as recently added since its mark (epoch
    seconds). A season or show row is expanded to its episodes. An item with
    no TMDb id yet (Plex not matched) holds the mark back so it is read again
    next poll, for TAUTULLI_RETRY_S after it was added. At most
    TAUTULLI_MAX_ITEMS are handled per poll, oldest first; the mark never
    passes an item that was not handled. The mark moves only after the items
    were stored, so a failure reads them again next time."""
    raw = await store.get_poller_mark(TAUTULLI_MARK)
    if raw is None:
        since = await _first_tautulli_mark(store)
        await store.set_poller_mark(TAUTULLI_MARK, str(since))
    else:
        since = int(raw)
    items = sorted(await _read_fresh(tautulli, since), key=_added_at)
    truncated = len(items) > TAUTULLI_MAX_ITEMS
    items = items[:TAUTULLI_MAX_ITEMS]
    now = int(time.time())
    show_ids: dict = {}
    pending: list[int] = []  # added_at of items to read again

    async def show_tmdb(show_key):
        if show_key not in show_ids:
            show_ids[show_key] = ids_from_guids(
                await tautulli.get_metadata(show_key))
        return show_ids[show_key]

    for item in items:
        kind = item.get("media_type")
        key = item.get("rating_key")
        if key is None or kind not in ("movie", "episode", "season", "show"):
            continue
        added = _added_at(item)
        retry = now - added < TAUTULLI_RETRY_S
        try:
            if kind == "movie":
                tmdb, _tvdb = ids_from_guids(item)
                if tmdb is None and not item.get("guids"):
                    tmdb, _tvdb = ids_from_guids(
                        await tautulli.get_metadata(key))
                if tmdb:
                    await store.add_new_content(
                        f"tautulli:{key}", "movie", tmdb,
                        item.get("title") or "?")
                elif retry:
                    pending.append(added)
                continue
            if kind == "episode":
                episodes = [(item, item.get("grandparent_rating_key"))]
            else:
                episodes = await _expand_episodes(tautulli, item)
            for ep, show_key in episodes:
                season = _int(ep.get("parent_media_index"))
                number = _int(ep.get("media_index"))
                ep_key = ep.get("rating_key")
                if (show_key is None or season is None or number is None
                        or ep_key is None):
                    continue
                tmdb, tvdb = await show_tmdb(show_key)
                if not tmdb:
                    if retry:
                        pending.append(added)
                    break
                title = (ep.get("grandparent_title")
                         or item.get("grandparent_title")
                         or item.get("parent_title")
                         or (item.get("title") if kind == "show" else None)
                         or "?")
                await store.add_new_content(
                    f"tautulli:{ep_key}", "episode", tmdb, title, season,
                    number, tvdb_id=tvdb or 0)
        except Exception:
            if not retry:
                logger.warning("Tautulli item %s skipped", key, exc_info=True)
                continue
            logger.warning("Tautulli item %s will be read again", key,
                           exc_info=True)
            pending.append(added)
    mark = since
    if items:
        newest = _added_at(items[-1])
        # Rows sharing the last second may be unhandled when truncated; read
        # that second again (the store de-duplicates).
        mark = max(since, newest - 1 if truncated else newest)
    if pending:
        mark = max(since, min(mark, min(pending) - 1))
    await store.set_poller_mark(TAUTULLI_MARK, str(mark))


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
    tautulli = app.bot_data.get("tautulli")
    if tautulli is not None:
        sources = (("tautulli", tautulli, _poll_tautulli),)
        inactive = (RADARR_MARK, SONARR_MARK)
    else:
        sources = (("radarr", app.bot_data.get("radarr"), _poll_radarr),
                   ("sonarr", app.bot_data.get("sonarr"), _poll_sonarr))
        inactive = (TAUTULLI_MARK,)
    ok = True
    for key, client, poll in sources:
        if client is None:
            continue
        try:
            await poll(store, client)
        except Exception:
            ok = False
            logger.warning("New-content poll of %s failed", key, exc_info=True)
    if ok:
        # The unused source's mark follows along so switching back later
        # does not replay what the active one already recorded. It moves only
        # after the active poll succeeded.
        try:
            for mark in inactive:
                if mark == TAUTULLI_MARK:
                    if await store.get_poller_mark(mark) is None:
                        continue
                    value = str(int(time.time()))
                else:
                    value = _now_iso()
                await store.set_poller_mark(mark, value)
        except Exception:
            logger.warning("Could not move the idle source's mark",
                           exc_info=True)
    try:
        await send_follow_pings(app)
    except Exception:
        logger.exception("Follow pings failed")
