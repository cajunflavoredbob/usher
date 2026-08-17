"""Morphing request cards: the /request confirmation message edits in place
through the download's life (waiting → grabbing with live percent/ETA →
final state), plus the SABnzbd priority boost for bot-originated grabs.

The poller paints progress (including a download-failed holding line while
the arr hunts for another release); the states that CLOSE a watch come only
from Seerr's webhook events (approved/declined/available/failed).
A watch that outlives its timeout gets one honest final edit and is
dropped -- the /requests list remains the source of truth."""
from __future__ import annotations

import html
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from telegram.ext import ContextTypes

from seerr import SeerrClient
from store import UserStore

from const import REQUEST_WATCH_TIMEOUT_HOURS

logger = logging.getLogger("usher")

# Watch ids currently mid-tick (same guard as the autofix poller: a slow
# tick must not double-process against the next one).
_inflight: set[int] = set()


# When SABnzbd errors, skip it for this long: each card independently
# rediscovering an outage through client timeouts froze every card for
# minutes (the exact staleness this feature exists to fix).
_SAB_SKIP_S = 120.0
_sab_down_until = 0.0

# Post-processing stages worth naming; anything else (Queued, Running, ...)
# renders as a generic post-processing line.
_PP_STAGES = {"Verifying", "Repairing", "Extracting", "Moving"}


async def resolve_progress_line(ctx, progress, *, verb: str = "Downloading",
                                fallback_line: str = "") -> str:
    """Human progress line for a set of queue records. Prefers SABnzbd's
    live view when it's configured and knows the downloads: the arrs only
    sync with their clients on their own schedule, so their percent lags,
    and they show nothing during SABnzbd post-processing (the old frozen
    95% / silent minutes before import).

    All ids are read in one batched SAB lookup and AGGREGATED: a season
    grabbed as several NZBs shows the mean percent of the still-downloading
    jobs, not whichever job happened to list first. On a SAB error the
    ERRORING tick keeps the previous line (fallback_line) rather than
    flashing the arr's lagging numbers, and SAB is skipped for a cooldown;
    during the cooldown cards run on the arr's own (laggier but moving)
    numbers."""
    global _sab_down_until
    sab = ctx.bot_data.get("sabnzbd")
    now = time.monotonic()
    if sab is not None and progress.download_ids and now >= _sab_down_until:
        try:
            statuses = await sab.get_items_status(progress.download_ids)
        except Exception:
            logger.debug("SAB status lookup failed; skipping SAB for %.0fs",
                         _SAB_SKIP_S, exc_info=True)
            _sab_down_until = now + _SAB_SKIP_S
            statuses = None
        if statuses:
            known = list(statuses.values())
            downloading = [s for s in known if s[0] == "downloading"]
            if downloading:
                percent = round(sum(s[1] for s in downloading) / len(downloading))
                slowest = min(downloading, key=lambda s: s[1])
                line = f"⬇️ {verb} — {percent}%"
                timeleft = html.escape((slowest[2] or "").strip())
                if timeleft and timeleft != "0:00:00":
                    line += f" · ~{timeleft} left"
                if len(progress.download_ids) > 1:
                    line += f" · {len(progress.download_ids)} files"
                return line
            postproc = [s for s in known if s[0] == "postproc"]
            if postproc:
                stage = postproc[0][2]
                if stage in _PP_STAGES:
                    return f"📦 Post-processing ({html.escape(stage)})…"
                return "📦 Post-processing…"
            if any(s[0] == "completed" for s in known):
                return "📥 Importing…"
            if known and all(s[0] == "failed" for s in known):
                return "⚠️ Download failed — waiting for another release…"
        elif statuses is None and fallback_line:
            # Transient SAB error: keep the last painted line instead of
            # flashing the arr's lagging percent backwards for one tick.
            return fallback_line
    # Fallback: the arr queue's own numbers.
    line = f"⬇️ {verb} — {progress.percent}%"
    timeleft = html.escape((progress.timeleft or "").strip())
    if timeleft and timeleft != "00:00:00":
        line += f" · ~{timeleft} left"
    if progress.count > 1:
        line += f" · {progress.count} files"
    return line


# Both pollers call refresh_arr_downloads; without a shared throttle the
# arrs would see up to 6 POSTs/min each. One refresh per arr per interval
# is plenty -- the command runs async in the arr anyway, so its benefit
# lands by the NEXT tick's queue read regardless.
_REFRESH_MIN_GAP_S = 15.0
_last_refresh: dict = {}


