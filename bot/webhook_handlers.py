"""Handlers for the three Seerr webhook events: ISSUE_COMMENT,
ISSUE_RESOLVED, ISSUE_CREATED."""
from __future__ import annotations

import html
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from seerr import SeerrClient
from store import UserStore

from bot.callback_prefixes import TK_CLOSE, TK_FIX, TK_REPLY
from bot.shared import (
    ISSUE_TYPE_LABELS,
    format_media_title_line,
    format_se_suffix,
    record_btn,
)

logger = logging.getLogger("hermes")


async def handle_seerr_comment(app: Application, payload: dict) -> None:
    """Process an ISSUE_COMMENT webhook and notify the other party in the
    conversation: the reporter when someone else comments, and the admin when
    the reporter (or a third party) comments. Whoever wrote the comment is
    never notified about their own comment."""
    issue = payload.get("issue") or {}
    comment = payload.get("comment") or {}
    media = payload.get("media") or {}

    try:
        issue_id = int(issue.get("issue_id"))
    except (TypeError, ValueError):
        logger.warning("Webhook comment: missing/invalid issue_id; dropping")
        return

    reporter_username = (issue.get("reportedBy_username") or "").strip()
    commenter_username = (comment.get("commentedBy_username") or "").strip()
    comment_text = (comment.get("comment_message") or "").strip()

    if not reporter_username:
        logger.info("Webhook comment on issue #%d: no reporter username; dropping", issue_id)
        return
    if not comment_text:
        logger.info("Webhook comment on issue #%d: empty comment; dropping", issue_id)
        return

    store: UserStore = app.bot_data["store"]
    admin_id = app.bot_data.get("admin_id")

    # Resolve the admin's Plex username so we can tell whether the admin wrote
    # this comment (and must therefore not be notified about it).
    admin_mapping = await store.get(admin_id) if admin_id else None
    admin_plex = (admin_mapping.plex_username if admin_mapping else "") or ""
    commenter_is_admin = bool(
        commenter_username and admin_plex
        and commenter_username.lower() == admin_plex.lower()
    )
    commenter_is_reporter = bool(
        commenter_username
        and commenter_username.lower() == reporter_username.lower()
    )

    seerr: Optional[SeerrClient] = app.bot_data.get("seerr")
    title_line = await format_media_title_line(
        seerr, media,
        problem_season=issue.get("problemSeason"),
        problem_episode=issue.get("problemEpisode"),
    )

    safe_comment = html.escape(comment_text)
    safe_commenter = html.escape(commenter_username or "Seerr")
    safe_title = html.escape(title_line) if title_line else ""

    lines = [f"💬 New comment on issue #{issue_id}"]
    if safe_title:
        lines.append(safe_title)
    lines.append("")
    lines.append(f"<b>From:</b> {safe_commenter}")
    lines.append("")
    lines.append("<b>Comment:</b>")
    lines.append(f"<i>\"{safe_comment}\"</i>")
    text = "\n".join(lines)

    # Offer an inline Reply button when the ticket is still open
    issue_status = (issue.get("issue_status") or "").upper()

    async def _notify(chat_id: int, who: str) -> None:
        reply_kb = None
        if issue_status == "OPEN":
            reply_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("💬 Reply", callback_data=f"{TK_REPLY}:{issue_id}"),
            ]])
        try:
            sent = await app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=reply_kb,
            )
            if reply_kb is not None:
                record_btn(app, chat_id, sent)
            logger.info(
                "Notified %s telegram_id=%d of comment on issue #%d from '%s'",
                who, chat_id, issue_id, commenter_username,
            )
        except Exception:
            logger.exception(
                "Failed to DM %s telegram_id=%d about issue #%d comment",
                who, chat_id, issue_id,
            )

    # Notify the reporter unless they wrote the comment.
    reporter_tid = None
    if not commenter_is_reporter:
        mapping = await store.find_by_plex_username(reporter_username)
        if mapping is None:
            logger.info(
                "Webhook comment on issue #%d: reporter '%s' not linked; "
                "skipping reporter notify", issue_id, reporter_username,
            )
        else:
            reporter_tid = mapping.telegram_id
            await _notify(mapping.telegram_id, "reporter")

    # Notify the admin unless they wrote the comment or are the reporter
    # (already notified just above).
    if admin_id and not commenter_is_admin and admin_id != reporter_tid:
        await _notify(admin_id, "admin")


