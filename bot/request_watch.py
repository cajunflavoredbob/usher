"""Morphing request cards: the /request confirmation message edits in place
through the download's life (waiting → grabbing with live percent/ETA →
final state), plus the SABnzbd priority boost for bot-originated grabs.

The poller only paints PROGRESS; terminal states come from Seerr's webhook
events (approved/declined/available/failed), which also close the watch.
A watch that outlives its timeout gets one honest final edit and is
dropped -- the /requests list remains the source of truth."""
from __future__ import annotations

import html
import logging
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


def _progress_line(progress) -> str:
    line = f"⬇️ Downloading — {progress.percent}%"
    timeleft = (progress.timeleft or "").strip()
    if timeleft and timeleft != "00:00:00":
        line += f" · ~{timeleft} left"
    if progress.count > 1:
        line += f" · {progress.count} files"
    return line


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
        return  # not grabbed yet, or already imported (webhook will close)

    if not watch["bumped"]:
        if await maybe_bump_priority(ctx, progress.download_ids):
            await store.update_request_watch(watch["id"], bumped=1)

    line = _progress_line(progress)
    if line == watch["last_progress"]:
        return  # nothing changed; skip the edit
    if await _edit_card(ctx, watch, line):
        await store.update_request_watch(watch["id"], last_progress=line)


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
        await _edit_card(ctx, watch, final, footer=False)
        await store.delete_request_watch(watch["id"])
