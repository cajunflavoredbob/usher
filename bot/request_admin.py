"""Action buttons on the Admin's Request DMs (the MEDIA_* webhook DMs).

A pending-Request DM carries Approve and Decline, a failed-Request DM
carries Retry, and both carry an "Open in Seerr" link when the Seerr public
URL is set. Auto-approved DMs get no buttons. A tap first reads the
Request's status in Seerr: when it no longer matches the button (approved
or declined meanwhile, no longer failed), Seerr is left alone and the DM
says what the Request is now. After an action, or a stale tap, the DM keeps
only the Open in Seerr link. When Seerr refuses or is down, the DM keeps its
buttons and gains one line saying why.
"""
from __future__ import annotations

import html
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from http_util import NotFoundAPIError, user_friendly_message
from seerr import (
    REQUEST_STATUS_APPROVED,
    REQUEST_STATUS_COMPLETED,
    REQUEST_STATUS_DECLINED,
    REQUEST_STATUS_FAILED,
    REQUEST_STATUS_PENDING,
    SeerrClient,
)

from bot.callback_prefixes import RQ_ADMIN_APPROVE, RQ_ADMIN_DECLINE, RQ_ADMIN_RETRY
from bot.shared import coerce_tmdb_id, end_action, try_begin_action
from bot.tickets import _require_admin

logger = logging.getLogger("usher")

OPEN_IN_SEERR_LABEL = "Open in Seerr"
# Start of every failure line; _base_text strips one before adding the next.
_FAILURE_MARK = "⚠️ Couldn't "

# prefix -> (status the Request must have, Seerr client method, outcome line,
#            verb for the failure line)
_ACTIONS = {
    RQ_ADMIN_APPROVE: (REQUEST_STATUS_PENDING, "approve_request",
                       "✅ Approved", "approve"),
    RQ_ADMIN_DECLINE: (REQUEST_STATUS_PENDING, "decline_request",
                       "🚫 Declined", "decline"),
    RQ_ADMIN_RETRY: (REQUEST_STATUS_FAILED, "retry_request",
                     "🔁 Retrying", "retry"),
}

_STATUS_NOTES = {
    REQUEST_STATUS_PENDING: "ℹ️ Pending approval in Seerr",
    REQUEST_STATUS_APPROVED: "ℹ️ Already approved in Seerr",
    REQUEST_STATUS_DECLINED: "ℹ️ Already declined in Seerr",
    REQUEST_STATUS_FAILED: "ℹ️ Already failed in Seerr",
    REQUEST_STATUS_COMPLETED: "ℹ️ Already completed in Seerr",
}


def seerr_link(public_url: str, media: dict) -> Optional[str]:
    """The Seerr page for the Request's media, or None without a public URL.
    Seerr has no page per Request; the media page lists its Requests with
    their own approve and decline controls. Without a usable TMDb id the
    link falls back to Seerr's Requests list."""
    base = (public_url or "").strip().rstrip("/")
    if not base:
        return None
    tmdb_id = coerce_tmdb_id(media.get("tmdbId"))
    media_type = media.get("media_type")
    if tmdb_id is not None and media_type in ("movie", "tv"):
        return f"{base}/{media_type}/{tmdb_id}"
    return f"{base}/requests"


def admin_request_kb(notification_type: str, request_id: Optional[int],
                     link_url: Optional[str]) -> Optional[InlineKeyboardMarkup]:
    """Buttons for the Admin's DM about a Request event, or None.

    MEDIA_PENDING gets Approve and Decline, MEDIA_FAILED gets Retry, each
    only with a request id. Both get Open in Seerr when link_url is set.
    Every other event (auto-approved included) gets no buttons."""
    if notification_type not in ("MEDIA_PENDING", "MEDIA_FAILED"):
        return None
    rows = []
    if request_id is not None:
        if notification_type == "MEDIA_PENDING":
            rows.append([
                InlineKeyboardButton(
                    "✅ Approve", callback_data=f"{RQ_ADMIN_APPROVE}:{request_id}"),
                InlineKeyboardButton(
                    "🚫 Decline", callback_data=f"{RQ_ADMIN_DECLINE}:{request_id}"),
            ])
        else:
            rows.append([InlineKeyboardButton(
                "🔁 Retry", callback_data=f"{RQ_ADMIN_RETRY}:{request_id}")])
    if link_url:
        rows.append([InlineKeyboardButton(OPEN_IN_SEERR_LABEL, url=link_url)])
    return InlineKeyboardMarkup(rows) if rows else None


