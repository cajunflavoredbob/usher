"""Handlers for Seerr webhook events: the issue lifecycle (ISSUE_COMMENT,
ISSUE_RESOLVED, ISSUE_CREATED) and the request lifecycle (MEDIA_*)."""
from __future__ import annotations

import html
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from seerr import SeerrClient
from store import UserStore

from bot.callback_prefixes import TK_CLOSE, TK_FIX, TK_OPEN, TK_REPLY
from bot.shared import (
    ISSUE_TYPE_LABELS,
    extract_affected_se,
    followup_scope_label,
    format_media_title_line,
    format_scope_label,
    record_btn,
)

logger = logging.getLogger("usher")


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
    title_line = await format_media_title_line(seerr, media)
    # Comment payloads carry no affected season/episode; look it up rather
    # than letting absence render as "All seasons" on a per-episode ticket.
    scope_label = await followup_scope_label(
        seerr, payload, issue_id, media.get("media_type"),
    )

    safe_comment = html.escape(comment_text)
    safe_commenter = html.escape(commenter_username or "Seerr")
    safe_title = html.escape(title_line) if title_line else ""

    lines = [f"💬 New comment on ticket #{issue_id}"]
    if safe_title:
        lines.append(safe_title)
    if scope_label:
        lines.append(html.escape(scope_label))
    lines.append("")
    lines.append(f"<b>From:</b> {safe_commenter}")
    lines.append("")
    lines.append("<b>Comment:</b>")
    lines.append(f"<i>\"{safe_comment}\"</i>")
    text = "\n".join(lines)

    # Always offer History (the full comment chain); add Reply while open.
    issue_status = (issue.get("issue_status") or "").upper()
    row = []
    if issue_status == "OPEN":
        row.append(InlineKeyboardButton("💬 Reply", callback_data=f"{TK_REPLY}:{issue_id}"))
    row.append(InlineKeyboardButton("📜 History", callback_data=f"{TK_OPEN}:{issue_id}"))
    reply_kb = InlineKeyboardMarkup([row])

    async def _notify(chat_id: int, who: str) -> None:
        try:
            sent = await app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=reply_kb,
            )
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
            "Webhook resolved on issue #%d: reporter '%s' not linked in Usher",
            issue_id, reporter_username,
        )

    seerr: Optional[SeerrClient] = app.bot_data.get("seerr")
    title_line = await format_media_title_line(seerr, media)
    # Same as the comment path: resolved payloads don't carry the scope.
    scope_label = await followup_scope_label(
        seerr, payload, issue_id, media.get("media_type"),
    )

    safe_title = html.escape(title_line) if title_line else ""
    safe_scope = html.escape(scope_label) if scope_label else ""
    safe_reporter = html.escape(reporter_username)
    admin_id = app.bot_data.get("admin_id")

    # DM the reporter (if they're linked)
    if mapping is not None:
        reporter_lines = [f"✅ Your ticket #{issue_id} was resolved."]
        if safe_title:
            reporter_lines.append(safe_title)
        if safe_scope:
            reporter_lines.append(safe_scope)
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
        admin_lines = [f"✅ Ticket #{issue_id} resolved"]
        if safe_title:
            admin_lines.append(safe_title)
        if safe_scope:
            admin_lines.append(safe_scope)
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
    title_line = await format_media_title_line(seerr, media)
    season, episode = extract_affected_se(payload)
    scope_label = format_scope_label(media.get("media_type"), season, episode)

    issue_type_str = (issue.get("issue_type") or "OTHER").upper()
    type_emoji, type_label = ISSUE_TYPE_LABELS.get(issue_type_str, ("❓", "Other"))

    safe_reporter = html.escape(reporter_username)
    safe_desc = html.escape(description) if description else "(no description)"
    safe_title = html.escape(title_line) if title_line else "(unknown media)"

    lines = [
        f"🆕 New ticket <b>#{issue_id}</b>",
        "",
        safe_title,
    ]
    if scope_label:
        lines.append(html.escape(scope_label))
    lines += [
        "",
        f"<b>Issue type:</b> {type_emoji} {type_label}",
        f"<b>Reported by:</b> {safe_reporter}",
        "<b>Status:</b> Open",
        "",
        "<b>Description:</b>",
        f"<i>\"{safe_desc}\"</i>",
    ]

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💬 Reply", callback_data=f"{TK_REPLY}:{issue_id}"),
            InlineKeyboardButton("🔧 Fix", callback_data=f"{TK_FIX}:{issue_id}"),
            InlineKeyboardButton("✅ Close", callback_data=f"{TK_CLOSE}:{issue_id}"),
        ],
        [InlineKeyboardButton("📜 History", callback_data=f"{TK_OPEN}:{issue_id}")],
    ])

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