async def refresh_arr_downloads(ctx, *, movies: bool, tv: bool) -> None:
    """Fire RefreshMonitoredDownloads on the relevant arrs (throttled to
    once per _REFRESH_MIN_GAP_S per arr across both pollers) so their queue
    data and import detection track reality instead of their own leisurely
    sync schedule."""
    now = time.monotonic()
    for enabled, key in ((movies, "radarr"), (tv, "sonarr")):
        if not enabled:
            continue
        client = ctx.bot_data.get(key)
        if client is None:
            continue
        if now - _last_refresh.get(key, 0.0) < _REFRESH_MIN_GAP_S:
            continue
        _last_refresh[key] = now
        try:
            await client.refresh_monitored_downloads()
        except Exception:
            logger.debug("%s refresh failed (non-fatal)", key, exc_info=True)


def _card_text(label: str, body_line: str, *, footer: bool = True) -> str:
    text = f"<b>{html.escape(label)}</b>\n{body_line}"
    if footer:
        text += "\n\nUse /requests to check on it."
    return text


async def _edit_card(ctx, watch: dict, body_line: str, *,
                     footer: bool = True) -> bool:
    try:
        await ctx.bot.edit_message_text(
            chat_id=watch["chat_id"],
            message_id=watch["message_id"],
            text=_card_text(watch["label"], body_line, footer=footer),
            parse_mode="HTML",
        )
        return True
    except Exception:
        # "message is not modified" and deleted-message errors both land
        # here; neither is worth more than a debug line.
        logger.debug("watch card edit failed (non-fatal)", exc_info=True)
        return False


async def maybe_bump_priority(ctx, download_ids: list) -> bool:
    """Boost the given download-client items per the admin's SABnzbd boost
    setting. Returns True when a bump was attempted (so callers mark the
    row and never re-bump)."""
    sab = ctx.bot_data.get("sabnzbd")
    settings_store = ctx.bot_data.get("settings_store")
    boost = getattr(getattr(settings_store, "settings", None),
                    "sabnzbd_boost", "off")
    if sab is None or boost == "off" or not download_ids:
        return False
    for did in download_ids:
        try:
            await sab.set_priority(did, boost)
            logger.info("SABnzbd priority '%s' applied to %s", boost, did)
        except Exception:
            # A non-SABnzbd download id (torrent client) or a finished item
            # lands here; the boost is best-effort by design.
            logger.debug("SABnzbd bump failed for %s (non-fatal)", did,
                         exc_info=True)
    return True


async def _resolve_arr_id(ctx, watch: dict) -> Optional[int]:
    """The arr's internal id for this watch's media (cached on the row)."""
    if watch["arr_id"]:
        return watch["arr_id"]
    store: UserStore = ctx.bot_data["store"]
    if watch["media_type"] == "movie":
        radarr = ctx.bot_data.get("radarr")
        if radarr is None:
            return None
        movie = await radarr.get_movie_by_tmdb(watch["tmdb_id"])
        if movie is None:
            return None
        await store.update_request_watch(watch["id"], arr_id=movie.id)
        return movie.id
    sonarr = ctx.bot_data.get("sonarr")
    seerr: Optional[SeerrClient] = ctx.bot_data.get("seerr")
    if sonarr is None or seerr is None:
        return None
    _, tvdb_id = await seerr.get_tv_seasons(watch["tmdb_id"])
    if not tvdb_id:
        return None
    series = await sonarr.get_series_by_tvdb(tvdb_id)
    if series is None:
        return None
    await store.update_request_watch(watch["id"], arr_id=series.id)
    return series.id


