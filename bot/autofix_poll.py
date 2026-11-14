"""Pending auto-fix completion poller and notification helpers.

DM conventions (matching bot.webhook_handlers): parse_mode="HTML" with
html.escape on every interpolated field -- media titles routinely contain
Markdown metacharacters (M*A*S*H) that would kill the send under legacy
Markdown. Every keyboard-bearing send must be passed to record_btn or the
global button gate rejects its taps (see bot.shared.record_btn).
"""
from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from fix_kinds import (KIND_MARK_FAILED, OUTCOME_COMPLETE, OUTCOME_FAILED,
                       OUTCOME_TIMEOUT)
from fix_result import ReleaseFingerprint, compare_fingerprints
from http_util import NotFoundAPIError, TransientAPIError
from radarr import RadarrClient
from sonarr import SonarrClient
from store import UserStore

from bot.ask_admin import (ask_admin_button, mark_failed_button,
                           release_description)
from bot.callback_prefixes import RESOLVE
from bot.progress_card import (maybe_bump_priority, refresh_arr_downloads,
                               resolve_progress_line)
from bot.shared import (end_action, media_action_key, record_btn,
                        try_begin_action)
from bot.tickets import (enqueue_fix_completion, resolve_fix_context,
                         run_arr_action)
from const import AUTOFIX_TIMEOUT_HOURS

logger = logging.getLogger("usher")

# Module-level set of fix IDs currently being processed by a tick. If a single
# tick's await chain stretches past the next poll mark (slow Sonarr/Radarr), the
# next tick sees the ID still in-flight and skips it -- otherwise we'd
# double-notify on a fix that completes mid-tick.
_inflight: set[int] = set()