# --- Request lifecycle (MEDIA_*) --------------------------------------------

# Who hears about each request event. The requester is DMed only for state
# changes they didn't witness in the /request flow itself: an auto-approval
# fires seconds after the flow already said "Approved", so it stays
# admin-only, while approve/decline/available/failed arrive minutes-to-days
# later and deserve a DM. MEDIA_PENDING is the admin's action item.
_MEDIA_EVENTS = {
    #                (requester_line,                              admin_line)
    "MEDIA_PENDING": (None, "📥 New request — approve it in Seerr"),
    "MEDIA_APPROVED": ("✅ Your request was approved — it's being grabbed.", None),
    "MEDIA_AUTO_APPROVED": (None, "📥 New request (auto-approved)"),
    "MEDIA_DECLINED": ("🚫 Your request was declined.", None),
    "MEDIA_AVAILABLE": ("🎉 Your request is available in Plex!", None),
    "MEDIA_FAILED": ("⚠️ Your request failed to download. The admin has been notified.",
                     "⚠️ Request FAILED — check Radarr/Sonarr"),
    # Watchlist auto-requests: log-only for now (the user opted into
    # watchlist syncing in Seerr; a DM per synced item would be noise).
    "MEDIA_AUTO_REQUESTED": (None, None),
}

# The dispatch set in webhook.py and the handler table here are maintained
# in two files; fail LOUDLY at import if they ever drift apart.
from webhook import MEDIA_NOTIFICATION_TYPES as _DISPATCHED_MEDIA_TYPES  # noqa: E402

assert set(_MEDIA_EVENTS) == set(_DISPATCHED_MEDIA_TYPES), (
    "webhook.MEDIA_NOTIFICATION_TYPES and webhook_handlers._MEDIA_EVENTS "
    "have drifted apart")


def _requested_seasons_note(payload: dict) -> str:
    """"Requested Seasons" from the webhook's extra array, or ""."""
    for item in payload.get("extra") or []:
        # isinstance guard (same as extract_affected_se): a garbage extra
        # entry must degrade to no note, not kill both DMs.
        if not isinstance(item, dict):
            continue
        if item.get("name") == "Requested Seasons":
            value = str(item.get("value") or "").strip()
            if value:
                return f"Seasons: {value}"
    return ""


async def _resolve_requester(app: Application, request_block: dict,
                             fallback_username: str):
    """Resolve the webhook's requester to (mapping, requester_seerr_id).

    Primary path: fetch the request by id (admin key) and join on the
    numeric Seerr user id -- display names are user-editable in Seerr, so
    the username in the payload can be spoofed or simply customized, which
    mis-routes or suppresses DMs. Fallback when the fetch fails or the
    payload has no request id: the historical username match."""
    store: UserStore = app.bot_data["store"]
    seerr: Optional[SeerrClient] = app.bot_data.get("seerr")
    request_id = None
    try:
        request_id = int(request_block.get("request_id"))
    except (TypeError, ValueError):
        pass
    if seerr is not None and request_id is not None:
        try:
            req = await seerr.get_request(request_id)
            if req.requested_by_id is not None:
                mapping = await store.find_by_seerr_id(req.requested_by_id)
                return mapping, req.requested_by_id
        except Exception:
            logger.debug("requester resolution via request id failed; "
                         "falling back to username match", exc_info=True)
    if fallback_username:
        return await store.find_by_plex_username(fallback_username), None
    return None, None


