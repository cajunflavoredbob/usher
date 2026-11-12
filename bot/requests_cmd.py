"""/requests: list the user's media requests (admin sees everyone's) with
status, plus cancel buttons for still-pending ones. Stateless against Seerr,
same as /tickets -- nothing request-shaped is stored locally. The Requests
Hub's "My requests" button shows the same list edited into the Hub message,
with a back row (requests_hub_mine)."""
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

from bot.callback_prefixes import REQUESTS_MINE, RQ_LIST_CANCEL
from bot.hub import back_button, came_from_hub, end_markup, reply_end
from bot.requests_hub import REQUESTS_HUB
from bot.shared import (
    DECRYPT_FAILED_MSG,
    edit_or_send,
    force_end_flow_conversations,
    format_age,
    prompt_plex_relink,
    record_btn,
    require_seerr,
    send_typing,
    truncate_message,
)
from bot.progress_card import _edit_card
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
                           notice: str = "", hub=None) -> None:
    """Build and send/edit the requests list. Shared by /requests, the Hub
    button and the post-cancel re-render; `notice` (already-escaped or plain
    text) is prepended as a status line (cancel outcomes arrive this way
    because the callback was answered up front). With `hub` set the list
    gets a last row holding the back button, and the error and empty texts
    carry the end keyboard (Try again on the error)."""
    seerr: SeerrClient = ctx.bot_data["seerr"]
    admin_id = ctx.bot_data.get("admin_id")
    is_admin = user_id == admin_id
    store: UserStore = ctx.bot_data["store"]
    link = await store.get(user_id)

    try:
        requests, total = await seerr.list_requests(
            take=REQUEST_LIST_TAKE,
            as_plex_token=None if is_admin else link.plex_token,
            requested_by=None if is_admin else link.seerr_id,
        )
    except PlexTokenInvalidError:
        await prompt_plex_relink(update, ctx,
                                 back=back_button(hub) if hub else None)
        return
    except Exception as exc:
        logger.exception("list_requests failed")
        await reply_end(
            ctx, reply_method,
            f"Couldn't fetch requests. {user_friendly_message(exc)}",
            hub, REQUESTS_MINE, user_id=user_id)
        return

    if not requests:
        empty = ("No requests across all users yet." if is_admin
                 else "You haven't requested anything yet. /request to start!")
        empty_text = f"{notice}\n\n{empty}" if notice else empty
        await reply_end(ctx, reply_method, empty_text, hub, user_id=user_id)
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
            media_label += f" - {_summarize_seasons(req.seasons)}"
        emoji, status_label = _STATUS_LABELS.get(req.status, ("❓", "unknown"))
        type_emoji = "🎬" if req.media_type == "movie" else "📺"
        line = f"#{req.id} {type_emoji} {html.escape(media_label)} - {emoji} {status_label}"
        age = format_age(req.created_at)
        if age:
            line += f" - {age}"
        if is_admin and req.requested_by != "?":
            line += f" - {html.escape(req.requested_by)}"
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
    if hub:
        rows.append([back_button(hub)])

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
    link = await store.get(user_id)
    if not is_admin:
        if link and link.plex_token_decrypt_failed:
            await update.effective_message.reply_text(DECRYPT_FAILED_MSG)
            return
        if not link or not link.plex_token:
            await update.effective_message.reply_text(
                "DM me /link first so I know which Plex account is yours."
            )
            return
    await _render_requests(update, ctx, update.effective_message.reply_text,
                           user_id=user_id)