async def poll_pending_autofixes(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Check on each pending auto-fix; notify when complete or timed out."""
    store: UserStore = ctx.bot_data["store"]
    pending = await store.list_pending_autofixes()
    if not pending:
        return
    logger.debug("Polling %d pending auto-fixes", len(pending))
    await refresh_arr_downloads(
        ctx,
        movies=any(f.media_type == "movie" for f in pending),
        tv=any(f.media_type == "tv" for f in pending))

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
                # The timestamp is the ONLY time bound on a fix. Leaving
                # timed_out False on a corrupt value re-polled the row every
                # tick forever; treat it as timed out so it
                # exits the poll set through the normal marked path.
                logger.exception(
                    "timeout parse failed for fix %d; treating as timed out", fix.id)
                timed_out = True
            if timed_out:
                try:
                    await store.mark_autofix_status(fix.id, OUTCOME_TIMEOUT)
                except Exception:
                    logger.exception("couldn't mark fix %d timed out; retrying next tick", fix.id)
                    continue
                await _paint_progress(
                    ctx, store, fix,
                    final=f"⏱️ Timed out after {AUTOFIX_TIMEOUT_HOURS}h, no new file imported.")
                await _notify_timeout(ctx, fix)
                continue

            # Poll for completion (dispatch lives on PendingAutofix.is_complete)
            try:
                done, extra = await fix.is_complete(radarr, sonarr)
                if done:
                    await store.mark_autofix_status(fix.id, OUTCOME_COMPLETE)
                    repeat = await _detect_repeat(fix, radarr, sonarr)
                    refunded = False
                    if repeat is not None:
                        # The Admin's own Fixes are never refunded (they
                        # are not counted against a limit). The DM only
                        # claims a refund that was actually recorded.
                        # The bot's own Mark Failed (blocklist-on-repeat)
                        # is not the Reporter's use either.
                        refunded = await _record_repeat(
                            store, fix,
                            refund=(fix.user_id != ctx.bot_data.get("admin_id")
                                    and not fix.auto_blocklisted))
                        await _paint_progress(
                            ctx, store, fix,
                            final=f"🔁 {_repeat_sentence(repeat)}")
                    else:
                        await _paint_progress(ctx, store, fix,
                                              final="🎉 Replacement downloaded.")
                    retrying = (repeat is not None
                                and await _blocklist_on_repeat(
                                    ctx, store, fix, repeat))
                    await _notify_complete(
                        ctx, fix, extra, repeat=repeat,
                        refunded=refunded and _limit_enforced(ctx),
                        retrying=retrying)
                else:
                    await _paint_progress(ctx, store, fix)
            except NotFoundAPIError:
                # Media was deleted from Sonarr/Radarr between enqueue and poll.
                logger.info("poll: media removed for fix %d; marking failed", fix.id)
                await store.mark_autofix_status(fix.id, OUTCOME_FAILED)
                await _notify_media_gone(ctx, fix)
            except TransientAPIError as exc:
                logger.debug("poll: transient error for fix %d; will retry next tick: %s", fix.id, exc)
            except Exception as exc:
                logger.exception("poll failed for fix %d", fix.id, exc_info=exc)
        finally:
            _inflight.discard(fix.id)


async def _detect_repeat(fix, radarr, sonarr) -> Optional[ReleaseFingerprint]:
    """The replacement's fingerprint when it looks like the same release as
    the file the Fix deleted, else None. An unknown comparison (nothing was
    read before, or the read after failed) is not a repeat: it must never
    block or change a completion."""
    if not fix.before_fingerprint.known:
        return None
    try:
        if fix.media_type == "movie" and radarr and fix.radarr_movie_id:
            after = await radarr.read_fingerprint(fix.radarr_movie_id)
        elif fix.media_type == "tv" and sonarr and fix.sonarr_episode_id:
            after = await sonarr.read_fingerprint(fix.sonarr_episode_id)
        else:
            return None
    except Exception as exc:
        logger.warning("repeat check: couldn't read the new file for fix %d: %s",
                       fix.id, exc)
        return None
    if compare_fingerprints(fix.before_fingerprint, after) == "same":
        return after
    return None


async def _record_repeat(store: UserStore, fix, *, refund: bool) -> bool:
    """Record the repeat outcome on the Fix's event (and the refund when
    `refund`). Returns whether the use was really given back. The completion
    is already marked; a failure here costs the refund and the 'repeat'
    outcome mark, and is logged."""
    try:
        await store.mark_autofix_repeat(fix.id, refunded=refund)
    except Exception:
        logger.exception("couldn't record the repeat for fix %d", fix.id)
        return False
    return refund


async def _blocklist_on_repeat(ctx, store: UserStore, fix,
                               repeat: ReleaseFingerprint) -> bool:
    """With the "Auto-fix blocklists on a repeat" switch on, run Mark Failed
    once for a Reporter's repeat: the same workflow and per-title
    serialization as an Admin's Mark Failed, enqueued as a new Fix row marked
    so its own repeat never comes back here. The Admin is DMed what was
    blocklisted. Returns True when a search for a different release is
    running (the Reporter is told). Never raises: the completion DM must go
    out whatever happens here."""
    cfg = ctx.bot_data["settings_store"].settings
    admin_id = ctx.bot_data.get("admin_id")
    if (not getattr(cfg, "autofix_blocklist_on_repeat", False)
            or fix.user_id == admin_id or fix.auto_blocklisted):
        return False
    blocklisted = False  # Mark Failed ran: a later error is not a skip.
    try:
        fix_ctx, err = await resolve_fix_context(
            ctx.bot_data["seerr"], fix.ticket_id, action_name="Mark Failed")
        if err is not None:
            logger.info("blocklist on repeat skipped for #%d: %s", fix.ticket_id, err)
            await _notify_admin_skipped(ctx, fix, repeat, err)
            return False
        assert fix_ctx is not None
        key = media_action_key(fix_ctx.media)
        if not try_begin_action(ctx, key):
            logger.info("blocklist on repeat skipped for #%d: another fix is running",
                        fix.ticket_id)
            await _notify_admin_skipped(ctx, fix, repeat,
                                        "another fix is running for this title.")
            return False
        try:
            result = await run_arr_action(
                fix_ctx.media, fix_ctx.season, fix_ctx.episode,
                ctx.bot_data.get("radarr"), ctx.bot_data.get("sonarr"),
                action="mark_failed")
            if result.status == "failed":
                logger.warning("blocklist on repeat failed for #%d: %s",
                               fix.ticket_id, result.message)
                await _notify_admin_skipped(
                    ctx, fix, repeat, f"Mark Failed failed: {result.message}")
                return False
            blocklisted = True
            # Logged as the Admin's Fix (not counted against anyone's limit,
            # and the retry warning still sees it).
            event_id = await store.log_autofix(
                admin_id, fix_ctx.media["type"], fix_ctx.media["tmdb_id"],
                season=fix_ctx.season, episode=fix_ctx.episode,
                kind=KIND_MARK_FAILED)
            searching = False
            if result.should_poll:
                try:
                    await enqueue_fix_completion(
                        store, fix=fix_ctx, result=result, chat_id=fix.chat_id,
                        user_id=fix.user_id, ticket_url=fix.ticket_url,
                        event_id=event_id, auto_blocklisted=True)
                    searching = True
                except Exception:
                    logger.exception("couldn't enqueue the blocklist-on-repeat "
                                     "fix for #%d", fix.ticket_id)
        finally:
            end_action(ctx, key)
        await _notify_admin_blocklisted(ctx, fix, repeat, result.message)
        return searching
    except Exception as exc:
        logger.exception("blocklist on repeat failed for #%d", fix.ticket_id)
        if not blocklisted:
            await _notify_admin_skipped(
                ctx, fix, repeat, f"an error came up ({type(exc).__name__}).")
        return False


async def _notify_admin_skipped(ctx, fix, repeat: ReleaseFingerprint,
                                reason: str) -> None:
    """Tell the Admin the switch did not act on a Reporter's repeat. Never
    raises: a failed send must not stop the Reporter's completion DM."""
    try:
        lines = [
            f"⚠️ Auto-fix could not blocklist a repeat on its own: "
            f"<b>{html.escape(fix.label)}</b>",
            f"A Reporter's Auto-fix brought back the same release: "
            f"{html.escape(release_description(repeat))}.",
            f"Why: {html.escape(reason)}",
            *_ticket_line(fix),
            "Use Mark Failed from the Ticket if you want it blocklisted.",
        ]
        await ctx.bot.send_message(chat_id=ctx.bot_data["admin_id"],
                                   text="\n".join(lines), parse_mode="HTML")
    except Exception as exc:
        logger.exception("notify admin of skipped blocklist failed for fix %d",
                         fix.id, exc_info=exc)