async def handle_seerr_resolved(app: Application, payload: dict) -> None:
    """Process an ISSUE_RESOLVED webhook and DM the reporter (and the admin
    unless admin IS the reporter)."""
    issue = payload.get("issue") or {}
    media = payload.get("media") or {}

    try:
        issue_id = int(issue.get("issue_id"))
    except (TypeError, ValueError):
        logger.warning("Webhook resolved: missing/invalid issue_id; dropping")
        return

    reporter_username = (issue.get("reportedBy_username") or "").strip()
    if not reporter_username:
        logger.info("Webhook resolved on issue #%d: no reporter username; dropping", issue_id)
        return

    store: UserStore = app.bot_data["store"]
    mapping = await store.find_by_plex_username(reporter_username)
    if mapping is None:
        logger.info(
            "Webhook resolved on issue #%d: reporter '%s' not linked in Hermes",
            issue_id, reporter_username,
        )

    seerr: Optional[SeerrClient] = app.bot_data.get("seerr")
    title_line = await format_media_title_line(
        seerr, media,
        problem_season=issue.get("problemSeason"),
        problem_episode=issue.get("problemEpisode"),
    )

    safe_title = html.escape(title_line) if title_line else ""
    safe_reporter = html.escape(reporter_username)
    admin_id = app.bot_data.get("admin_id")

    # DM the reporter (if they're linked)
    if mapping is not None:
        reporter_lines = [f"✅ Your issue #{issue_id} was resolved."]
        if safe_title:
            reporter_lines.append(safe_title)
        try:
            await app.bot.send_message(
                chat_id=mapping.telegram_id,
                text="\n".join(reporter_lines),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            logger.info("Notified telegram_id=%d of resolved issue #%d",
                        mapping.telegram_id, issue_id)
        except Exception:
            logger.exception(
                "Failed to DM telegram_id=%d about resolved issue #%d",
                mapping.telegram_id, issue_id,
            )

    # Also DM the admin (unless admin IS the reporter)
    if admin_id and (mapping is None or mapping.telegram_id != admin_id):
        admin_lines = [f"✅ Issue #{issue_id} resolved"]
        if safe_title:
            admin_lines.append(safe_title)
        admin_lines.append(f"<b>Reported by:</b> {safe_reporter}")
        try:
            await app.bot.send_message(
                chat_id=admin_id,
                text="\n".join(admin_lines),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            logger.info("Notified admin of resolved issue #%d (reported by '%s')",
                        issue_id, reporter_username)
        except Exception:
            logger.exception("Failed to DM admin about resolved issue #%d", issue_id)


async def handle_seerr_reported(app: Application, payload: dict) -> None:
    """Process an ISSUE_CREATED/ISSUE_REPORTED webhook and DM the admin
    (unless admin filed the issue themselves)."""
    issue = payload.get("issue") or {}
    media = payload.get("media") or {}
    description = (payload.get("message") or "").strip()

    try:
        issue_id = int(issue.get("issue_id"))
    except (TypeError, ValueError):
        logger.warning("Webhook reported: missing/invalid issue_id; dropping")
        return

    reporter_username = (issue.get("reportedBy_username") or "").strip()
    if not reporter_username:
        logger.info("Webhook reported on issue #%d: no reporter username; dropping", issue_id)
        return

    admin_id = app.bot_data.get("admin_id")
    if not admin_id:
        return

    # Skip if admin filed it themselves -- they already saw the /issue confirmation
    store: UserStore = app.bot_data["store"]
    admin_mapping = await store.get(admin_id)
    if (admin_mapping and admin_mapping.plex_username
            and admin_mapping.plex_username.lower() == reporter_username.lower()):
        logger.info("ISSUE_REPORTED #%d filed by admin themselves; not DMing", issue_id)
        return

    seerr: Optional[SeerrClient] = app.bot_data.get("seerr")
    # The reported flow shows just the media title; S/E suffix appended below
    # so the unconditional season-bit join in the legacy code is preserved.
    base_title = await format_media_title_line(seerr, media)
    se_suffix = format_se_suffix(issue.get("problemSeason"), issue.get("problemEpisode"))
    title_line = base_title
    if base_title and se_suffix:
        title_line = f"{base_title} — {se_suffix}"

    issue_type_str = (issue.get("issue_type") or "OTHER").upper()
    type_emoji, type_label = ISSUE_TYPE_LABELS.get(issue_type_str, ("❓", "Other"))

    safe_reporter = html.escape(reporter_username)
    safe_desc = html.escape(description) if description else "(no description)"
    safe_title = html.escape(title_line) if title_line else "(unknown media)"

    lines = [
        f"🆕 New issue <b>#{issue_id}</b>",
        "",
        safe_title,
        "",
        f"<b>Issue type:</b> {type_emoji} {type_label}",
        f"<b>Reported by:</b> {safe_reporter}",
        "<b>Status:</b> Open",
        "",
        "<b>Description:</b>",
        f"<i>\"{safe_desc}\"</i>",
    ]

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 Reply", callback_data=f"{TK_REPLY}:{issue_id}"),
        InlineKeyboardButton("🔧 Fix", callback_data=f"{TK_FIX}:{issue_id}"),
        InlineKeyboardButton("✅ Close", callback_data=f"{TK_CLOSE}:{issue_id}"),
    ]])

    try:
        sent = await app.bot.send_message(
            chat_id=admin_id,
            text="\n".join(lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb,
        )
        record_btn(app, admin_id, sent)
        logger.info("Notified admin of new issue #%d from '%s'", issue_id, reporter_username)
    except Exception:
        logger.exception("Failed to DM admin about new issue #%d", issue_id)
