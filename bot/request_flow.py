"""/request conversation: search, pick media, pick seasons (TV), confirm,
submit to Seerr as the linked user. Bare /request opens the Requests Hub; the
Hub's "New request" button (request_hub_new) and a pick from a Hub-opened list
(request_from_ticket) start the conversation with the Hub as its origin, so its
ends carry a back button (and Try again on failures). A typed /request <title>
keeps its old error and cancel texts with no buttons; a successful submit ends
with the back button and a Progress card, the same as from the Hub.

Seerr is the sole authority on who may request and how much: the bot adds no
allowlist or quota of its own. Requests ride the per-user session client, so
Seerr evaluates the real user's REQUEST permission, quota, and auto-approve
bits and the outcome message reflects what actually happened (pending vs
approved immediately).

Search fetches one Seerr page (up to 20 results) and renders screens of 5
with pure re-render paging. Each result offers an optional detail card
(poster + overview) so users unsure by name alone can check before picking;
cards are deliberately NOT recorded in the button-gate history (three of
them would evict the live pick list), and their Dismiss is gate-exempt.

Every flow-scoped callback (Cancel included) carries the search version,
and the version counter survives _clear_rq_state, so buttons on a
superseded message from an earlier /request toast instead of acting on (or
ending) the current flow.

4K is offered on the confirm screen only to users whose Seerr permissions
allow it; a standard-covered title stays requestable in 4K for those users
instead of dead-ending. Known limitation: the TV season picker marks
availability on the standard track only, so re-requesting a standard-
available season in 4K needs the Seerr UI for now."""
from __future__ import annotations

import html
import logging
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from http_util import user_friendly_message
from seerr import (
    MEDIA_STATUS_AVAILABLE,
    MEDIA_STATUS_BLOCKLISTED,
    MEDIA_STATUS_PARTIALLY_AVAILABLE,
    MEDIA_STATUS_PENDING,
    MEDIA_STATUS_PROCESSING,
    REQUEST_STATUS_APPROVED,
    DuplicateRequestError,
    MediaResult,
    NothingToRequestError,
    PlexTokenInvalidError,
    QuotaExceededError,
    RequestPermissionError,
    SeerrClient,
    can_request_4k,
)
from store import UserStore

from bot.callback_prefixes import (
    RQ_BEHALF_PAGE,
    RQ_BEHALF_PICK,
    RQ_CANCEL,
    RQ_FROM_ISSUE_MARK,
    RQ_FROM_TICKET,
    RQ_GO,
    RQ_GO_4K,
    RQ_INFO,
    RQ_INFO_DISMISS,
    RQ_MEDIA,
    RQ_PAGE,
    RQ_REMAINING_GO,
    RQ_SEASON,
    RQ_SEASON_ALL,
    RQ_SEASON_DONE,
    RQ_SEASON_NA,
    REQUESTS_BEHALF,
    REQUESTS_NEW,
)
from bot.hub import (
    back_button,
    came_from_hub,
    end_flow,
    end_markup,
    hub_origin,
    send_hub,
    set_hub_origin,
)
from bot.media_card import send_detail_card
from bot.progress_card import APPROVED_LINE, card_text
from bot.remaining import requestable_seasons
from bot.requests_hub import REQUESTS_HUB
from bot.shared import (
    DECRYPT_FAILED_MSG,
    KEYCAP_DIGITS,
    admin_view,
    day_count,
    RQ_CONFIRM,
    RQ_PICK_MEDIA,
    RQ_PICK_USER,
    RQ_PICK_SEASONS,
    RQ_TITLE,
    RELINK_RESUME_EXECUTORS,
    RQ_KEYS,
    edit_or_send,
    flow_step_gen,
    flow_step_is_stale,
    force_end_flow_conversations,
    prompt_plex_relink,
    record_btn,
    require_seerr,
    send_typing,
    token_for,
    user_in_conversation,
)
from const import (
    KB_BUTTONS_PER_ROW,
    PROGRESS_CARD_TIMEOUT_HOURS,
    REQUEST_FLOW_TIMEOUT_S,
    REQUEST_RESULTS_PER_PAGE,
    REQUEST_SEARCH_FETCH_LIMIT,
    SEASON_BUTTONS_PER_ROW,
)

logger = logging.getLogger("usher")


# user_data keys this flow owns (cleared on timeout/cancel/finish).
# rq_search_version is deliberately NOT here: it must survive across flows
# so a superseded message's version-stamped buttons can never collide with a
# fresh flow's counter restarting at 1.
_RQ_KEYS = RQ_KEYS

# The confirm screen's snapshot of whom it names (see _show_confirm). One of
# RQ_KEYS, so ending the flow clears it; entry points never pop it themselves.
_CONFIRM_KEY = "rq_confirm_for"
# Starting anything else ends the user's open flows in every chat, so a flow
# that finds its pick gone was ended behind its back: say that, not that the
# person was "cleared".
_TARGET_CLEARED_TEXT = ("That Request for someone was ended when you started "
                        "something else. Start Request for someone again.")
_TARGET_CLEARED_TYPED = ("That Request for someone was ended when you started "
                         "something else. /request, then Request for someone "
                         "again.")

# Request for someone is DM-only (admin_view), so its pick (rq_for) belongs to
# the private chat: only an entry point in a private chat pops it, and only a
# flow in a private chat reads it. _PICKED_KEY marks that the DM flow was
# started as Request for someone. It is NOT one of RQ_KEYS: should anything
# clear RQ_KEYS (rq_for among them, user_data is per user) while the DM flow
# runs on, the marker is how the DM flow then knows its person is gone. A flow
# started in another chat no longer leaves it running (force-ending ends the
# user's flows in every chat), so this is a backstop. Only the private-chat
# entry points pop it.
_PICKED_KEY = "rq_for_picked"

# Availability annotations for the search list. UNKNOWN/DELETED get none:
# they're plainly requestable.
_STATUS_NOTES = {
    MEDIA_STATUS_PENDING: "⏳ requested",
    MEDIA_STATUS_PROCESSING: "⏳ downloading",
    MEDIA_STATUS_PARTIALLY_AVAILABLE: "🌗 partly available",
    MEDIA_STATUS_AVAILABLE: "✅ available",
    MEDIA_STATUS_BLOCKLISTED: "🚫 not requestable",
}

# Standard-track statuses that mean "a plain request would bounce".
_COVERED_STATUSES = (MEDIA_STATUS_PENDING, MEDIA_STATUS_PROCESSING,
                     MEDIA_STATUS_AVAILABLE)

_STALE_MENU_TOAST = "That menu is from an older search: use the newest one."

def _current_version(ctx: ContextTypes.DEFAULT_TYPE) -> int:
    return ctx.user_data.get("rq_search_version") or 0


async def _request_timeout(update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    hub = hub_origin(ctx)
    _clear_rq_state(ctx)
    try:
        if update is not None and update.effective_chat is not None:
            if hub is None:
                await ctx.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⏱️ The /request flow timed out. Start again with /request.",
                )
            else:
                sent = await ctx.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⏱️ The request timed out.",
                    reply_markup=end_markup(hub, REQUESTS_NEW),
                )
                record_btn(ctx.application, update.effective_user.id, sent)
    except Exception as exc:
        logger.debug("request-timeout notice failed (non-fatal)", exc_info=exc)
    return ConversationHandler.END


async def _request_expect_text_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "I can only read text here: type the title, or /cancel to stop.")
    return RQ_TITLE