async def requests_hub_mine(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Requests Hub "My requests" button: the /requests list, edited into the
    tapped Hub message."""
    q = update.callback_query
    await q.answer()
    force_end_flow_conversations(ctx, update)
    back_only = end_markup(REQUESTS_HUB)
    seerr = await require_seerr(update, ctx, reply_markup=back_only)
    if seerr is None:
        return
    await send_typing(update, ctx)
    user_id = update.effective_user.id
    is_admin = user_id == ctx.bot_data.get("admin_id")
    store: UserStore = ctx.bot_data["store"]
    link = await store.get(user_id)
    guard = None
    if not is_admin:
        if link and link.plex_token_decrypt_failed:
            guard = DECRYPT_FAILED_MSG
        elif not link or not link.plex_token:
            guard = "DM me /link first so I know which Plex account is yours."
    if guard is not None:
        sent = await edit_or_send(q, guard, reply_markup=back_only)
        record_btn(ctx.application, user_id, sent)
        return

    async def edit_into_hub(text, **kwargs):
        return await edit_or_send(q, text, **kwargs)

    await _render_requests(update, ctx, edit_into_hub, user_id=user_id,
                           hub=REQUESTS_HUB)


CANCELLED_CARD_LINE = "🚫 Request cancelled."


async def _close_progress_cards(ctx, info, *, fallback_user_id,
                                tapper_seerr_id=None) -> None:
    """Paint a final line on the requester's Progress card(s) for a Request
    that was just cancelled, and drop the rows. Best effort: the cancel has
    already succeeded, so nothing here may fail the handler. Only cards of
    the Request's requester are touched; another user's Progress card for
    the same title is a Request of their own."""
    if info is None:
        return
    store: UserStore = ctx.bot_data["store"]
    try:
        requester_tg = None
        if info.requested_by_id is None:
            requester_tg = fallback_user_id
        elif tapper_seerr_id == info.requested_by_id:
            # Several Telegram accounts can share one Seerr account; the
            # tapper's own card is the one this cancel was about.
            requester_tg = fallback_user_id
        else:
            owner = await store.find_by_seerr_id(info.requested_by_id)
            if owner is not None:
                requester_tg = owner.telegram_id
        if requester_tg is None:
            return
        cards = await store.find_progress_cards(info.media_type, info.tmdb_id)
        # Only a pending Request can be cancelled, and its card is still
        # "waiting". Cards carry no Request id, only the 4K flag and, for
        # TV, the seasons the Request was made with, so a card counts only
        # when its 4K flag matches the cancelled Request's and, for TV, its
        # stored seasons equal the Request's. A card with no stored seasons
        # (made before they were recorded) never matches. When several
        # still match we cannot tell which one this was: leave them all
        # rather than close a live Request's card.
        is_tv = info.media_type != "movie"
        request_seasons = sorted(info.seasons)
        mine = [c for c in cards
                if c["user_id"] == requester_tg and c["status"] == "waiting"
                and bool(c["is4k"]) == info.is4k
                and (not is_tv or (c.get("seasons") and
                                   sorted(c["seasons"]) == request_seasons))]
        if len(mine) != 1:
            return
        card = mine[0]
        # Delete first so a mid-tick poller cannot paint over the line.
        await store.delete_progress_card(card["id"])
        await _edit_card(ctx, card, CANCELLED_CARD_LINE, footer=False)
    except Exception:
        logger.exception("closing Progress card after cancel failed")


async def rq_list_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel button on the /requests list. Seerr enforces ownership: a
    regular user may only delete their own request while it is PENDING, so
    a stale button (approved meanwhile, someone else's id) fails safely and
    the message says why."""
    q = update.callback_query
    # Keep the back row when the tapped list carried it (opened from the Hub).
    hub = REQUESTS_HUB if came_from_hub(q.message, REQUESTS_HUB) else None
    try:
        request_id = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        await q.answer("Couldn't parse that button.", show_alert=True)
        return
    user_id = update.effective_user.id
    is_admin = user_id == ctx.bot_data.get("admin_id")
    store: UserStore = ctx.bot_data["store"]
    link = await store.get(user_id)
    token = None if is_admin else (link.plex_token if link else None)
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
    # Read the Request first: once deleted it 404s, and its media and
    # requester are how we find the Progress card to close.
    try:
        info = await seerr.get_request(request_id, as_plex_token=token)
    except Exception:
        logger.debug("pre-cancel lookup of #%s failed (non-fatal)",
                     request_id, exc_info=True)
        info = None
    try:
        await seerr.delete_request(request_id, as_plex_token=token)
    except NotFoundAPIError:
        # Already gone -- either cancelled elsewhere, or our DELETE landed
        # and a retry after a read timeout saw the 404. Both are success
        # from the user's point of view.
        pass
    except PlexTokenInvalidError:
        await prompt_plex_relink(update, ctx,
                                 back=back_button(hub) if hub else None)
        return
    except Exception as exc:
        logger.exception("delete_request failed")
        await _render_requests(
            update, ctx, q.edit_message_text, user_id=user_id,
            notice=f"⚠️ Couldn't cancel #{request_id}. {user_friendly_message(exc)}",
            hub=hub)
        return
    await _close_progress_cards(
        ctx, info, fallback_user_id=None if is_admin else user_id,
        tapper_seerr_id=None if is_admin or not link else link.seerr_id)
    # Re-render the list in place so the cancelled entry disappears.
    await _render_requests(update, ctx, q.edit_message_text, user_id=user_id,
                           notice=f"✅ Request #{request_id} cancelled.",
                           hub=hub)
