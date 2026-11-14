"""Ask the Admin to try a different release.

A Reporter whose Auto-fix brought back the same file (or who said "No, add a
comment") taps one button; the Admin gets a DM built like the new-Ticket DM,
plus the release that came back and a Mark Failed button first. The ask is
sent whatever the Ticket DM class switch says (the Reporter asked for it),
once per Ticket: the claim lives in the store, so it survives a restart.
"""
from __future__ import annotations

import html
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from fix_result import ReleaseFingerprint

from bot.callback_prefixes import ASK_ADMIN, TK_FIX_MARK_FAILED
from bot.shared import (
    end_action,
    format_scope_label,
    new_ticket_dm_kb,
    record_btn,
    truncate_message,
    try_begin_action,
)

logger = logging.getLogger("usher")

ALREADY_SENT = "Already sent to the Admin"


def ask_admin_button(ticket_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton("🙋 Ask the Admin to try a different release",
                                callback_data=f"{ASK_ADMIN}:{ticket_id}")


def mark_failed_button(ticket_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton("🚫 Mark Failed",
                                callback_data=f"{TK_FIX_MARK_FAILED}:{ticket_id}")


def release_description(fp: ReleaseFingerprint) -> str:
    """The release as one plain line: name, then group, quality and size."""
    parts = [p for p in (
        fp.group, fp.quality,
        f"{fp.size / 1024 ** 3:.2f} GB" if fp.size else None) if p]
    detail = f" ({', '.join(parts)})" if parts else ""
    return f"{fp.release_name or 'Unknown release name'}{detail}"


async def _release_that_came_back(ctx, ticket_id: int) -> Optional[ReleaseFingerprint]:
    """The file on disk now for the Ticket's latest Fix, read live; None when
    there is no Fix row, no client, or the read fails."""
    store = ctx.bot_data["store"]
    try:
        fix = await store.latest_autofix_for_ticket(ticket_id)
        if fix is None:
            return None
        radarr = ctx.bot_data.get("radarr")
        sonarr = ctx.bot_data.get("sonarr")
        if fix.media_type == "movie" and radarr and fix.radarr_movie_id:
            fp = await radarr.read_fingerprint(fix.radarr_movie_id)
        elif fix.media_type == "tv" and sonarr and fix.sonarr_episode_id:
            fp = await sonarr.read_fingerprint(fix.sonarr_episode_id)
        else:
            return None
    except Exception as exc:
        logger.warning("ask admin: couldn't read the release for #%d: %s", ticket_id, exc)
        return None
    return fp if fp.known else None


async def _build_admin_dm(ctx, ticket_id: int) -> tuple[str, InlineKeyboardMarkup]:
    seerr = ctx.bot_data["seerr"]
    ticket = await seerr.get_issue(ticket_id)
    title = ""
    try:
        name, year = await seerr.get_media_title(ticket.media_type, ticket.tmdb_id)
        emoji = "🎬" if ticket.media_type == "movie" else "📺"
        title = f"{emoji} {name}" + (f" ({year})" if year else "")
    except Exception as exc:
        logger.warning("ask admin: couldn't fetch the title for #%d: %s", ticket_id, exc)
    scope = format_scope_label(ticket.media_type, ticket.problem_season,
                               ticket.problem_episode)
    lines = [
        f"🙋 Reporter asks for a different release: ticket <b>#{ticket_id}</b>",
        "",
        html.escape(title) if title else "(unknown media)",
    ]
    if scope:
        lines.append(html.escape(scope))
    lines += [
        "",
        f"<b>Reported by:</b> {html.escape(ticket.created_by or 'unknown')}",
        f"Ticket: {html.escape(f'{seerr.public_url}/issues/{ticket_id}')}",
    ]
    fp = await _release_that_came_back(ctx, ticket_id)
    if fp is not None:
        lines += ["", f"<b>Release that came back:</b> {html.escape(release_description(fp))}"]
    base = new_ticket_dm_kb(ticket_id)
    kb = InlineKeyboardMarkup([[mark_failed_button(ticket_id)],
                               *base.inline_keyboard])
    return truncate_message("\n".join(lines)), kb


async def ask_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Reporter tapped "Ask the Admin to try a different release"."""
    q = update.callback_query
    try:
        ticket_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await q.answer()
        logger.warning("Unparseable callback data: %r", q.data)
        return
    store = ctx.bot_data["store"]
    admin_id = ctx.bot_data.get("admin_id")
    key = f"ask:{ticket_id}"
    if not try_begin_action(ctx, key):
        await q.answer(ALREADY_SENT)
        return
    try:
        if not admin_id:
            await q.answer("There is no Admin to ask.", show_alert=True)
            return
        if not await store.claim_ticket_escalation(ticket_id):
            await q.answer(ALREADY_SENT)
            return
        try:
            text, kb = await _build_admin_dm(ctx, ticket_id)
            sent = await ctx.bot.send_message(
                chat_id=admin_id, text=text, parse_mode="HTML",
                disable_web_page_preview=True, reply_markup=kb)
            record_btn(ctx.application, admin_id, sent)
        except Exception as exc:
            logger.exception("ask admin failed for #%d", ticket_id, exc_info=exc)
            await store.release_ticket_escalation(ticket_id)
            await q.answer("Couldn't reach the Admin. Try again in a moment.",
                           show_alert=True)
            return
        await q.answer()
        await q.message.reply_text(
            f"📨 Sent to the Admin. Ticket #{ticket_id}: they can try a different release.")
    finally:
        end_action(ctx, key)