async def poll_request_watches(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Paint download progress onto each grabbing watch card."""
    store: UserStore = ctx.bot_data["store"]
    watches = await store.list_request_watches()
    grabbing = [w for w in watches if w["status"] == "grabbing"]
    if grabbing:
        await refresh_arr_downloads(
            ctx,
            movies=any(w["media_type"] == "movie" for w in grabbing),
            tv=any(w["media_type"] == "tv" for w in grabbing))
    for watch in watches:
        if watch["id"] in _inflight:
            continue
        _inflight.add(watch["id"])
        try:
            await _poll_one(ctx, store, watch)
        except Exception:
            logger.exception("request-watch poll failed for %d", watch["id"])
        finally:
            _inflight.discard(watch["id"])


async def _poll_one(ctx, store: UserStore, watch: dict) -> None:
    # Timeout: one final honest edit, then drop the row.
    try:
        timeout_at = datetime.fromisoformat(
            watch["timeout_at"].replace(" ", "T")).replace(tzinfo=timezone.utc)
        timed_out = datetime.now(timezone.utc) >= timeout_at
    except Exception:
        timed_out = True
    if timed_out:
        await _edit_card(
            ctx, watch,
            f"⏳ Still processing after {REQUEST_WATCH_TIMEOUT_HOURS}h — "
            "I'll DM you when Seerr reports it available.")
        await store.delete_request_watch(watch["id"])
        return

    # Pending-approval watches have nothing to poll; the MEDIA_APPROVED
    # webhook flips them to grabbing.
    if watch["status"] != "grabbing":
        return

    arr_id = await _resolve_arr_id(ctx, watch)
    if arr_id is None:
        return
    if watch["media_type"] == "movie":
        radarr = ctx.bot_data.get("radarr")
        progress = await radarr.get_queue_progress(arr_id) if radarr else None
    else:
        sonarr = ctx.bot_data.get("sonarr")
        progress = await sonarr.get_queue_progress(arr_id) if sonarr else None
    if progress is None:
        # Not grabbed yet, or already imported (the webhook closes those).
        # One special case: after a failed download the arr drops the queue
        # record while it hunts for another release; without this the card
        # would sit on the failure line until the timeout edit contradicted
        # it.
        if watch["last_progress"].startswith("⚠️"):
            line = "🔎 Looking for another release…"
            if await store.update_request_watch(watch["id"],
                                                last_progress=line):
                if not await _edit_card(ctx, watch, line):
                    # Roll back so the next tick retries the edit (persist-
                    # first is the race guard, but a swallowed failure on a
                    # static line would otherwise never repaint).
                    await store.update_request_watch(
                        watch["id"], last_progress=watch["last_progress"])
        return

    if not watch["bumped"]:
        if await maybe_bump_priority(ctx, progress.download_ids):
            await store.update_request_watch(watch["id"], bumped=1)

    line = await resolve_progress_line(ctx, progress,
                                       fallback_line=watch["last_progress"])
    if line == watch["last_progress"]:
        return  # nothing changed; skip the edit
    # Persist BEFORE editing: update_request_watch reports whether the row
    # still exists, so a watch the webhook just finalized (and deleted)
    # mid-tick doesn't get its terminal card overwritten with stale
    # progress.
    if await store.update_request_watch(watch["id"], last_progress=line):
        if not await _edit_card(ctx, watch, line):
            # Roll back so static lines (postproc/importing) retry next tick;
            # without this a transient Telegram failure on a line that never
            # changes text would strand the card (percent lines self-heal).
            await store.update_request_watch(
                watch["id"], last_progress=watch["last_progress"])


# --- Webhook-driven state changes --------------------------------------------

_FINAL_LINES = {
    "MEDIA_AVAILABLE": "✅ Available in Plex — enjoy!",
    "MEDIA_DECLINED": "🚫 Declined by the admin.",
    "MEDIA_FAILED": "⚠️ Download failed — the admin has been notified.",
}


async def apply_webhook_event(app, nt: str, media_type: str,
                              tmdb_id: int) -> None:
    """Morph every watch card for this media per the lifecycle event.
    Approved -> grabbing (poller takes over); terminal events paint the
    final line and close the watch."""
    store: UserStore = app.bot_data["store"]
    try:
        watches = await store.find_request_watches(media_type, tmdb_id)
    except Exception:
        logger.exception("watch lookup failed for %s/%s", media_type, tmdb_id)
        return
    if not watches:
        return

    class _Shim:  # _edit_card/maybe_bump take a ctx-shaped object
        bot = app.bot
        bot_data = app.bot_data

    ctx = _Shim()
    if nt in ("MEDIA_APPROVED", "MEDIA_AUTO_APPROVED"):
        for watch in watches:
            if watch["status"] != "grabbing":
                await store.update_request_watch(watch["id"], status="grabbing")
                await _edit_card(ctx, watch, "✅ Approved — grabbing…")
        return
    final = _FINAL_LINES.get(nt)
    if final is None:
        return
    for watch in watches:
        # Delete first: the poller guards its progress edits on the row
        # still existing, so removing the row before painting the terminal
        # line closes the overwrite race (a mid-tick poller would otherwise
        # stamp stale progress over the final state).
        await store.delete_request_watch(watch["id"])
        await _edit_card(ctx, watch, final, footer=False)