async def _notify_admin_blocklisted(ctx, fix, repeat: ReleaseFingerprint,
                                    message: str) -> None:
    lines = [
        f"🚫 Auto-fix blocklisted a release on its own: <b>{html.escape(fix.label)}</b>",
        f"A Reporter's Auto-fix brought back the same release: "
        f"{html.escape(release_description(repeat))}.",
        html.escape(message),
        *_ticket_line(fix),
        "If it was a good release, remove it from the blocklist in Sonarr or Radarr.",
    ]
    try:
        await ctx.bot.send_message(chat_id=ctx.bot_data["admin_id"],
                                   text="\n".join(lines), parse_mode="HTML")
    except Exception as exc:
        logger.exception("notify admin of blocklist failed for fix %d", fix.id,
                         exc_info=exc)


def _limit_enforced(ctx) -> bool:
    """Whether the daily Auto-fix limit applies (so a refund means something
    to the Reporter)."""
    cfg = ctx.bot_data["settings_store"].settings
    return not getattr(cfg, "daily_autofix_unlimited", False)


def _repeat_sentence(after: ReleaseFingerprint) -> str:
    """The repeat line, naming the group and quality when known."""
    known = [x for x in (after.group, after.quality) if x]
    named = f" ({html.escape(', '.join(known))})" if known else ""
    return (f"Replacement looks like the same release{named} "
            "and probably didn't fix the problem.")


async def _paint_progress(ctx: ContextTypes.DEFAULT_TYPE, store: UserStore,
                          fix, final: str = "") -> None:
    """Paint a fix's Progress card (the /issue confirmation, or the new
    message an admin Fix sends) through the replacement: live
    queue percent/ETA while downloading, a final line on completion or
    timeout. Each edit keeps the card's stored keyboard. Legacy rows (no
    message_id) keep the send-only behavior. All
    failures are non-fatal -- the completion/timeout DMs remain the
    guaranteed channel."""
    if not fix.message_id:
        return
    try:
        if final:
            line = final
        else:
            progress = None
            if fix.media_type == "movie" and fix.radarr_movie_id:
                radarr = ctx.bot_data.get("radarr")
                if radarr:
                    progress = await radarr.get_queue_progress(fix.radarr_movie_id)
            elif fix.media_type == "tv" and fix.sonarr_series_id:
                sonarr = ctx.bot_data.get("sonarr")
                if sonarr:
                    progress = await sonarr.get_queue_progress(
                        fix.sonarr_series_id,
                        [fix.sonarr_episode_id] if fix.sonarr_episode_id else None)
            if progress is None:
                line = "🔎 Searching for a replacement…"
            else:
                line = await resolve_progress_line(
                    ctx, progress, verb="Replacing",
                    fallback_line=fix.last_progress)
                if not fix.bumped and progress.download_ids:
                    if await maybe_bump_priority(ctx, progress.download_ids):
                        await store.mark_autofix_bumped(fix.id)
                        fix.bumped = 1
        if line == fix.last_progress:
            return
        # An edit without reply_markup drops the keyboard, so pass back the
        # one the card was sent with (a Hub /issue card's back button; a
        # typed /issue card and an admin Fix's card store none).
        markup = (InlineKeyboardMarkup.de_json(json.loads(fix.reply_markup),
                                               ctx.bot)
                  if fix.reply_markup else None)
        await ctx.bot.edit_message_text(
            chat_id=fix.chat_id,
            message_id=fix.message_id,
            text=f"{_progress_header(fix)}\n{line}",
            parse_mode="HTML",
            reply_markup=markup,
        )
        await store.set_autofix_progress(fix.id, line)
    except Exception as exc:
        logger.debug("autofix progress paint failed (non-fatal)", exc_info=exc)


