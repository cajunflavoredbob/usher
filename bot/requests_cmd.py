"""/requests: list the user's media requests (admin sees everyone's) with
status, plus cancel buttons for still-pending ones. Stateless against Seerr,
same as /tickets -- nothing request-shaped is stored locally."""
from __future__ import annotations

import asyncio
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from http_util import NotFoundAPIError, user_friendly_message
from seerr import (
    REQUEST_STATUS_APPROVED,
    REQUEST_STATUS_COMPLETED,
    REQUEST_STATUS_DECLINED,
    REQUEST_STATUS_FAILED,
    REQUEST_STATUS_PENDING,
    PlexTokenInvalidError,
    SeerrClient,
)
from store import UserStore

from bot.callback_prefixes import RQ_LIST_CANCEL
from bot.shared import (
    DECRYPT_FAILED_MSG,
    format_age,
    prompt_plex_relink,
    record_btn,
    require_seerr,
    send_typing,
    truncate_message,
)
from bot.request_flow import _summarize_seasons
from const import REQUEST_LIST_TAKE, TICKET_BUTTONS_PER_ROW

logger = logging.getLogger("usher")

_STATUS_LABELS = {
    REQUEST_STATUS_PENDING: ("🕓", "pending approval"),
    REQUEST_STATUS_APPROVED: ("⬇️", "approved, grabbing"),
    REQUEST_STATUS_DECLINED: ("🚫", "declined"),
    REQUEST_STATUS_FAILED: ("⚠️", "failed"),
    REQUEST_STATUS_COMPLETED: ("✅", "available"),
}


async def _render_requests(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                           reply_method, *, user_id: int,
                           notice: str = "") -> None:
    """Build and send/edit the requests list. Shared by /requests and the
    post-cancel re-render; `notice` (already-escaped or plain text) is
    prepended as a status line (cancel outcomes arrive this way because the
    callback was answered up front)."""
    seerr: SeerrClient = ctx.bot_data["seerr"]
    admin_id = ctx.bot_data.get("admin_id")
    is_admin = user_id == admin_id
    store: UserStore = ctx.bot_data["store"]
    mapping = await store.get(user_id)

    try:
        requests, total = await seerr.list_requests(
            take=REQUEST_LIST_TAKE,
            as_plex_token=None if is_admin else mapping.plex_token,
        )
    except PlexTokenInvalidError:
        await prompt_plex_relink(update, ctx)
        return
    except Exception as exc:
        logger.exception("list_requests failed")
        await reply_method(f"Couldn't fetch requests. {user_friendly_message(exc)}")
        return

    if not requests:
        empty = ("No requests across all users yet." if is_admin
                 else "You haven't requested anything yet. /request to start!")
        await reply_method(f"{notice}\n\n{empty}" if notice else empty)
        return

    title_tasks = [seerr.get_media_title(r.media_type, r.tmdb_id) for r in requests]
    title_results = await asyncio.gather(*title_tasks, return_exceptions=True)

    scope = "All requests" if is_admin else "Your requests"
    if total > len(requests):
        header = f"📥 {scope} (showing {len(requests)} of {total}; the rest are in Seerr):"
    else:
        header = f"📥 {scope} ({len(requests)}):"
    lines = ([notice, "", header, ""] if notice else [header, ""])
    cancelable: list[int] = []
    for req, tr in zip(requests, title_results):
        if isinstance(tr, Exception):
            media_label = f"TMDb {req.tmdb_id}"
        else:
            title, year = tr
            media_label = f"{title} ({year})" if year else title
        if req.seasons:
            media_label += f" — {_summarize_seasons(req.seasons)}"
        emoji, status_label = _STATUS_LABELS.get(req.status, ("❓", "unknown"))
        type_emoji = "🎬" if req.media_type == "movie" else "📺"
        line = f"#{req.id} {type_emoji} {html.escape(media_label)} — {emoji} {status_label}"
        age = format_age(req.created_at)
        if age:
            line += f" — {age}"
        if is_admin and req.requested_by != "?":
            line += f" — {html.escape(req.requested_by)}"
        lines.append(line)
        if req.status == REQUEST_STATUS_PENDING:
            cancelable.append(req.id)

    rows: list[list[InlineKeyboardButton]] = []
    if cancelable:
        lines.append("")
        lines.append("Cancel a pending request:")
        row: list[InlineKeyboardButton] = []
        for rid in cancelable:
            row.append(InlineKeyboardButton(
                f"❌ #{rid}", callback_data=f"{RQ_LIST_CANCEL}:{rid}"))
            if len(row) == TICKET_BUTTONS_PER_ROW:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

    sent = await reply_method(
        truncate_message("\n".join(lines)),
        reply_markup=InlineKeyboardMarkup(rows) if rows else None,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    if rows:
        record_btn(ctx.application, user_id, sent)


async def cmd_requests(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    seerr = await require_seerr(update, ctx)
    if seerr is None:
        return
    await send_typing(update, ctx)
    user_id = update.effective_user.id
    is_admin = user_id == ctx.bot_data.get("admin_id")
    store: UserStore = ctx.bot_data["store"]
    mapping = await store.get(user_id)
    if not is_admin:
        if mapping and mapping.plex_token_decrypt_failed:
            await update.effective_message.reply_text(DECRYPT_FAILED_MSG)
            return
        if not mapping or not mapping.plex_token:
            await update.effective_message.reply_text(
                "DM me /link first so I know which Plex account is yours."
            )
            return
    await _render_requests(update, ctx, update.effective_message.reply_text,
                           user_id=user_id)


async def rq_list_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel button on the /requests list. Seerr enforces ownership: a
    regular user may only delete their own request while it is PENDING, so
    a stale button (approved meanwhile, someone else's id) fails safely and
    the message says why."""
    q = update.callback_query
    try:
        request_id = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        await q.answer("Couldn't parse that button.", show_alert=True)
        return
    user_id = update.effective_user.id
    is_admin = user_id == ctx.bot_data.get("admin_id")
    store: UserStore = ctx.bot_data["store"]
    mapping = await store.get(user_id)
    token = None if is_admin else (mapping.plex_token if mapping else None)
    if not is_admin and not token:
        await q.answer("Your /link isn't usable anymore. /link again first.",
                       show_alert=True)
        return
    # Ack BEFORE any network work: DELETE's retry chain can outlive the
    # callback query's validity, and a late q.answer then raises out of the
    # handler -- turning a successful cancel into an error DM plus an
    # error-handler state wipe. All outcome feedback below rides message
    # edits instead.
    await q.answer()
    seerr: SeerrClient = ctx.bot_data["seerr"]
    try:
        await seerr.delete_request(request_id, as_plex_token=token)
    except NotFoundAPIError:
        # Already gone -- either cancelled elsewhere, or our DELETE landed
        # and a retry after a read timeout saw the 404. Both are success
        # from the user's point of view.
        pass
    except PlexTokenInvalidError:
        await prompt_plex_relink(update, ctx)
        return
    except Exception as exc:
        logger.exception("delete_request failed")
        await _render_requests(
            update, ctx, q.edit_message_text, user_id=user_id,
            notice=f"⚠️ Couldn't cancel #{request_id}. {user_friendly_message(exc)}")
        return
    # Re-render the list in place so the cancelled entry disappears.
    await _render_requests(update, ctx, q.edit_message_text, user_id=user_id,
                           notice=f"✅ Request #{request_id} cancelled.")