def _request_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("request", request_start),
            # Requests Hub "New request" button.
            CallbackQueryHandler(request_hub_new, pattern=fr"^{REQUESTS_NEW}$"),
            # Requests Hub "Request for someone" (Admin only).
            CallbackQueryHandler(request_hub_behalf,
                                 pattern=fr"^{REQUESTS_BEHALF}$"),
            # Jump-in from the /issue not-in-library dead-end.
            CallbackQueryHandler(request_from_ticket, pattern=fr"^{RQ_FROM_TICKET}:"),
        ],
        states={
            RQ_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, request_title),
                MessageHandler(~filters.COMMAND, _request_expect_text_title),
            ],
            RQ_PICK_USER: [
                CallbackQueryHandler(request_behalf_page,
                                     pattern=fr"^{RQ_BEHALF_PAGE}:"),
                CallbackQueryHandler(request_behalf_pick,
                                     pattern=fr"^{RQ_BEHALF_PICK}:"),
            ],
            RQ_PICK_MEDIA: [
                CallbackQueryHandler(request_pick_media, pattern=fr"^{RQ_MEDIA}:"),
                CallbackQueryHandler(request_info, pattern=fr"^{RQ_INFO}:"),
                CallbackQueryHandler(request_page, pattern=fr"^{RQ_PAGE}:"),
            ],
            RQ_PICK_SEASONS: [
                CallbackQueryHandler(request_toggle_season, pattern=fr"^{RQ_SEASON}:"),
                CallbackQueryHandler(request_season_na, pattern=fr"^{RQ_SEASON_NA}:"),
                CallbackQueryHandler(request_select_all, pattern=fr"^{RQ_SEASON_ALL}:"),
                CallbackQueryHandler(request_seasons_done, pattern=fr"^{RQ_SEASON_DONE}:"),
            ],
            RQ_CONFIRM: [CallbackQueryHandler(
                request_submit, pattern=fr"^({RQ_GO}|{RQ_GO_4K}):")],
            # TypeHandler, not MessageHandler: message filters reject
            # callback-query updates and the timeout would never fire for a
            # flow abandoned at a button step (same as /issue).
            ConversationHandler.TIMEOUT: [TypeHandler(Update, _request_timeout)],
        },
        fallbacks=[
            CommandHandler("cancel", request_cancel),
            # Version-suffixed (rqcancel:<v>); bare ^rqcancel matches both.
            CallbackQueryHandler(request_cancel, pattern=fr"^{RQ_CANCEL}"),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
        name="request",
        persistent=False,
        conversation_timeout=REQUEST_FLOW_TIMEOUT_S,
    )


async def _gate_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *,
                      hub: Optional[str] = None) -> bool:
    """Shared /request entry gate: Seerr configured + a usable link. The
    admin passes without a link (admin-key attribution, like /issue's
    admin paths). On success, the user's Seerr permission bitmask is cached
    in user_data so pick/confirm can decide whether to offer 4K without a
    round-trip per screen. With `hub` set (entry from a Hub button) the link
    guards edit the tapped message and the Seerr-not-set-up guard replies with
    a new message; both carry the back button."""
    if await require_seerr(
            update, ctx,
            reply_markup=end_markup(hub) if hub else None) is None:
        return False
    is_admin, token, decrypt_failed = await token_for(ctx, update.effective_user.id)
    if not is_admin:
        if decrypt_failed:
            await end_flow(update, ctx, DECRYPT_FAILED_MSG, hub, edit=hub is not None)
            return False
        if not token:
            await end_flow(
                update, ctx,
                "DM me /link first so requests are filed as you. It's a quick "
                "Plex sign-in - no username needed.",
                hub, edit=hub is not None)
            return False
    seerr: SeerrClient = ctx.bot_data["seerr"]
    try:
        ctx.user_data["rq_perms"] = await seerr.get_my_permissions(
            as_plex_token=token)
    except Exception:
        # No permissions no 4K offer; the request path itself is unaffected.
        logger.debug("permission fetch failed (non-fatal)", exc_info=True)
        ctx.user_data["rq_perms"] = 0
    return True


def _in_private_chat(update: Update) -> bool:
    return getattr(update.effective_chat, "type", None) == "private"


def _forget_behalf_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """A new request started in the private chat forgets a Request for
    someone pick. One started in another chat does not pop it here: starting
    it force-ends the user's open DM request, which clears the pick."""
    if _in_private_chat(update):
        ctx.user_data.pop("rq_for", None)
        ctx.user_data.pop(_PICKED_KEY, None)


async def request_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    # A typed /request is never a Hub flow, even if a Hub left its mark behind.
    set_hub_origin(ctx, None)
    _forget_behalf_pick(update, ctx)
    # Bare `/request` opens the Requests Hub; it needs no gate (each button
    # runs the same checks as its typed command).
    query = " ".join(ctx.args or ()).strip()
    if not query:
        await send_hub(update, ctx, REQUESTS_HUB)
        return ConversationHandler.END
    # Typing indicator BEFORE the gate: its permission fetch can ride a cold
    # per-user auth (retry chain 60s+ on a degraded Seerr) and the bot
    # otherwise looks frozen before its first reply.
    await send_typing(update, ctx)
    if not await _gate_entry(update, ctx):
        return ConversationHandler.END
    # `/request dune part two` skips the title prompt.
    return await _run_search(
        update, ctx, update.effective_message.reply_text, query,
        user_id=update.effective_user.id, hub=None,
    )