def _link_only(markup) -> Optional[InlineKeyboardMarkup]:
    """The tapped DM's URL buttons (Open in Seerr) without its action
    buttons, or None when it has none."""
    rows = []
    for row in getattr(markup, "inline_keyboard", None) or ():
        kept = [b for b in row if getattr(b, "url", None)]
        if kept:
            rows.append(kept)
    return InlineKeyboardMarkup(rows) if rows else None


def _base_text(message) -> str:
    """The DM's text, as HTML, without an earlier failure line this module
    added. Only a failure keeps the action buttons, so a failure line is the
    only note a later tap can find; it is the last paragraph and starts with
    _FAILURE_MARK. The rest is kept as sent, blank lines included."""
    text = getattr(message, "text_html", None)
    if not text:
        text = html.escape(getattr(message, "text", "") or "")
    head, sep, last = text.rpartition("\n\n")
    if sep and last.startswith(_FAILURE_MARK):
        return head
    return text


async def _edit_dm(q, text: str, markup) -> None:
    try:
        await q.edit_message_text(text, parse_mode="HTML",
                                  disable_web_page_preview=True,
                                  reply_markup=markup)
    except Exception as exc:
        logger.exception("Couldn't edit the Admin's Request DM", exc_info=exc)


async def rq_admin_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Approve, Decline or Retry tapped on the Admin's Request DM."""
    q = update.callback_query
    try:
        prefix, raw_id = q.data.split(":", 1)
        request_id = int(raw_id)
        required_status, method, outcome, verb = _ACTIONS[prefix]
    except (ValueError, KeyError):
        await q.answer("Couldn't parse that button.", show_alert=True)
        return
    if not await _require_admin(q, ctx, action_label=f"request_{verb}"):
        return
    key = f"rqadmin:{request_id}"
    if not try_begin_action(ctx, key):
        await q.answer("Already working on that request.")
        return
    try:
        # Ack before any network work (see rq_list_cancel): a late answer
        # raises once the query expires. Outcomes ride the message edit.
        await q.answer()
        base = _base_text(q.message)
        markup = getattr(q.message, "reply_markup", None)
        seerr: Optional[SeerrClient] = ctx.bot_data.get("seerr")
        async def fail(reason: str) -> None:
            # The buttons stay so the Admin can tap again.
            await _edit_dm(q, f"{base}\n\n{_FAILURE_MARK}{verb}. "
                              f"{html.escape(reason)}", markup)

        if seerr is None:
            await fail("Seerr isn't set up.")
            return
        try:
            current = await seerr.get_request(request_id)
        except NotFoundAPIError:
            await _edit_dm(q, f"{base}\n\nℹ️ This request no longer exists in Seerr",
                           _link_only(markup))
            return
        except Exception as exc:
            logger.exception("get_request #%d failed before %s", request_id, verb)
            await fail(user_friendly_message(exc))
            return
        if current.status != required_status:
            note = _STATUS_NOTES.get(current.status,
                                     "ℹ️ This request changed in Seerr")
            await _edit_dm(q, f"{base}\n\n{note}", _link_only(markup))
            return
        try:
            await getattr(seerr, method)(request_id)
        except Exception as exc:
            logger.exception("%s #%d failed", method, request_id)
            await fail(user_friendly_message(exc))
            return
        logger.info("Admin %s request #%d from the DM", verb, request_id)
        await _edit_dm(q, f"{base}\n\n{outcome}", _link_only(markup))
    finally:
        end_action(ctx, key)
