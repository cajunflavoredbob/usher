"""Pending auto-fix completion poller and notification helpers.

DM conventions (matching bot.webhook_handlers): parse_mode="HTML" with
html.escape on every interpolated field -- media titles routinely contain
Markdown metacharacters (M*A*S*H) that would kill the send under legacy
Markdown. Every keyboard-bearing send must be passed to record_btn or the
global button gate rejects its taps (see bot.shared.record_btn).
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from http_util import NotFoundAPIError, TransientAPIError
from radarr import RadarrClient
from sonarr import SonarrClient
from store import UserStore

from bot.callback_prefixes import RESOLVE
from bot.shared import record_btn
from const import AUTOFIX_TIMEOUT_HOURS

logger = logging.getLogger("hermes")

# Module-level set of fix IDs currently being processed by a tick. If a single
# tick's await chain stretches past the next 60s mark (slow Sonarr/Radarr), the
# next tick sees the ID still in-flight and skips it -- otherwise we'd
# double-notify on a fix that completes mid-tick (audit CONC #8).
_inflight: set[int] = set()


async def poll_pending_autofixes(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Check on each pending auto-fix; notify when complete or timed out."""
    store: UserStore = ctx.bot_data["store"]
    pending = await store.list_pending_autofixes()
    if not pending:
        return
    logger.debug("Polling %d pending auto-fixes", len(pending))

    for fix in pending:
        if fix.id in _inflight:
            logger.debug("poll: fix %d still in-flight from prior tick; skipping", fix.id)
            continue
        _inflight.add(fix.id)
        try:
            # Re-fetch the arr clients each iteration so a settings reload
            # mid-tick picks up the new clients on the very next fix.
            radarr: Optional[RadarrClient] = ctx.bot_data.get("radarr")
            sonarr: Optional[SonarrClient] = ctx.bot_data.get("sonarr")

            # Check timeout first. Status is marked BEFORE notifying: a failed
            # status write must not re-send the same DM every tick, so we
            # prefer losing one notification over spamming (same policy as
            # the webhook dedupe).
            timed_out = False
            try:
                timeout_at = datetime.fromisoformat(fix.timeout_at.replace(" ", "T")).replace(tzinfo=timezone.utc)
                timed_out = datetime.now(timezone.utc) >= timeout_at
            except Exception:
                logger.exception("timeout parse failed for fix %d", fix.id)
            if timed_out:
                try:
                    await store.mark_autofix_status(fix.id, "timeout")
                except Exception:
                    logger.exception("couldn't mark fix %d timed out; retrying next tick", fix.id)
                    continue
                await _notify_timeout(ctx, fix)
                continue

            # Poll for completion (dispatch lives on PendingAutofix.is_complete)
            try:
                done, extra = await fix.is_complete(radarr, sonarr)
                if done:
                    await store.mark_autofix_status(fix.id, "complete")
                    await _notify_complete(ctx, fix, extra)
            except NotFoundAPIError:
                # Media was deleted from Sonarr/Radarr between enqueue and poll.
                logger.info("poll: media removed for fix %d; marking failed", fix.id)
                await store.mark_autofix_status(fix.id, "failed")
                await _notify_media_gone(ctx, fix)
            except TransientAPIError:
                logger.debug("poll: transient error for fix %d; will retry next tick", fix.id)
            except Exception:
                logger.exception("poll failed for fix %d", fix.id)
        finally:
            _inflight.discard(fix.id)


def _issue_line(fix) -> list[str]:
    """The "Original issue: <url>" line, or nothing for legacy rows enqueued
    before the admin-fix path learned to set issue_url."""
    return [f"Original issue: {html.escape(fix.issue_url)}"] if fix.issue_url else []


async def _notify_media_gone(ctx: ContextTypes.DEFAULT_TYPE, fix) -> None:
    lines = [
        f"⚠️ Auto-fix abandoned for <b>{html.escape(fix.label)}</b>.",
        "The media was removed from Sonarr/Radarr before the new file landed.",
        *_issue_line(fix),
    ]
    try:
        await ctx.bot.send_message(chat_id=fix.chat_id, text="\n".join(lines), parse_mode="HTML")
    except Exception:
        logger.exception("notify_media_gone send_message failed for fix %d", fix.id)


async def _notify_complete(ctx: ContextTypes.DEFAULT_TYPE, fix, extra: str = "") -> None:
    lines = [
        f"🎉 Auto-fix complete: <b>{html.escape(fix.label)}</b> downloaded{extra}.",
        *_issue_line(fix),
        "",
        "Did this resolve the problem?",
    ]
    keyboard = [[
        InlineKeyboardButton("✅ Yes, close it", callback_data=f"{RESOLVE}:{fix.issue_id}:yes"),
        InlineKeyboardButton("💬 No, add a comment", callback_data=f"{RESOLVE}:{fix.issue_id}:no"),
    ]]
    try:
        sent = await ctx.bot.send_message(
            chat_id=fix.chat_id,
            text="\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        record_btn(ctx.application, fix.user_id, sent)
    except Exception:
        logger.exception("notify_complete send_message failed for fix %d", fix.id)


async def _notify_timeout(ctx: ContextTypes.DEFAULT_TYPE, fix) -> None:
    # The admin follows up on their own issues; don't tell them to comment
    # "for the admin".
    is_admin = fix.user_id == ctx.bot_data.get("admin_id")
    lines = [
        f"⏱️ Auto-fix timed out ({AUTOFIX_TIMEOUT_HOURS}h) for <b>{html.escape(fix.label)}</b>.",
        "No new file was imported. Check Sonarr/Radarr to see if a release was grabbed.",
        *_issue_line(fix),
        "",
        "Want to add a note to the issue?" if is_admin
        else "Want to add a comment for the admin to follow up?",
    ]
    keyboard = [[
        InlineKeyboardButton("💬 Add a comment", callback_data=f"{RESOLVE}:{fix.issue_id}:no"),
        InlineKeyboardButton("🙅 No, leave it", callback_data=f"{RESOLVE}:{fix.issue_id}:skip"),
    ]]
    try:
        sent = await ctx.bot.send_message(
            chat_id=fix.chat_id,
            text="\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        record_btn(ctx.application, fix.user_id, sent)
    except Exception:
        logger.exception("notify_timeout send_message failed for fix %d", fix.id)