def _fix_name(fix) -> str:
    """What a Fix is called to the person told about it: a Reporter's own
    Redownload is an "Auto-fix"; an Admin's Fix says its own name."""
    return html.escape(fix.action_name) if fix.action_name else "Auto-fix"


def _progress_header(fix) -> str:
    """The Progress card's first line. A Reporter's /issue Auto-fix says
    "Auto-fix"; an admin Fix says its own name, as its card did when painted."""
    label = f"<b>{html.escape(fix.label)}</b>"
    if fix.action_name:
        return f"🔧 {html.escape(fix.action_name)} for #{fix.ticket_id}: {label}"
    return f"🔧 Auto-fix: {label}"


def _ticket_line(fix) -> list[str]:
    """The "Ticket: <url>" line, or nothing for legacy rows enqueued
    before the admin-fix path learned to set ticket_url."""
    return [f"Ticket: {html.escape(fix.ticket_url)}"] if fix.ticket_url else []


async def _notify_media_gone(ctx: ContextTypes.DEFAULT_TYPE, fix) -> None:
    lines = [
        f"⚠️ {_fix_name(fix)} abandoned for <b>{html.escape(fix.label)}</b>.",
        "The media was removed from Sonarr/Radarr before the new file landed.",
        *_ticket_line(fix),
    ]
    try:
        await ctx.bot.send_message(chat_id=fix.chat_id, text="\n".join(lines), parse_mode="HTML")
    except Exception as exc:
        logger.exception("notify_media_gone send_message failed for fix %d", fix.id, exc_info=exc)


async def _notify_complete(ctx: ContextTypes.DEFAULT_TYPE, fix, extra: str = "",
                           repeat: Optional[ReleaseFingerprint] = None,
                           refunded: bool = False,
                           retrying: bool = False) -> None:
    if repeat is not None:
        head = [f"🔁 {_fix_name(fix)} complete: <b>{html.escape(fix.label)}</b> "
                f"downloaded{extra}.", _repeat_sentence(repeat)]
        if refunded:
            head.append("That use was given back, so it doesn't count "
                        "against your daily limit.")
        if retrying:
            head.append("I'm trying a different release now and will DM you "
                        "when it lands.")
    else:
        head = [f"🎉 {_fix_name(fix)} complete: <b>{html.escape(fix.label)}</b> downloaded{extra}."]
    lines = [
        *head,
        *_ticket_line(fix),
        "",
        "Did this resolve the problem?",
    ]
    row = [InlineKeyboardButton("💬 No, add a comment", callback_data=f"{RESOLVE}:{fix.ticket_id}:no")]
    if not retrying:
        # No close button while the bot's own retry runs: the Ticket is not
        # done until that retry lands.
        row.insert(0, InlineKeyboardButton("✅ Yes, close it", callback_data=f"{RESOLVE}:{fix.ticket_id}:yes"))
    keyboard = [row]
    if repeat is not None and not retrying:
        # The Admin's own repeat offers Mark Failed; a Reporter asks the Admin
        # (not while the bot is already trying a different release).
        is_admin = fix.user_id == ctx.bot_data.get("admin_id")
        keyboard.append([mark_failed_button(fix.ticket_id) if is_admin
                         else ask_admin_button(fix.ticket_id)])
    try:
        sent = await ctx.bot.send_message(
            chat_id=fix.chat_id,
            text="\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        record_btn(ctx.application, fix.user_id, sent)
    except Exception as exc:
        logger.exception("notify_complete send_message failed for fix %d", fix.id, exc_info=exc)


async def _notify_timeout(ctx: ContextTypes.DEFAULT_TYPE, fix) -> None:
    # The admin follows up on their own Tickets; don't tell them to comment
    # "for the admin".
    is_admin = fix.user_id == ctx.bot_data.get("admin_id")
    lines = [
        f"⏱️ {_fix_name(fix)} timed out ({AUTOFIX_TIMEOUT_HOURS}h) for <b>{html.escape(fix.label)}</b>.",
        "No new file was imported."
        + (" Check Sonarr/Radarr to see if a release was grabbed." if is_admin else ""),
        *_ticket_line(fix),
        "",
        "Want to add a note to the ticket?" if is_admin
        else "Want to add a comment for the admin to follow up?",
    ]
    keyboard = [[
        InlineKeyboardButton("💬 Add a comment", callback_data=f"{RESOLVE}:{fix.ticket_id}:no"),
        InlineKeyboardButton("🙅 No, leave it", callback_data=f"{RESOLVE}:{fix.ticket_id}:skip"),
    ]]
    try:
        sent = await ctx.bot.send_message(
            chat_id=fix.chat_id,
            text="\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        record_btn(ctx.application, fix.user_id, sent)
    except Exception as exc:
        logger.exception("notify_timeout send_message failed for fix %d", fix.id, exc_info=exc)