async def request_hub_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Requests Hub "New request" button: the title prompt, edited into the
    tapped Hub message."""
    q = update.callback_query
    await q.answer()
    force_end_flow_conversations(ctx, update)
    set_hub_origin(ctx, REQUESTS_HUB)
    _forget_behalf_pick(update, ctx)
    await send_typing(update, ctx)
    if not await _gate_entry(update, ctx, hub=REQUESTS_HUB):
        return ConversationHandler.END
    await edit_or_send(q, "What movie or show do you want? (Reply with the title.)")
    return RQ_TITLE


# Request for someone (Admin): pick a Linked user, then the normal flow

BEHALF_PER_PAGE = 8


def _behalf_screen(users: list, page: int, version: int):
    """(text, markup) for one page of the Linked-user list."""
    pages = max(1, -(-len(users) // BEHALF_PER_PAGE))
    page = min(max(page, 0), pages - 1)
    chunk = users[page * BEHALF_PER_PAGE:(page + 1) * BEHALF_PER_PAGE]
    rows = [[InlineKeyboardButton(
        name or f"User {seerr_id}",
        callback_data=f"{RQ_BEHALF_PICK}:{seerr_id}")]
        for _tg, seerr_id, name in chunk]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "◀️ Prev", callback_data=f"{RQ_BEHALF_PAGE}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(
            "Next ▶️", callback_data=f"{RQ_BEHALF_PAGE}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(
        "🛑 Cancel", callback_data=f"{RQ_CANCEL}:{version}")])
    text = "👥 <b>Request for someone</b>\nWho is it for?"
    if pages > 1:
        text += f" (page {page + 1} of {pages})"
    return text, InlineKeyboardMarkup(rows)


async def request_hub_behalf(update: Update,
                             ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Requests Hub "Request for someone": the Admin's list of Linked users."""
    q = update.callback_query
    if not admin_view(update, ctx):
        # The button is only drawn for the Admin in a DM; a forged tap, or a
        # tap in a group, gets nothing.
        await q.answer("Only the Admin can do that.", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    force_end_flow_conversations(ctx, update)
    set_hub_origin(ctx, REQUESTS_HUB)
    _forget_behalf_pick(update, ctx)
    await send_typing(update, ctx)
    if not await _gate_entry(update, ctx, hub=REQUESTS_HUB):
        return ConversationHandler.END
    store: UserStore = ctx.bot_data["store"]
    users = await store.list_linked_users()
    if not users:
        await end_flow(update, ctx, "No one has linked their Plex account yet.",
                       REQUESTS_HUB)
        return ConversationHandler.END
    # A fresh version so buttons of an older flow's messages go stale.
    version = _current_version(ctx) + 1
    ctx.user_data["rq_search_version"] = version
    text, markup = _behalf_screen(users, 0, version)
    sent = await edit_or_send(q, text, reply_markup=markup, parse_mode="HTML")
    record_btn(ctx.application, update.effective_user.id, sent)
    return RQ_PICK_USER


async def request_behalf_page(update: Update,
                              ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    try:
        page = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        return RQ_PICK_USER
    store: UserStore = ctx.bot_data["store"]
    users = await store.list_linked_users()
    text, markup = _behalf_screen(users, page, _current_version(ctx))
    sent = await edit_or_send(q, text, reply_markup=markup, parse_mode="HTML")
    record_btn(ctx.application, update.effective_user.id, sent)
    return RQ_PICK_USER


async def request_behalf_pick(update: Update,
                              ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    hub = hub_origin(ctx)
    try:
        seerr_id = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        return RQ_PICK_USER
    store: UserStore = ctx.bot_data["store"]
    picked = next((u for u in await store.list_linked_users()
                   if u[1] == seerr_id), None)
    if picked is None:
        await end_flow(update, ctx, "That user isn't linked any more.", hub,
                       REQUESTS_BEHALF)
        _clear_rq_state(ctx)
        return ConversationHandler.END
    name = picked[2] or f"User {seerr_id}"
    ctx.user_data["rq_for"] = {"seerr_id": seerr_id, "name": name,
                               "telegram_id": picked[0]}
    ctx.user_data[_PICKED_KEY] = True
    sent = await edit_or_send(
        q, f"Requesting for <b>{html.escape(name)}</b>. What movie or show "
           "do you want? (Reply with the title.)", parse_mode="HTML")
    record_btn(ctx.application, update.effective_user.id, sent)
    return RQ_TITLE


def _behalf_target(update: Update,
                   ctx: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    """The Linked user the Admin is requesting for, or None. Only the Admin
    can have one: nobody else can reach the picker. A flow outside the
    private chat never has one: the pick belongs to the DM flow."""
    if not _in_private_chat(update):
        return None
    return ctx.user_data.get("rq_for")


def _behalf_pick_lost(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """True when the private chat's flow was started as Request for someone
    and its person has since been cleared while it ran on. Such a flow must
    not go on as the Admin's own Request."""
    return (_in_private_chat(update) and bool(ctx.user_data.get(_PICKED_KEY))
            and ctx.user_data.get("rq_for") is None)


async def _refuse_lost_pick(update: Update,
                            ctx: ContextTypes.DEFAULT_TYPE) -> int:
    # The marker stays: the next request in the private chat starts at an
    # entry point, which pops it.
    await end_flow(update, ctx, _TARGET_CLEARED_TEXT, hub_origin(ctx),
                   REQUESTS_BEHALF, typed_text=_TARGET_CLEARED_TYPED)
    return ConversationHandler.END


def _coverage_verdict(ctx: ContextTypes.DEFAULT_TYPE, media_type: str,
                      selected: MediaResult) -> tuple[Optional[str], bool]:
    """Availability checks shared by a search pick and a /trending tap.
    Returns (toast, force_4k): a toast means "stop here, keep the list";
    force_4k means offer the 4K-only confirm for a movie whose standard
    track is already covered."""
    status = selected.status or 0
    # Blocklisted media isn't requestable in either tier or type; preempt
    # before the user invests in the season picker just to be told no.
    if status == MEDIA_STATUS_BLOCKLISTED:
        return "That title isn't requestable here 🚫", False
    if media_type == "movie" and status in _COVERED_STATUSES:
        # Standard track already covered. A 4K-permitted user can still
        # want the 4K copy, so pass through to a 4K-only confirm unless
        # the 4K track is covered too.
        perms = ctx.user_data.get("rq_perms", 0)
        covered_4k = (selected.status_4k or 0) in _COVERED_STATUSES
        if can_request_4k(perms, "movie") and not covered_4k:
            return None, True
        if status == MEDIA_STATUS_AVAILABLE:
            msg = ("Already available in Plex ✅ (including 4K)"
                   if covered_4k and can_request_4k(perms, "movie")
                   else "Already available in Plex ✅")
            return msg, False
        return "Already requested. It's on its way ⏳", False
    return None, False


def _split_jump(data: Optional[str]) -> tuple[str, int, bool]:
    """(media_type, tmdb_id, from_issue) of an rqfi tap. The /issue dead-end
    button carries a trailing ":i"; /trending's buttons, and /issue buttons
    sent before the marker existed, do not. Raises ValueError on a bad tap."""
    parts = (data or "").split(":")
    if len(parts) == 4 and parts[3] == RQ_FROM_ISSUE_MARK:
        parts = parts[:3]
        from_issue = True
    else:
        from_issue = False
    _, media_type, tmdb_id_s = parts
    return media_type, int(tmdb_id_s), from_issue


def _trending_result(ctx: ContextTypes.DEFAULT_TYPE, data: Optional[str],
                     stash: dict) -> Optional[MediaResult]:
    """The /trending list entry a rqfi tap points at, or None (no list, a
    different entry, or the /issue jump, whose `stash` holds its own title)."""
    try:
        media_type, tmdb_id, _ = _split_jump(data)
    except ValueError:
        return None
    if stash.get("tmdb_id") == tmdb_id:
        return None
    return next((r for r in ctx.user_data.get("dv_results") or []
                 if r.media_type == media_type and r.tmdb_id == tmdb_id), None)


def _in_latest_browse(ctx: ContextTypes.DEFAULT_TYPE,
                      data: Optional[str]) -> bool:
    """True if the tapped title is in the latest /trending list (an
    unparseable tap counts as in: the parse error path owns it)."""
    try:
        media_type, tmdb_id, _ = _split_jump(data)
    except ValueError:
        return True
    return any(r.media_type == media_type and r.tmdb_id == tmdb_id
               for r in ctx.user_data.get("dv_results") or [])


async def request_from_ticket(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry via the /issue dead-end button. The tapped media is already
    known, so this drops straight into the season picker / confirm."""
    q = update.callback_query
    # Ack first: the gate below can run a slow Seerr login, and a late
    # answer to an expired tap raises out of the handler.
    await q.answer()
    # Take the /issue jump's stash now, before anything can end or refuse the
    # tap: a stash left behind would make a later /trending tap on the same
    # title skip the availability check. (The force-end below then has no
    # stash to keep.)
    stash = ctx.user_data.pop("rq_jump_media", None) or {}
    try:
        from_issue = _split_jump(q.data)[2]
    except ValueError:
        from_issue = False
    # A /trending number tap carries no title, but the latest browse list is
    # still in user_data with each title's status. The /issue jump stashes
    # its own title and has no status to check.
    tapped = _trending_result(ctx, q.data, stash)
    if (tapped is None and not stash
            and user_in_conversation(ctx, update, "request")
            and not _in_latest_browse(ctx, q.data)):
        # An older list's button carries no list version. While a /request is
        # open, a title the latest browse no longer holds must not replace
        # its draft.
        # With no /trending list on hand the tap is a "Request it instead"
        # from /issue, which a /trending message would misdescribe.
        # The text follows where the tapped button came from: the /issue
        # dead-end button names the open request, never a /trending list. A
        # button sent before the marker existed falls back to whether a
        # /trending list is on hand.
        if from_issue or not ctx.user_data.get("dv_results"):
            refusal_text = "Finish or /cancel the open request first."
        else:
            refusal_text = ("That list is out of date. Run /trending again "
                            "to pick from the new one.")
        await update.effective_message.reply_text(refusal_text)
        return None
    # A pick from a Hub's list (the tapped message carries the back button)
    # ends back at that Hub; any other pick behaves like a typed /request.
    hub = REQUESTS_HUB if came_from_hub(q.message, REQUESTS_HUB) else None
    await send_typing(update, ctx)  # gate + title fetch can be slow
    if not await _gate_entry(update, ctx, hub=hub):
        set_hub_origin(ctx, hub)
        force_end_flow_conversations(ctx, update)
        return ConversationHandler.END
    force_4k = False
    if tapped is not None:
        refusal, force_4k = _coverage_verdict(ctx, tapped.media_type, tapped)
        if refusal:
            # Answer already spent, so the refusal goes out as a message.
            # The list stays, and so does the Hub origin of any flow the
            # user has open (set_hub_origin only runs once the tap proceeds).
            # An open /request flow stays open too: returning None from this
            # re-entered entry point leaves its conversation where it is. With
            # none open, None would still make PTB arm a conversation timeout
            # for a conversation that doesn't exist (a stray "timed out"
            # notice and a user-wide state wipe ten minutes later), so END.
            await update.effective_message.reply_text(
                f"{tapped.title}: {refusal}")
            if user_in_conversation(ctx, update, "request"):
                return None
            return ConversationHandler.END
    # The jump files for the tapper. Only a tap that goes ahead forgets a
    # Request for someone pick: a refused one leaves the open flow's pick, and
    # a tap in another chat leaves the DM's to the force-end below.
    _forget_behalf_pick(update, ctx)
    set_hub_origin(ctx, hub)
    # Another flow may still be parked in a pick state, an open /request
    # included; end everything so no stale flow can fire a timeout notice
    # mid-request. Ending an open /request pops its rq_* keys, rq_perms (just
    # cached by the gate above) among them, so put it back: the confirm needs
    # it for the 4K button. The force-end can't move before the gate, because
    # the coverage refusal above must leave an open /request alone.
    perms = ctx.user_data.get("rq_perms")
    force_end_flow_conversations(ctx, update)
    if perms is not None:
        ctx.user_data["rq_perms"] = perms
    try:
        media_type, tmdb_id, _ = _split_jump(q.data)
    except (ValueError, AttributeError):
        await end_flow(update, ctx, "Couldn't parse selection.", hub, REQUESTS_NEW,
                       typed_text="Couldn't parse selection. /request to start over.")
        return ConversationHandler.END
    # Title travels via user_data (stashed by ticket_flow only while its
    # parent re-search keeps the /issue conversation open); otherwise, or if
    # it got clobbered, fall back to a TMDb lookup.
    title = stash.get("title")
    year = stash.get("year", "")
    if not title or stash.get("tmdb_id") != tmdb_id:
        seerr: SeerrClient = ctx.bot_data["seerr"]
        try:
            title, year = await seerr.get_media_title(media_type, tmdb_id)
        except Exception:
            title, year = f"TMDb {tmdb_id}", ""
    # Bump the version even though this entry runs no search: two
    # consecutive from-ticket jumps must not share a version, or the first
    # jump's still-gated season/confirm buttons would pass the version
    # check and act on the second flow's draft.
    ctx.user_data["rq_search_version"] = _current_version(ctx) + 1
    ctx.user_data["rq_media"] = {"type": media_type, "tmdb_id": tmdb_id,
                                 "title": title, "year": year}
    if force_4k:
        ctx.user_data["rq_media"]["force_4k"] = True
        ctx.user_data["rq_media"]["std_status"] = tapped.status or 0
    if media_type == "tv":
        return await _show_season_picker(update, ctx)
    return await _show_confirm(update, ctx)


async def request_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _behalf_pick_lost(update, ctx):
        return await _refuse_lost_pick(update, ctx)
    query = update.effective_message.text.strip()
    await send_typing(update, ctx)
    return await _run_search(
        update, ctx, update.effective_message.reply_text, query,
        user_id=update.effective_user.id, hub=hub_origin(ctx),
    )


# Search + result screens

async def _run_search(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    reply_method,
    query: str,
    *,
    user_id: int,
    hub: Optional[str] = None,
) -> int:
    """One Seerr fetch (a full server page, up to 20 results); screens of 5
    render from the stored batch so paging never re-queries."""
    seerr: SeerrClient = ctx.bot_data["seerr"]
    try:
        results = await seerr.search(query, limit=REQUEST_SEARCH_FETCH_LIMIT)
    except Exception as exc:
        logger.exception("request search failed")
        await end_flow(update, ctx, f"Search failed. {user_friendly_message(exc)}",
                       hub, REQUESTS_NEW)
        return ConversationHandler.END
    if not results:
        await reply_method(
            f'No matches for "{query}". TMDB search is strict about '
            "spelling: try fewer words or a corrected title, or /cancel.")
        return RQ_TITLE

    version = _current_version(ctx) + 1
    ctx.user_data["rq_search_version"] = version
    ctx.user_data["rq_results"] = results
    ctx.user_data["rq_screen"] = 0
    ctx.user_data["rq_search_results"] = {
        "version": version,
        "by_key": {(r.media_type, r.tmdb_id): r for r in results},
    }
    return await _render_results_screen(reply_method, ctx, user_id=user_id)


def _result_line(index: int, r: MediaResult) -> str:
    type_emoji = "🎬" if r.media_type == "movie" else "📺"
    line = f"{index}. {type_emoji} {r.title}"
    if r.year:
        line += f" ({r.year})"
    note = _STATUS_NOTES.get(r.status or 0)
    if note:
        line += f" - {note}"
    return line


async def _render_results_screen(
    reply_method,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
) -> int:
    results: list = ctx.user_data["rq_results"]
    version: int = _current_version(ctx)
    screen: int = ctx.user_data.get("rq_screen", 0)
    last_screen = (len(results) - 1) // REQUEST_RESULTS_PER_PAGE
    screen = max(0, min(screen, last_screen))
    start = screen * REQUEST_RESULTS_PER_PAGE
    chunk = results[start:start + REQUEST_RESULTS_PER_PAGE]

    lines = ["Pick which one to request:", ""]
    for offset, r in enumerate(chunk):
        lines.append(_result_line(start + offset + 1, r))
    lines.append("")
    lines.append("🖼 = poster + details")

    # Numbered pick buttons (global numbering so screen 2 reads 6..10).
    rows: list[list[InlineKeyboardButton]] = []
    btn_row: list[InlineKeyboardButton] = []
    for offset, r in enumerate(chunk):
        n = start + offset
        keycap = KEYCAP_DIGITS[n] if n < len(KEYCAP_DIGITS) else str(n + 1)
        btn_row.append(InlineKeyboardButton(
            keycap, callback_data=f"{RQ_MEDIA}:{version}:{r.media_type}:{r.tmdb_id}",
        ))
        if len(btn_row) == KB_BUTTONS_PER_ROW:
            rows.append(btn_row)
            btn_row = []
    if btn_row:
        rows.append(btn_row)
    # One compact info row for the whole screen.
    rows.append([
        InlineKeyboardButton(
            f"🖼 {start + offset + 1}",
            callback_data=f"{RQ_INFO}:{version}:{r.media_type}:{r.tmdb_id}")
        for offset, r in enumerate(chunk)
    ])
    if last_screen > 0:
        nav: list[InlineKeyboardButton] = []
        if screen > 0:
            nav.append(InlineKeyboardButton(
                f"⬅️ Page {screen}",
                callback_data=f"{RQ_PAGE}:{version}:{screen - 1}"))
        if screen < last_screen:
            nav.append(InlineKeyboardButton(
                f"Page {screen + 2} ➡️",
                callback_data=f"{RQ_PAGE}:{version}:{screen + 1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(
        "🛑 Cancel", callback_data=f"{RQ_CANCEL}:{version}")])

    sent = await reply_method("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
    record_btn(ctx.application, user_id, sent)
    return RQ_PICK_MEDIA


async def request_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    try:
        _, version_s, screen_s = q.data.split(":")
        version = int(version_s)
        screen = int(screen_s)
    except (ValueError, AttributeError):
        await q.answer()
        return RQ_PICK_MEDIA
    # A page arrow on a superseded list must not re-render the NEW search
    # into the old message (or touch rq_screen); toast and leave both flows
    # alone.
    if version != _current_version(ctx) or not ctx.user_data.get("rq_results"):
        await q.answer(_STALE_MENU_TOAST, show_alert=True)
        return RQ_PICK_MEDIA
    await q.answer()
    ctx.user_data["rq_screen"] = screen
    return await _render_results_screen(
        q.edit_message_text, ctx, user_id=update.effective_user.id)


async def request_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Detail card: a separate message (photo when a poster exists) so the
    pick list stays live above it. Info-only by design -- the decision
    action stays on the numbered list. The card is NOT recorded in the
    button-gate history (it would evict the pick list); its Dismiss is
    gate-exempt instead."""
    q = update.callback_query
    try:
        _, version_s, media_type, tmdb_id_s = q.data.split(":")
        version = int(version_s)
        tmdb_id = int(tmdb_id_s)
    except (ValueError, AttributeError):
        await q.answer()
        return RQ_PICK_MEDIA
    current = ctx.user_data.get("rq_search_results") or {}
    if version != _current_version(ctx) or current.get("version") != version:
        # One answer per query: the alert IS the answer.
        await q.answer(_STALE_MENU_TOAST, show_alert=True)
        return RQ_PICK_MEDIA
    r: Optional[MediaResult] = (current.get("by_key") or {}).get((media_type, tmdb_id))
    if r is None:
        await q.answer()
        return RQ_PICK_MEDIA
    await q.answer()
    await send_detail_card(ctx, update.effective_chat.id, r,
                           update.effective_user.id)
    return RQ_PICK_MEDIA


async def request_info_dismiss(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Global handler (works even after the conversation ended, and exempt
    from the button gate): a detail card's Dismiss just deletes that card.

    Gate-exempt means callback data can be forged against ANY bot message
    id, so verify the referenced message really is a detail card (its
    keyboard carries the Dismiss button) before deleting -- otherwise a
    modified client in a group chat could silently delete the bot's
    messages to others."""
    q = update.callback_query
    await q.answer()
    markup = getattr(q.message, "reply_markup", None)
    is_card = any(
        getattr(btn, "callback_data", None) == RQ_INFO_DISMISS
        for row in getattr(markup, "inline_keyboard", ()) or ()
        for btn in row
    )
    if not is_card:
        logger.warning("rqx dismiss on a non-card message ignored")
        return
    try:
        await q.message.delete()
    except Exception as exc:
        logger.debug("detail-card delete failed (non-fatal)", exc_info=exc)


async def request_pick_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    hub = hub_origin(ctx)
    try:
        _, version_s, media_type, tmdb_id_s = q.data.split(":")
        version = int(version_s)
        tmdb_id = int(tmdb_id_s)
    except (ValueError, AttributeError):
        await q.answer()
        await end_flow(update, ctx, "Couldn't parse selection.", hub, REQUESTS_NEW,
                       typed_text="Couldn't parse selection. /request to start over.")
        return ConversationHandler.END
    current = ctx.user_data.get("rq_search_results") or {}
    if version != _current_version(ctx) or current.get("version") != version:
        # A pick on a superseded list must not end or mutate the active
        # flow; toast on the old message and leave everything alone.
        await q.answer(_STALE_MENU_TOAST, show_alert=True)
        return RQ_PICK_MEDIA
    selected = (current.get("by_key") or {}).get((media_type, tmdb_id))
    if selected is None:
        await q.answer()
        await end_flow(update, ctx, "Lost that selection.", hub, REQUESTS_NEW,
                       typed_text="Lost that selection. /request to start over.")
        return ConversationHandler.END

    status = selected.status or 0
    toast, force_4k = _coverage_verdict(ctx, media_type, selected)
    if toast:
        await q.answer(toast, show_alert=True)
        return RQ_PICK_MEDIA

    await q.answer()
    ctx.user_data["rq_media"] = {
        "type": media_type,
        "tmdb_id": tmdb_id,
        "title": selected.title,
        "year": selected.year,
        "force_4k": force_4k,
        # Remembered so the 4K-only confirm can word the standard-track
        # coverage honestly (available vs merely requested).
        "std_status": status,
    }
    if media_type == "tv":
        return await _show_season_picker(update, ctx, keep_list=True)
    return await _show_confirm(update, ctx)


# Season multi-select

def _season_keyboard(seasons: list, selected: set,
                     version: int) -> InlineKeyboardMarkup:
    """Multi-select season grid. Requestable seasons toggle a checkmark;
    already-covered ones render with ✔ and only toast on tap. Every
    callback carries the flow's search version so a superseded picker
    can't mutate a newer flow's selection."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for s in seasons:
        n = s.season_number
        if s.requestable:
            label = f"☑ S{n}" if n in selected else f"S{n}"
            cb = f"{RQ_SEASON}:{version}:{n}"
        else:
            label = f"S{n} ✔"
            cb = f"{RQ_SEASON_NA}:{version}:{n}"
        row.append(InlineKeyboardButton(label, callback_data=cb))
        if len(row) == SEASON_BUTTONS_PER_ROW:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("📦 Select all",
                             callback_data=f"{RQ_SEASON_ALL}:{version}"),
        InlineKeyboardButton("▶️ Done",
                             callback_data=f"{RQ_SEASON_DONE}:{version}"),
    ])
    rows.append([InlineKeyboardButton(
        "🛑 Cancel", callback_data=f"{RQ_CANCEL}:{version}")])
    return InlineKeyboardMarkup(rows)


def _media_label(media: dict) -> str:
    label = media["title"]
    if media.get("year"):
        label += f" ({media['year']})"
    return label


async def _show_season_picker(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                              *, keep_list: bool = False) -> int:
    """keep_list=True (the pick-list path): dead ends reply as a NEW message
    and stay in RQ_PICK_MEDIA so the list survives -- symmetric with the
    movie toasts. keep_list=False (the from-ticket jump, no list exists):
    dead ends edit in place and END."""
    q = update.callback_query
    seerr: SeerrClient = ctx.bot_data["seerr"]
    media = ctx.user_data["rq_media"]
    hub = hub_origin(ctx)

    async def _dead_end(text: str, retry: Optional[str] = None) -> int:
        if keep_list:
            await q.message.reply_text(text)
            return RQ_PICK_MEDIA
        await end_flow(update, ctx, text, hub, retry)
        return ConversationHandler.END

    try:
        seasons = await seerr.get_tv_season_availability(media["tmdb_id"])
    except Exception as exc:
        logger.exception("get_tv_season_availability failed")
        return await _dead_end(
            f"Couldn't fetch seasons. {user_friendly_message(exc)}",
            REQUESTS_NEW)
    if not seasons:
        return await _dead_end("No seasons found for this show.")
    if not any(s.requestable for s in seasons):
        return await _dead_end(
            "Every season of that show is already available or requested. "
            "Nothing to do 🎉"
        )
    ctx.user_data["rq_seasons"] = seasons
    ctx.user_data["rq_selected"] = set()
    # HTML + escape, never Markdown: raw titles (M*A*S*H, [REC]) break
    # Markdown entity parsing and kill the edit.
    sent = await q.edit_message_text(
        f"Selected: <b>{html.escape(_media_label(media))}</b>\n\n"
        "Which seasons? Tap to select (✔ = already available or requested), "
        "then ▶️ Done.",
        reply_markup=_season_keyboard(seasons, set(), _current_version(ctx)),
        parse_mode="HTML",
    )
    record_btn(ctx.application, update.effective_user.id, sent)
    return RQ_PICK_SEASONS


def _parse_versioned(data: str, *, want_arg: bool):
    """Parse "prefix:version[:arg]" -> (version, arg|None); raises ValueError
    on shape mismatch."""
    parts = data.split(":")
    if want_arg:
        return int(parts[1]), int(parts[2])
    return int(parts[1]), None


async def request_toggle_season(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    try:
        version, n = _parse_versioned(q.data, want_arg=True)
    except (ValueError, IndexError):
        await q.answer()
        return RQ_PICK_SEASONS
    if version != _current_version(ctx):
        await q.answer(_STALE_MENU_TOAST, show_alert=True)
        return RQ_PICK_SEASONS
    seasons = ctx.user_data.get("rq_seasons")
    selected: Optional[set] = ctx.user_data.get("rq_selected")
    if seasons is None or selected is None:
        await q.answer()
        await end_flow(update, ctx, "That selection expired.", hub_origin(ctx),
                       REQUESTS_NEW,
                       typed_text="That selection expired. /request to start over.")
        return ConversationHandler.END
    if n in selected:
        selected.discard(n)
    else:
        selected.add(n)
    await q.answer()
    try:
        await q.edit_message_reply_markup(
            _season_keyboard(seasons, selected, version))
    except Exception:
        # A double-tap can race two edits; the second is a no-op Telegram
        # rejects with "message is not modified". Selection state is already
        # updated, so ignore.
        logger.debug("season keyboard edit failed (non-fatal)", exc_info=True)
    return RQ_PICK_SEASONS


async def request_season_na(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    try:
        version, n = _parse_versioned(q.data, want_arg=True)
    except (ValueError, IndexError):
        await q.answer()
        return RQ_PICK_SEASONS
    if version != _current_version(ctx):
        # Same discipline as every sibling: a ✔ tap on a superseded picker
        # must not narrate the OLD flow's show into the new one.
        await q.answer(_STALE_MENU_TOAST, show_alert=True)
        return RQ_PICK_SEASONS
    await q.answer(f"Season {n} is already available or requested.", show_alert=False)
    return RQ_PICK_SEASONS


async def request_select_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    try:
        version, _ = _parse_versioned(q.data, want_arg=False)
    except (ValueError, IndexError):
        await q.answer()
        return RQ_PICK_SEASONS
    if version != _current_version(ctx):
        await q.answer(_STALE_MENU_TOAST, show_alert=True)
        return RQ_PICK_SEASONS
    seasons = ctx.user_data.get("rq_seasons")
    if seasons is None:
        await q.answer()
        await end_flow(update, ctx, "That selection expired.", hub_origin(ctx),
                       REQUESTS_NEW,
                       typed_text="That selection expired. /request to start over.")
        return ConversationHandler.END
    selected = {s.season_number for s in seasons if s.requestable}
    ctx.user_data["rq_selected"] = selected
    await q.answer("All requestable seasons selected")
    try:
        await q.edit_message_reply_markup(
            _season_keyboard(seasons, selected, version))
    except Exception as exc:
        logger.debug("season keyboard edit failed (non-fatal)", exc_info=exc)
    return RQ_PICK_SEASONS


async def request_seasons_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    try:
        version, _ = _parse_versioned(q.data, want_arg=False)
    except (ValueError, IndexError):
        await q.answer()
        return RQ_PICK_SEASONS
    if version != _current_version(ctx):
        await q.answer(_STALE_MENU_TOAST, show_alert=True)
        return RQ_PICK_SEASONS
    selected: Optional[set] = ctx.user_data.get("rq_selected")
    if selected is None:
        await q.answer()
        await end_flow(update, ctx, "That selection expired.", hub_origin(ctx),
                       REQUESTS_NEW,
                       typed_text="That selection expired. /request to start over.")
        return ConversationHandler.END
    if not selected:
        await q.answer("Pick at least one season first.", show_alert=True)
        return RQ_PICK_SEASONS
    await q.answer()
    return await _show_confirm(update, ctx)


# Confirm + submit

def _summarize_seasons(numbers: list[int]) -> str:
    """Compact season summary: [1,2,3,5] -> "S1-S3, S5"."""
    if not numbers:
        return ""
    numbers = sorted(numbers)
    parts: list[str] = []
    start = prev = numbers[0]
    for n in numbers[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(f"S{start}" if start == prev else f"S{start}-S{prev}")
        start = prev = n
    parts.append(f"S{start}" if start == prev else f"S{start}-S{prev}")
    return ", ".join(parts)


async def _quota_line(ctx: ContextTypes.DEFAULT_TYPE, tg_id: int,
                      media_type: str, *, target: Optional[dict] = None) -> str:
    """One line of quota context for the confirm screen, or "" when the
    user's quota is unlimited, unknown, or the lookup fails (the submit
    itself is the authority; this is a courtesy preview). An exhausted
    quota says so plainly instead of offering doomed arithmetic. `target` is
    the Request for someone person the confirm names, read only for the
    Admin."""
    try:
        is_admin, token, _ = await token_for(ctx, tg_id)
        if not is_admin:
            target = None
        if target is not None:
            # The Admin's own key reads the picked user's quota.
            seerr_id, token, whose = target["seerr_id"], None, "Their"
        else:
            store: UserStore = ctx.bot_data["store"]
            link = await store.get(tg_id)
            if link is None or not link.seerr_id:
                return ""
            seerr_id, whose = link.seerr_id, "Your"
        seerr: SeerrClient = ctx.bot_data["seerr"]
        quota = await seerr.get_quota(seerr_id, as_plex_token=token)
        bucket = quota.movie if media_type == "movie" else quota.tv
        if not bucket.limit or bucket.remaining is None:
            return ""
        unit = "requests" if media_type == "movie" else "seasons"
        if bucket.remaining <= 0:
            return (f"\n🙅 {whose} quota ({bucket.limit} {unit} per "
                    f"{day_count(bucket.days)}) is used up, Seerr will reject "
                    "this until older requests age out of the window.")
        return (f"\nQuota: {bucket.remaining} of {bucket.limit} {unit} left "
                f"in the current {bucket.days}-day window.")
    except Exception:
        logger.debug("quota preview failed (non-fatal)", exc_info=True)
        return ""


async def _show_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if _behalf_pick_lost(update, ctx):
        return await _refuse_lost_pick(update, ctx)
    # The person this confirm names, taken before any await and kept under a
    # key of its own: the submit files with this snapshot only, so a later
    # entry point clearing rq_for cannot turn it into the Admin's own Request.
    target = _behalf_target(update, ctx)
    ctx.user_data[_CONFIRM_KEY] = {
        "gen": flow_step_gen(ctx), "version": _current_version(ctx),
        "target": dict(target) if target is not None else None}
    await send_typing(update, ctx)  # quota preview is two network calls
    media = ctx.user_data["rq_media"]
    label = _media_label(media)
    if media["type"] == "tv":
        label += f" - {_summarize_seasons(sorted(ctx.user_data.get('rq_selected') or ()))}"
    quota_note = await _quota_line(ctx, update.effective_user.id, media["type"],
                                   target=target)
    perms = ctx.user_data.get("rq_perms", 0)
    offer_4k = can_request_4k(perms, media["type"])
    force_4k = bool(media.get("force_4k"))
    version = _current_version(ctx)

    text = f"Request <b>{html.escape(label)}</b>?"
    if target is not None:
        text = (f"Request <b>{html.escape(label)}</b> for "
                f"<b>{html.escape(target['name'])}</b>?")
    buttons: list[InlineKeyboardButton] = []
    if force_4k:
        # Word the standard-track coverage honestly: available vs merely
        # requested/downloading.
        if media.get("std_status") == MEDIA_STATUS_AVAILABLE:
            text += "\nAlready available in standard quality: request the 4K copy?"
        else:
            text += ("\nAlready requested in standard quality: request the "
                     "4K copy too?")
        buttons.append(InlineKeyboardButton(
            "✨ Request in 4K", callback_data=f"{RQ_GO_4K}:{version}"))
    else:
        buttons.append(InlineKeyboardButton(
            "✅ Request", callback_data=f"{RQ_GO}:{version}"))
        if offer_4k:
            buttons.append(InlineKeyboardButton(
                "✨ 4K", callback_data=f"{RQ_GO_4K}:{version}"))
    buttons.append(InlineKeyboardButton(
        "🛑 Cancel", callback_data=f"{RQ_CANCEL}:{version}"))
    text += html.escape(quota_note)

    sent = await q.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([buttons]),
        parse_mode="HTML",
    )
    record_btn(ctx.application, update.effective_user.id, sent)
    return RQ_CONFIRM


async def request_submit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Double-tap guard around the real submit (concurrent_updates(True):
    a second confirm tap runs in parallel and would file a duplicate)."""
    q = update.callback_query
    try:
        prefix, version_s = q.data.split(":")
        version = int(version_s)
    except (ValueError, AttributeError):
        await q.answer()
        return ConversationHandler.END
    if version != _current_version(ctx):
        # A confirm button on a superseded flow's message must not submit
        # the ACTIVE flow's draft.
        await q.answer(_STALE_MENU_TOAST, show_alert=True)
        return RQ_CONFIRM
    await q.answer()
    # The marker holds the flow generation the submit started under, so it
    # blocks only this flow's double tap: a /request started while this one
    # is still submitting runs under a later generation and is not blocked.
    gen = flow_step_gen(ctx)
    if ctx.user_data.get("rq_submitting") == gen:
        return ConversationHandler.END
    # Read before any await: the person this confirm named.
    confirm = ctx.user_data.get(_CONFIRM_KEY)
    if not isinstance(confirm, dict) or confirm.get("version") != version:
        confirm = None
    ctx.user_data["rq_submitting"] = gen
    # Remembered (not just parsed) so a relink-resume re-submits at the tier
    # the user actually chose.
    is4k = prefix == RQ_GO_4K
    ctx.user_data["rq_is4k"] = is4k
    try:
        return await _submit_request(update, ctx, is4k=is4k, confirm=confirm,
                                     gen=gen)
    finally:
        # Only our own marker: a newer flow's submit may have replaced it.
        if ctx.user_data.get("rq_submitting") == gen:
            ctx.user_data.pop("rq_submitting", None)


async def _submit_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                          *, is4k: bool = False, confirm: Optional[dict],
                          gen: int) -> int:
    """`confirm` is the tapped confirm's snapshot (None when there is none for
    its version) and `gen` the flow generation the submit started under."""
    q = update.callback_query
    hub = hub_origin(ctx)
    media = ctx.user_data.get("rq_media")
    if not media:
        await end_flow(update, ctx, "Lost conversation state.", hub, REQUESTS_NEW,
                       typed_text="Lost conversation state. /request to start over.")
        return ConversationHandler.END
    is_admin, token, decrypt_failed = await token_for(ctx, update.effective_user.id)
    if not is_admin and (decrypt_failed or not token):
        await end_flow(update, ctx,
                       "Your Plex link is incomplete. Link again from /account.",
                       hub,
                       typed_text="Your /link is incomplete. /link then /request to start over.")
        return ConversationHandler.END
    # Only the Admin can request for someone, so only the Admin's submit needs
    # the snapshot. Without one nothing tells whom the confirm named: file
    # nothing rather than the Admin's own Request.
    target = None
    if is_admin:
        if confirm is None:
            await end_flow(update, ctx, "Lost conversation state.", hub,
                           REQUESTS_NEW,
                           typed_text="Lost conversation state. /request to start over.")
            return ConversationHandler.END
        target = confirm.get("target")
        if target is not None and confirm.get("gen") != gen:
            # The user's flows were reset since the confirm was drawn (a
            # command or a Hub button, maybe in another chat): the pick is no
            # longer this flow's to file.
            await end_flow(update, ctx, _TARGET_CLEARED_TEXT, hub,
                           REQUESTS_BEHALF, typed_text=_TARGET_CLEARED_TYPED)
            return ConversationHandler.END

    seasons = None
    if media["type"] == "tv":
        seasons = sorted(ctx.user_data.get("rq_selected") or ())
        if not seasons:
            await end_flow(update, ctx, "No seasons selected.", hub, REQUESTS_NEW,
                           typed_text="No seasons selected. /request to start over.")
            return ConversationHandler.END

    label = _media_label(media)
    if seasons:
        label += f" - {_summarize_seasons(seasons)}"
    if is4k:
        label += " [4K]"

    # Working state before the network hop (mirrors /issue's submit): the
    # per-user auth + create can take a while on a degraded Seerr.
    try:
        await q.edit_message_text(
            f"Submitting <b>{html.escape(label)}</b>…", parse_mode="HTML")
    except Exception:
        logger.debug("submit working-state edit failed (non-fatal)",
                     exc_info=True)
    await send_typing(update, ctx)

    seerr: SeerrClient = ctx.bot_data["seerr"]
    who = target["name"] if target else None
    try:
        created = await seerr.create_request(
            media_type=media["type"],
            tmdb_id=media["tmdb_id"],
            seasons=seasons,
            is4k=is4k,
            as_plex_token=token,
            user_id=target["seerr_id"] if target else None,
        )
    except PlexTokenInvalidError:
        # The resume carries this request itself, never the draft in
        # user_data: a /request started meanwhile and ended by a command
        # must not be filed in its place.
        await prompt_plex_relink(update, ctx, resume_kind="submit_request",
                                 resume_payload={"media": dict(media),
                                                 "seasons": seasons,
                                                 "is4k": is4k},
                                 back=back_button(hub) if hub else None)
        return ConversationHandler.END
    except NothingToRequestError:
        await end_flow(
            update, ctx,
            f"Everything in <b>{html.escape(label)}</b> is already available "
            "or requested. Nothing to do 🎉",
            hub, parse_mode="HTML",
        )
        _clear_finished_rq_state(ctx)
        return ConversationHandler.END
    except DuplicateRequestError:
        await end_flow(
            update, ctx,
            f"<b>{html.escape(label)}</b> is already requested and on its way.",
            hub, parse_mode="HTML",
            typed_text=f"<b>{html.escape(label)}</b> is already requested. "
                       "It's on its way. See /requests for status.",
        )
        _clear_finished_rq_state(ctx)
        return ConversationHandler.END
    except QuotaExceededError as exc:
        await end_flow(
            update, ctx,
            _quota_text(exc, who), hub, parse_mode="HTML",
        )
        _clear_finished_rq_state(ctx)
        return ConversationHandler.END
    except RequestPermissionError as exc:
        await end_flow(
            update, ctx, _permission_text(exc, who), hub, parse_mode="HTML",
        )
        _clear_finished_rq_state(ctx)
        return ConversationHandler.END
    except Exception as exc:
        logger.exception("create_request failed")
        await end_flow(
            update, ctx,
            f"Couldn't submit the request. {user_friendly_message(exc)}",
            hub, REQUESTS_NEW)
        return ConversationHandler.END

    # Word the outcome from what Seerr GRANTED: it silently drops seasons
    # that got covered between picker render and submit, and the
    # confirmation must not claim them (the /requests list would contradict
    # it).
    dropped_note = ""
    if seasons and created.seasons:
        granted = sorted(created.seasons)
        if set(granted) != set(seasons):
            label = _media_label(media) + f" - {_summarize_seasons(granted)}"
            if is4k:
                label += " [4K]"
            dropped = sorted(set(seasons) - set(granted))
            dropped_note = (f"\n({_summarize_seasons(dropped)} was already "
                            "available or requested, so Seerr skipped it.)")
    approved = created.status == REQUEST_STATUS_APPROVED
    # Promise a DM only when the Admin has Request updates to requesters on.
    settings_store = ctx.bot_data.get("settings_store")
    dms_on = (settings_store is None
              or settings_store.settings.tg_notify_requester)
    for_whom = f" for {html.escape(who)}" if who else ""
    dm_to = html.escape(who) if who else "you"
    if approved:
        text = (f"✅ Approved{for_whom}: <b>{html.escape(label)}</b> is "
                f"being grabbed.{html.escape(dropped_note)}"
                + (f"\nI'll DM {dm_to} when it's available." if dms_on else ""))
    else:
        text = (f"📨 Requested{for_whom}: <b>{html.escape(label)}</b>."
                f"{html.escape(dropped_note)}\n"
                "Waiting for approval."
                + (f" I'll DM {dm_to} when it moves." if dms_on else ""))
    # The result stays on the flow message, with the way back to the Hub.
    sent = await edit_or_send(q, text, parse_mode="HTML",
                              reply_markup=end_markup(REQUESTS_HUB))
    record_btn(ctx.application, update.effective_user.id, sent)
    # The card lives in this chat, but its row is owned by the picked user
    # when requesting on behalf of someone (webhook events and cancels match
    # on that owner), otherwise by the requester.
    await _send_progress_card(
        ctx, chat_id=update.effective_chat.id,
        owner_id=((target or {}).get("telegram_id")
                  or update.effective_user.id),
        media_type=media["type"], tmdb_id=media["tmdb_id"], label=label,
        is4k=is4k, approved=approved,
        # The seasons Seerr granted (the picked ones when it lists none), so
        # a later cancel can tell this card from another season pick of the
        # same show.
        seasons=(sorted(created.seasons or seasons)
                 if media["type"] == "tv" else None))
    _clear_finished_rq_state(ctx)
    return ConversationHandler.END


async def _send_progress_card(ctx: ContextTypes.DEFAULT_TYPE, *, chat_id: int,
                              owner_id: int, media_type: str, tmdb_id: int,
                              label: str, is4k: bool, approved: bool,
                              seasons: Optional[list]) -> None:
    """Send the Progress card for a just-filed Request and store its row.
    One helper for every submit path (normal, remaining seasons, relink
    resume) so they cannot drift. `owner_id` is the Telegram id of the
    requester (the Request's owner), which can differ from the chat the card
    is sent to when the Admin requests for someone. The card is a separate
    new message: the poller paints download progress onto it and lifecycle
    webhooks finish it; neither edits the confirmation. Best effort."""
    try:
        card = await ctx.bot.send_message(
            chat_id=chat_id,
            text=card_text(label, APPROVED_LINE if approved
                            else "⏳ Waiting for approval…"),
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("couldn't send the Progress card (no row added)", exc_info=exc)
        return
    try:
        # The Progress card row also drives the SABnzbd priority bump.
        store: UserStore = ctx.bot_data["store"]
        await store.add_progress_card(
            chat_id=chat_id,
            message_id=card.message_id,
            user_id=owner_id,
            media_type=media_type,
            tmdb_id=tmdb_id,
            label=label,
            is4k=is4k,
            status="grabbing" if approved else "waiting",
            timeout_hours=PROGRESS_CARD_TIMEOUT_HOURS,
            seasons=seasons,
        )
    except Exception as exc:
        logger.exception("couldn't enqueue the Progress card row (card stays static)", exc_info=exc)


def _quota_text(exc: Exception, who: Optional[str] = None) -> str:
    """`who` names the user an Admin requested for; None means the
    requester's own quota."""
    if who:
        return (f"🙅 {html.escape(who)}: {html.escape(str(exc))} Their quota "
                "frees up as older requests age out of the window.")
    return (f"🙅 {html.escape(str(exc))} Your quota frees up as older "
            "requests age out of the window.")


def _permission_text(exc: Exception, who: Optional[str] = None) -> str:
    if who:
        return (f"🚫 Seerr said no for {html.escape(who)}: "
                f"{html.escape(str(exc))}")
    return f"🚫 Seerr said no: {html.escape(str(exc))}"


# Request remaining seasons (global handlers: the button sits on detail cards
# and availability notices, outside any /request conversation)

NOTHING_LEFT_TEXT = "Nothing left to request 🎉"


def _tmdb_from_data(q) -> Optional[int]:
    try:
        return int(q.data.split(":")[1])
    except (ValueError, IndexError, AttributeError):
        return None


async def remaining_seasons_ask(update: Update,
                                ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Tap on "Request remaining seasons": re-read the seasons (they may have
    changed since the card was sent) and send a confirm naming them."""
    q = update.callback_query
    tmdb_id = _tmdb_from_data(q)
    if tmdb_id is None:
        await q.answer()
        return
    await q.answer()
    if await require_seerr(update, ctx) is None:
        return
    user_id = update.effective_user.id
    is_admin, token, decrypt_failed = await token_for(ctx, user_id)
    if not is_admin and not token:
        await q.message.reply_text(
            DECRYPT_FAILED_MSG if decrypt_failed else
            "DM me /link first so requests are filed as you.")
        return
    seerr: SeerrClient = ctx.bot_data["seerr"]
    left = await requestable_seasons(seerr, tmdb_id)
    if left is None:
        await q.message.reply_text("Couldn't fetch seasons. Try again in a bit.")
        return
    if not left:
        await q.message.reply_text(NOTHING_LEFT_TEXT)
        return
    try:
        title, year = await seerr.get_media_title("tv", tmdb_id)
    except Exception:
        title, year = f"TMDb {tmdb_id}", ""
    label = _media_label({"title": title, "year": year})
    # Outside the conversation: a stale pick must not change whose quota shows.
    quota_note = await _quota_line(ctx, user_id, "tv")
    text = (f"Request the remaining seasons of <b>{html.escape(label)}</b>?\n"
            f"Seasons: {_summarize_seasons(left)}" + html.escape(quota_note))
    sent = await q.message.reply_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "✅ Send", callback_data=f"{RQ_REMAINING_GO}:{tmdb_id}"),
            InlineKeyboardButton("🛑 Cancel", callback_data=RQ_INFO_DISMISS),
        ]]))
    record_btn(ctx.application, user_id, sent)


async def remaining_seasons_send(update: Update,
                                 ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Send on the remaining-seasons confirm: POST /request with seasons
    "all" as the user. Seerr skips seasons already available or requested."""
    q = update.callback_query
    tmdb_id = _tmdb_from_data(q)
    if tmdb_id is None:
        await q.answer()
        return
    await q.answer()
    busy: set = ctx.user_data.setdefault("rq_remaining_busy", set())
    if tmdb_id in busy:
        return  # a second tap while the first is in flight
    busy.add(tmdb_id)
    try:
        await _remaining_seasons_submit(update, ctx, tmdb_id)
    finally:
        busy.discard(tmdb_id)


async def _remaining_seasons_submit(update: Update,
                                    ctx: ContextTypes.DEFAULT_TYPE,
                                    tmdb_id: int) -> None:
    q = update.callback_query
    is_admin, token, decrypt_failed = await token_for(ctx, update.effective_user.id)
    if not is_admin and not token:
        await edit_or_send(q, DECRYPT_FAILED_MSG if decrypt_failed else
                           "DM me /link first so requests are filed as you.")
        return
    try:
        title, year = await ctx.bot_data["seerr"].get_media_title("tv", tmdb_id)
    except Exception:
        title, year = f"TMDb {tmdb_id}", ""
    label = _media_label({"title": title, "year": year})
    try:
        await q.edit_message_text(
            f"Submitting <b>{html.escape(label)}</b>…", parse_mode="HTML")
    except Exception:
        logger.debug("submit working-state edit failed (non-fatal)",
                     exc_info=True)
    seerr: SeerrClient = ctx.bot_data["seerr"]
    # The seasons a "all" request will stick for, read before the POST: the
    # card's fallback when Seerr's response lists none (None when the lookup
    # failed).
    expected = await requestable_seasons(seerr, tmdb_id)
    try:
        created = await seerr.create_request(
            media_type="tv", tmdb_id=tmdb_id, seasons="all",
            as_plex_token=token)
    except PlexTokenInvalidError:
        await prompt_plex_relink(update, ctx)
        return
    except NothingToRequestError:
        await edit_or_send(q, NOTHING_LEFT_TEXT)
        return
    except DuplicateRequestError:
        await edit_or_send(
            q, f"<b>{html.escape(label)}</b> is already requested and on its "
               "way.", parse_mode="HTML")
        return
    except QuotaExceededError as exc:
        await edit_or_send(q, _quota_text(exc), parse_mode="HTML")
        return
    except RequestPermissionError as exc:
        await edit_or_send(q, _permission_text(exc), parse_mode="HTML")
        return
    except Exception as exc:
        logger.exception("remaining-seasons create_request failed")
        await edit_or_send(
            q, f"Couldn't submit the request. {user_friendly_message(exc)}")
        return
    if created.seasons:
        label += f" - {_summarize_seasons(created.seasons)}"
    settings_store = ctx.bot_data.get("settings_store")
    dms_on = (settings_store is None
              or settings_store.settings.tg_notify_requester)
    if created.status == REQUEST_STATUS_APPROVED:
        text = (f"✅ Approved: <b>{html.escape(label)}</b> is being grabbed."
                + ("\nI'll DM you when it's available." if dms_on else ""))
    else:
        text = (f"📨 Requested: <b>{html.escape(label)}</b>.\n"
                "Waiting for approval."
                + (" I'll DM you when it moves." if dms_on else ""))
    await edit_or_send(q, text, parse_mode="HTML")
    await _send_progress_card(
        ctx, chat_id=update.effective_chat.id,
        owner_id=update.effective_user.id, media_type="tv", tmdb_id=tmdb_id,
        label=label, is4k=False,
        approved=created.status == REQUEST_STATUS_APPROVED,
        seasons=(sorted(created.seasons or expected)
                 if (created.seasons or expected) else None))


def _clear_rq_state(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    for key in _RQ_KEYS:
        ctx.user_data.pop(key, None)


def _clear_finished_rq_state(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """The end of a submit: clear the draft unless the user's flows were
    force-ended while it ran. Then a newer /request may own these keys (the
    force-end already cleared the draft this submit belonged to), and wiping
    them would break its list and confirm."""
    if not flow_step_is_stale(ctx):
        _clear_rq_state(ctx)


async def _resume_submit_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                                 payload: dict) -> None:
    """Relink-resume executor. Submits exactly the request the payload holds,
    never the draft in user_data (a newer /request may have replaced it). A
    marker with no payload request (written before the payload carried one)
    is treated as a lost draft."""
    if user_in_conversation(ctx, update, "request"):
        await update.effective_message.reply_text(
            "You've started a new /request since then, so I didn't auto-submit "
            "the interrupted one. Finish the current one instead."
        )
        return
    media = payload.get("media")
    if not media:
        await update.effective_message.reply_text(
            "The interrupted request's draft is gone. /request to start over.")
        return
    # No callback query to edit here; submit through a thin shim that
    # replies instead of editing.
    is_admin, token, decrypt_failed = await token_for(ctx, update.effective_user.id)
    if not is_admin and (decrypt_failed or not token):
        await update.effective_message.reply_text(
            "Your /link still isn't usable. /link then /request to start over.")
        return
    seasons = None
    if media["type"] == "tv":
        seasons = sorted(payload.get("seasons") or ()) or None
        if not seasons:
            await update.effective_message.reply_text(
                "The interrupted request lost its season selection. "
                "/request to start over.")
            return
    is4k = bool(payload.get("is4k"))
    seerr: SeerrClient = ctx.bot_data["seerr"]
    label = _media_label(media)
    if is4k:
        label += " [4K]"
    try:
        created = await seerr.create_request(
            media_type=media["type"], tmdb_id=media["tmdb_id"],
            seasons=seasons, is4k=is4k, as_plex_token=token,
        )
    except Exception as exc:
        await update.effective_message.reply_text(
            f"Couldn't submit the request. {user_friendly_message(exc)}")
        return
    if media["type"] == "tv" and (created.seasons or seasons):
        label += f" - {_summarize_seasons(sorted(created.seasons or seasons))}"
    if created.status == REQUEST_STATUS_APPROVED:
        msg = f"✅ Approved: {label} is being grabbed."
    else:
        msg = f"📨 Requested: {label}. Waiting for approval."
    await update.effective_message.reply_text(msg)
    await _send_progress_card(
        ctx, chat_id=update.effective_chat.id,
        owner_id=update.effective_user.id, media_type=media["type"],
        tmdb_id=media["tmdb_id"], label=label, is4k=is4k,
        approved=created.status == REQUEST_STATUS_APPROVED,
        seasons=(sorted(created.seasons or seasons)
                 if media["type"] == "tv" else None))
    # A /request started while this resume waited on Seerr owns the keys now
    # (the re-link step it runs in, strong or weak PIN, is then stale).
    _clear_finished_rq_state(ctx)


RELINK_RESUME_EXECUTORS["submit_request"] = _resume_submit_request


async def request_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        # A Cancel on a superseded message must not kill the active flow --
        # the one control that skipped version stamping was exactly the one
        # that ended the wrong conversation. Returning None keeps the
        # current state untouched.
        try:
            version = int(q.data.split(":")[1])
        except (ValueError, IndexError):
            version = None
        if version is not None and version != _current_version(ctx):
            await q.answer(_STALE_MENU_TOAST, show_alert=True)
            return None
        await q.answer("Cancelled")
    await end_flow(update, ctx, "Cancelled.", hub_origin(ctx),
                   typed_text="Cancelled. /request to start over.")
    _clear_rq_state(ctx)
    return ConversationHandler.END