async def handle_seerr_media(app: Application, payload: dict) -> None:
    """Process a request-lifecycle webhook (MEDIA_*) and DM the requester
    and/or the admin per _MEDIA_EVENTS."""
    nt = (payload.get("notification_type") or "").upper()
    lines_for = _MEDIA_EVENTS.get(nt)
    if lines_for is None:
        logger.info("Webhook media event %s: unknown type; dropping", nt)
        return
    requester_line, admin_line = lines_for
    if requester_line is None and admin_line is None:
        logger.info("Webhook media event %s: deliberately not notified", nt)
        return

    media = payload.get("media") or {}
    request = payload.get("request") or {}
    # Scan-triggered MEDIA_AVAILABLE events can carry a null request block;
    # the notifyuser_* fields then identify the target user (present when
    # the payload template includes them).
    requester_username = (request.get("requestedBy_username")
                          or payload.get("notifyuser_username") or "").strip()

    mapping, requester_seerr_id = await _resolve_requester(
        app, request, requester_username)
    if mapping is None and (requester_username or requester_seerr_id):
        logger.info("Webhook media event %s: requester '%s' not linked in Usher",
                    nt, requester_username or requester_seerr_id)

    seerr: Optional[SeerrClient] = app.bot_data.get("seerr")
    title_line = await format_media_title_line(seerr, media)
    seasons_note = _requested_seasons_note(payload)
    # Fall back to Seerr's own subject when the TMDb lookup fails, so the DM
    # never says just "Your request was approved" with no title at all.
    if not title_line:
        title_line = str(payload.get("subject") or "").strip()

    admin_id = app.bot_data.get("admin_id")
    requester_is_admin = mapping is not None and mapping.telegram_id == admin_id
    if not requester_is_admin and requester_seerr_id is not None and seerr is not None:
        # The admin can request WITHOUT a mapping (admin-key attribution);
        # those requests carry the API key's own Seerr user id. Without this
        # check the admin would be DMed "New request" about their own
        # requests.
        try:
            requester_is_admin = (requester_seerr_id
                                  == await seerr.get_admin_user_id())
        except Exception:
            logger.debug("admin-id comparison failed (non-fatal)", exc_info=True)

    # "The admin has been notified" is a lie when the requester IS the
    # admin (the admin FYI below is suppressed as self-noise); drop the
    # clause for them.
    if requester_is_admin and nt == "MEDIA_FAILED":
        requester_line = "⚠️ Your request failed to download."

    async def _dm(chat_id: int, first_line: str, *, with_requester: bool) -> None:
        parts = [first_line]
        if title_line:
            parts.append(html.escape(title_line))
        if seasons_note:
            parts.append(html.escape(seasons_note))
        if with_requester and requester_username:
            parts.append(f"<b>Requested by:</b> {html.escape(requester_username)}")
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text="\n".join(parts),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            logger.info("Notified telegram_id=%d of %s", chat_id, nt)
        except Exception:
            logger.exception("Failed to DM telegram_id=%d about %s", chat_id, nt)

    # The unlinked admin (admin-key attribution) has no mapping but is a
    # fully supported requester; their lifecycle DMs go to the admin chat.
    # Without this, MEDIA_FAILED on an admin request notified NOBODY (the
    # requester line had no mapping and the admin FYI is self-suppressed).
    requester_chat_id = None
    if mapping is not None:
        requester_chat_id = mapping.telegram_id
    elif requester_is_admin and admin_id:
        requester_chat_id = admin_id
    if requester_line and requester_chat_id:
        await _dm(requester_chat_id, requester_line, with_requester=False)

    # The admin line is skipped when the admin is the requester: their own
    # request coming back as "New request" is noise.
    if admin_line and admin_id and not requester_is_admin:
        await _dm(admin_id, admin_line, with_requester=True)

    # Morph any request cards tracking this media (approved -> grabbing,
    # terminal events paint the final line and close the watch).
    tmdb_for_watch = media.get("tmdbId")
    type_for_watch = media.get("media_type")
    if isinstance(tmdb_for_watch, int) and type_for_watch in ("movie", "tv"):
        from bot.request_watch import apply_webhook_event
        try:
            await apply_webhook_event(app, nt, type_for_watch, tmdb_for_watch)
        except Exception:
            logger.exception("watch morph failed for %s", nt)

    # Availability watches: one-shot fan-out to subscribers, minus whoever
    # this event already DMed as the requester.
    if nt == "MEDIA_AVAILABLE":
        tmdb_id = media.get("tmdbId")
        m_type = media.get("media_type")
        if isinstance(tmdb_id, int) and m_type in ("movie", "tv"):
            from bot.subscriptions import fan_out_availability
            already = {requester_chat_id} if requester_chat_id else set()
            await fan_out_availability(app, m_type, tmdb_id, title_line,
                                       already_notified=already)
