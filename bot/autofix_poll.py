"""Pending auto-fix completion poller and notification helpers."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from http_util import NotFoundAPIError, TransientAPIError
from radarr import RadarrClient
from sonarr import SonarrClient
from store import UserStore

logger = logging.getLogger("hermes")

async def poll_pending_autofixes(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Check on each pending auto-fix; notify when complete or timed out."""
    store: UserStore = ctx.bot_data["store"]
    radarr: Optional[RadarrClient] = ctx.bot_data.get("radarr")
    sonarr: Optional[SonarrClient] = ctx.bot_data.get("sonarr")
    pending = await store.list_pending_autofixes()
    if not pending:
        return
    logger.debug("Polling %d pending auto-fixes", len(pending))
    from datetime import datetime, timezone

    for fix in pending:
        # Check timeout first
        try:
            timeout_at = datetime.fromisoformat(fix.timeout_at.replace(" ", "T")).replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= timeout_at:
                await _notify_timeout(ctx, fix)
                await store.mark_autofix_status(fix.id, "timeout")
                continue
        except Exception:
            logger.exception("timeout parse failed for fix %d", fix.id)

        # Poll for completion
        try:
            done = False
            extra = ""
            if fix.media_type == "movie" and radarr and fix.radarr_movie_id:
                done = await radarr.movie_has_file(fix.radarr_movie_id)
            elif fix.media_type == "tv" and sonarr:
                if fix.sonarr_episode_id:
                    done = await sonarr.episode_has_file(fix.sonarr_episode_id)
                elif fix.sonarr_series_id and fix.sonarr_season and fix.expected_episode_ids:
                    present, total = await sonarr.season_files_present(
                        fix.sonarr_series_id, fix.sonarr_season, fix.expected_episode_ids
                    )
                    done = present >= total and total > 0
                    extra = f" ({present}/{total} episodes)"
            if done:
                await _notify_complete(ctx, fix, extra)
                await store.mark_autofix_status(fix.id, "complete")
        except NotFoundAPIError:
            # Media was deleted from Sonarr/Radarr between enqueue and poll.
            # Mark failed and DM the user instead of polling forever.
            logger.info("poll: media removed for fix %d; marking failed", fix.id)
            await _notify_media_gone(ctx, fix)
            await store.mark_autofix_status(fix.id, "failed")
        except TransientAPIError:
            # Service hiccup — keep polling next tick.
            logger.debug("poll: transient error for fix %d; will retry next tick", fix.id)
        except Exception:
            logger.exception("poll failed for fix %d", fix.id)


async def _notify_media_gone(ctx: ContextTypes.DEFAULT_TYPE, fix) -> None:
    text = (
        f"⚠️ Auto-fix abandoned for *{fix.label}*.\n"
        "The media was removed from Sonarr/Radarr before the new file landed. "
        f"Original issue: {fix.issue_url}"
    )
    try:
        await ctx.bot.send_message(chat_id=fix.chat_id, text=text, parse_mode="Markdown")
    except Exception:
        logger.exception("notify_media_gone send_message failed for fix %d", fix.id)


async def _notify_complete(ctx: ContextTypes.DEFAULT_TYPE, fix, extra: str = "") -> None:
    text = (
        f"🎉 Auto-fix complete: *{fix.label}* downloaded{extra}.\n"
        f"Original issue: {fix.issue_url}\n\n"
        "Did this resolve the problem?"
    )
    keyboard = [[
        InlineKeyboardButton("✅ Yes, close it", callback_data=f"resolve:{fix.issue_id}:yes"),
        InlineKeyboardButton("💬 No, add a comment", callback_data=f"resolve:{fix.issue_id}:no"),
    ]]
    try:
        await ctx.bot.send_message(
            chat_id=fix.chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception:
        logger.exception("notify_complete send_message failed for fix %d", fix.id)


async def _notify_timeout(ctx: ContextTypes.DEFAULT_TYPE, fix) -> None:
    text = (
        f"⏱️ Auto-fix timed out (6h) for *{fix.label}*.\n"
        f"No new file was imported. Check Sonarr/Radarr to see if a release was grabbed.\n"
        f"Original issue: {fix.issue_url}\n\n"
        "Want to add a comment for the admin to follow up?"
    )
    keyboard = [[
        InlineKeyboardButton("💬 Add a comment", callback_data=f"resolve:{fix.issue_id}:no"),
        InlineKeyboardButton("🙅 No, leave it", callback_data=f"resolve:{fix.issue_id}:skip"),
    ]]
    try:
        await ctx.bot.send_message(
            chat_id=fix.chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception:
        logger.exception("notify_timeout send_message failed for fix %d", fix.id)


