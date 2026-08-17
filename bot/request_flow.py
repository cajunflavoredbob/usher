"""/request conversation: search, pick media, pick seasons (TV), confirm,
submit to Seerr as the linked user.

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
    RQ_CANCEL,
    RQ_FROM_ISSUE,
    RQ_GO,
    RQ_GO_4K,
    RQ_INFO,
    RQ_INFO_DISMISS,
    RQ_MEDIA,
    RQ_PAGE,
    RQ_SEASON,
    RQ_SEASON_ALL,
    RQ_SEASON_DONE,
    RQ_SEASON_NA,
)
from bot.media_card import send_detail_card
from bot.shared import (
    DECRYPT_FAILED_MSG,
    KEYCAP_DIGITS,
    RQ_CONFIRM,
    RQ_PICK_MEDIA,
    RQ_PICK_SEASONS,
    RQ_TITLE,
    RELINK_RESUME_EXECUTORS,
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
    REQUEST_WATCH_TIMEOUT_HOURS,
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
_RQ_KEYS = ("rq_media", "rq_search_results",
            "rq_results", "rq_screen", "rq_perms", "rq_is4k",
            "rq_seasons", "rq_selected", "rq_submitting")

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

_STALE_MENU_TOAST = "That menu is from an older search — use the newest one."

def _current_version(ctx: ContextTypes.DEFAULT_TYPE) -> int:
    return ctx.user_data.get("rq_search_version") or 0


async def _request_timeout(update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_rq_state(ctx)
    try:
        if update is not None and update.effective_chat is not None:
            await ctx.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⏱️ The /request flow timed out. Start again with /request.",
            )
    except Exception:
        logger.debug("request-timeout notice failed (non-fatal)", exc_info=True)
    return ConversationHandler.END


async def _request_expect_text_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "I can only read text here — type the title, or /cancel to stop.")
    return RQ_TITLE


def _request_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("request", request_start),
            # Jump-in from the /issue not-in-library dead-end.
            CallbackQueryHandler(request_from_issue, pattern=fr"^{RQ_FROM_ISSUE}:"),
        ],
        states={
            RQ_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, request_title),
                MessageHandler(~filters.COMMAND, _request_expect_text_title),
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


async def _gate_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Shared /request entry gate: Seerr configured + a usable link. The
    admin passes without a mapping (admin-key attribution, like /issue's
    admin paths). On success, the user's Seerr permission bitmask is cached
    in user_data so pick/confirm can decide whether to offer 4K without a
    round-trip per screen."""
    if await require_seerr(update, ctx) is None:
        return False
    is_admin, token, decrypt_failed = await token_for(ctx, update.effective_user.id)
    if not is_admin:
        if decrypt_failed:
            await update.effective_message.reply_text(DECRYPT_FAILED_MSG)
            return False
        if not token:
            await update.effective_message.reply_text(
                "DM me /link first so requests are filed as you. It's a quick "
                "Plex sign-in - no username needed."
            )
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


async def request_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    # Typing indicator BEFORE the gate: its permission fetch can ride a cold
    # per-user auth (retry chain 60s+ on a degraded Seerr) and the bot
    # otherwise looks frozen before its first reply.
    await send_typing(update, ctx)
    if not await _gate_entry(update, ctx):
        return ConversationHandler.END
    # `/request dune part two` skips the title prompt.
    query = " ".join(ctx.args or ()).strip()
    if query:
        return await _run_search(
            update.effective_message.reply_text, ctx, query,
            user_id=update.effective_user.id,
        )
    await update.effective_message.reply_text(
        "What movie or show do you want? (Reply with the title.)"
    )
    return RQ_TITLE


async def request_from_issue(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry via the /issue dead-end button. The tapped media is already
    known, so this drops straight into the season picker / confirm."""
    q = update.callback_query
    await q.answer()
    # Another flow may still be parked in a pick state; end everything (this
    # conversation isn't active yet, so it's untouched) so no stale flow can
    # fire a timeout notice mid-request.
    force_end_flow_conversations(ctx, update)
    await send_typing(update, ctx)  # gate + title fetch can be slow
    if not await _gate_entry(update, ctx):
        return ConversationHandler.END
    try:
        _, media_type, tmdb_id_s = q.data.split(":")
        tmdb_id = int(tmdb_id_s)
    except (ValueError, AttributeError):
        await q.edit_message_text("Couldn't parse selection. /request to start over.")
        return ConversationHandler.END
    # Title travels via user_data (stashed by issue_flow when it renders the
    # button); fall back to a TMDb lookup if it got clobbered.
    stash = ctx.user_data.pop("rq_jump_media", None) or {}
    title = stash.get("title")
    year = stash.get("year", "")
    if not title or stash.get("tmdb_id") != tmdb_id:
        seerr: SeerrClient = ctx.bot_data["seerr"]
        try:
            title, year = await seerr.get_media_title(media_type, tmdb_id)
        except Exception:
            title, year = f"TMDb {tmdb_id}", ""
    # Bump the version even though this entry runs no search: two
    # consecutive from-issue jumps must not share a version, or the first
    # jump's still-gated season/confirm buttons would pass the version
    # check and act on the second flow's draft.
    ctx.user_data["rq_search_version"] = _current_version(ctx) + 1
    ctx.user_data["rq_media"] = {"type": media_type, "tmdb_id": tmdb_id,
                                 "title": title, "year": year}
    if media_type == "tv":
        return await _show_season_picker(update, ctx)
    return await _show_confirm(update, ctx)


async def request_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.effective_message.text.strip()
    await send_typing(update, ctx)
    return await _run_search(
        update.effective_message.reply_text, ctx, query,
        user_id=update.effective_user.id,
    )


# --- Search + result screens --------------------------------------------------

async def _run_search(
    reply_method,
    ctx: ContextTypes.DEFAULT_TYPE,
    query: str,
    *,
    user_id: int,
) -> int:
    """One Seerr fetch (a full server page, up to 20 results); screens of 5
    render from the stored batch so paging never re-queries."""
    seerr: SeerrClient = ctx.bot_data["seerr"]
    try:
        results = await seerr.search(query, limit=REQUEST_SEARCH_FETCH_LIMIT)
    except Exception as exc:
        logger.exception("request search failed")
        await reply_method(f"Search failed. {user_friendly_message(exc)}")
        return ConversationHandler.END
    if not results:
        await reply_method(f'No matches for "{query}". Try a different title, or /cancel.')
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
        line += f" — {note}"
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
    await send_detail_card(ctx, update.effective_chat.id, r)
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
    except Exception:
        logger.debug("detail-card delete failed (non-fatal)", exc_info=True)


async def request_pick_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    try:
        _, version_s, media_type, tmdb_id_s = q.data.split(":")
        version = int(version_s)
        tmdb_id = int(tmdb_id_s)
    except (ValueError, AttributeError):
        await q.answer()
        await q.edit_message_text("Couldn't parse selection. /request to start over.")
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
        await q.edit_message_text("Lost that selection. /request to start over.")
        return ConversationHandler.END

    status = selected.status or 0
    # Blocklisted media isn't requestable in either tier or type; preempt
    # before the user invests in the season picker just to be told no.
    if status == MEDIA_STATUS_BLOCKLISTED:
        await q.answer("That title isn't requestable here 🚫", show_alert=True)
        return RQ_PICK_MEDIA

    force_4k = False
    if media_type == "movie" and status in _COVERED_STATUSES:
        # Standard track already covered. A 4K-permitted user can still
        # want the 4K copy, so pass through to a 4K-only confirm unless
        # the 4K track is covered too.
        perms = ctx.user_data.get("rq_perms", 0)
        covered_4k = (selected.status_4k or 0) in _COVERED_STATUSES
        if can_request_4k(perms, "movie") and not covered_4k:
            force_4k = True
        elif status == MEDIA_STATUS_AVAILABLE:
            msg = ("Already available in Plex ✅ (including 4K)"
                   if covered_4k and can_request_4k(perms, "movie")
                   else "Already available in Plex ✅")
            await q.answer(msg, show_alert=True)
            return RQ_PICK_MEDIA
        else:
            await q.answer("Already requested — it's on its way ⏳",
                           show_alert=True)
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


# --- Season multi-select ------------------------------------------------------

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
    movie toasts. keep_list=False (the from-issue jump, no list exists):
    dead ends edit in place and END."""
    q = update.callback_query
    seerr: SeerrClient = ctx.bot_data["seerr"]
    media = ctx.user_data["rq_media"]

    async def _dead_end(text: str) -> int:
        if keep_list:
            await q.message.reply_text(text)
            return RQ_PICK_MEDIA
        await q.edit_message_text(text)
        return ConversationHandler.END

    try:
        seasons = await seerr.get_tv_season_availability(media["tmdb_id"])
    except Exception as exc:
        logger.exception("get_tv_season_availability failed")
        return await _dead_end(f"Couldn't fetch seasons. {user_friendly_message(exc)}")
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
        await q.edit_message_text("That selection expired. /request to start over.")
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
        await q.edit_message_text("That selection expired. /request to start over.")
        return ConversationHandler.END
    selected = {s.season_number for s in seasons if s.requestable}
    ctx.user_data["rq_selected"] = selected
    await q.answer("All requestable seasons selected")
    try:
        await q.edit_message_reply_markup(
            _season_keyboard(seasons, selected, version))
    except Exception:
        logger.debug("season keyboard edit failed (non-fatal)", exc_info=True)
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
        await q.edit_message_text("That selection expired. /request to start over.")
        return ConversationHandler.END
    if not selected:
        await q.answer("Pick at least one season first.", show_alert=True)
        return RQ_PICK_SEASONS
    await q.answer()
    return await _show_confirm(update, ctx)


# --- Confirm + submit ---------------------------------------------------------

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
                      media_type: str) -> str:
    """One line of quota context for the confirm screen, or "" when the
    user's quota is unlimited, unknown, or the lookup fails (the submit
    itself is the authority; this is a courtesy preview). An exhausted
    quota says so plainly instead of offering doomed arithmetic."""
    try:
        is_admin, token, _ = await token_for(ctx, tg_id)
        store: UserStore = ctx.bot_data["store"]
        mapping = await store.get(tg_id)
        if mapping is None or not mapping.seerr_id:
            return ""
        seerr: SeerrClient = ctx.bot_data["seerr"]
        quota = await seerr.get_quota(mapping.seerr_id, as_plex_token=token)
        bucket = quota.movie if media_type == "movie" else quota.tv
        if not bucket.limit or bucket.remaining is None:
            return ""
        unit = "requests" if media_type == "movie" else "seasons"
        if bucket.remaining <= 0:
            return (f"\n🙅 Your quota ({bucket.limit} {unit} per "
                    f"{bucket.days} days) is used up — Seerr will reject "
                    "this until older requests age out of the window.")
        return (f"\nQuota: {bucket.remaining} of {bucket.limit} {unit} left "
                f"in the current {bucket.days}-day window.")
    except Exception:
        logger.debug("quota preview failed (non-fatal)", exc_info=True)
        return ""


async def _show_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await send_typing(update, ctx)  # quota preview is two network calls
    media = ctx.user_data["rq_media"]
    label = _media_label(media)
    if media["type"] == "tv":
        label += f" — {_summarize_seasons(sorted(ctx.user_data.get('rq_selected') or ()))}"
    quota_note = await _quota_line(ctx, update.effective_user.id, media["type"])
    perms = ctx.user_data.get("rq_perms", 0)
    offer_4k = can_request_4k(perms, media["type"])
    force_4k = bool(media.get("force_4k"))
    version = _current_version(ctx)

    text = f"Request <b>{html.escape(label)}</b>?"
    buttons: list[InlineKeyboardButton] = []
    if force_4k:
        # Word the standard-track coverage honestly: available vs merely
        # requested/downloading.
        if media.get("std_status") == MEDIA_STATUS_AVAILABLE:
            text += "\nAlready available in standard quality — request the 4K copy?"
        else:
            text += ("\nAlready requested in standard quality — request the "
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
    if ctx.user_data.get("rq_submitting"):
        return ConversationHandler.END
    ctx.user_data["rq_submitting"] = True
    # Remembered (not just parsed) so a relink-resume re-submits at the tier
    # the user actually chose.
    is4k = prefix == RQ_GO_4K
    ctx.user_data["rq_is4k"] = is4k
    try:
        return await _submit_request(update, ctx, is4k=is4k)
    finally:
        ctx.user_data.pop("rq_submitting", None)


async def _submit_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                          *, is4k: bool = False) -> int:
    q = update.callback_query
    media = ctx.user_data.get("rq_media")
    if not media:
        await q.edit_message_text(
            "Lost conversation state. /request to start over.")
        return ConversationHandler.END
    is_admin, token, decrypt_failed = await token_for(ctx, update.effective_user.id)
    if not is_admin and (decrypt_failed or not token):
        await q.edit_message_text(
            "Your /link is incomplete. /link then /request to start over.")
        return ConversationHandler.END

    seasons = None
    if media["type"] == "tv":
        seasons = sorted(ctx.user_data.get("rq_selected") or ())
        if not seasons:
            await q.edit_message_text("No seasons selected. /request to start over.")
            return ConversationHandler.END

    label = _media_label(media)
    if seasons:
        label += f" — {_summarize_seasons(seasons)}"
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
    try:
        created = await seerr.create_request(
            media_type=media["type"],
            tmdb_id=media["tmdb_id"],
            seasons=seasons,
            is4k=is4k,
            as_plex_token=token,
        )
    except PlexTokenInvalidError:
        # The draft stays in user_data for the post-relink resume.
        await prompt_plex_relink(update, ctx, resume_kind="submit_request",
                                 resume_payload={})
        return ConversationHandler.END
    except NothingToRequestError:
        await q.edit_message_text(
            f"Everything in <b>{html.escape(label)}</b> is already available "
            "or requested. Nothing to do 🎉",
            parse_mode="HTML",
        )
        _clear_rq_state(ctx)
        return ConversationHandler.END
    except DuplicateRequestError:
        await q.edit_message_text(
            f"<b>{html.escape(label)}</b> is already requested — it's on its "
            "way. See /requests for status.",
            parse_mode="HTML",
        )
        _clear_rq_state(ctx)
        return ConversationHandler.END
    except QuotaExceededError as exc:
        await q.edit_message_text(
            f"🙅 {html.escape(str(exc))} Your quota frees up as older "
            "requests age out of the window.",
            parse_mode="HTML",
        )
        _clear_rq_state(ctx)
        return ConversationHandler.END
    except RequestPermissionError as exc:
        await q.edit_message_text(
            f"🚫 Seerr said no: {html.escape(str(exc))}",
            parse_mode="HTML",
        )
        _clear_rq_state(ctx)
        return ConversationHandler.END
    except Exception as exc:
        logger.exception("create_request failed")
        await q.edit_message_text(
            f"Couldn't submit the request. {user_friendly_message(exc)}")
        return ConversationHandler.END

    # Word the outcome from what Seerr GRANTED: it silently drops seasons
    # that got covered between picker render and submit, and the
    # confirmation must not claim them (the /requests list would contradict
    # it).
    dropped_note = ""
    if seasons and created.seasons:
        granted = sorted(created.seasons)
        if set(granted) != set(seasons):
            label = _media_label(media) + f" — {_summarize_seasons(granted)}"
            if is4k:
                label += " [4K]"
            dropped = sorted(set(seasons) - set(granted))
            dropped_note = (f"\n({_summarize_seasons(dropped)} was already "
                            "available or requested, so Seerr skipped it.)")
    if created.status == REQUEST_STATUS_APPROVED:
        text = (f"✅ Approved: <b>{html.escape(label)}</b> is being grabbed."
                f"{html.escape(dropped_note)}\n"
                "I'll DM you when it's available.")
    else:
        text = (f"📨 Requested: <b>{html.escape(label)}</b>."
                f"{html.escape(dropped_note)}\n"
                "Waiting for approval — I'll DM you when it moves.")
    text += "\n\nUse /requests to check on it."
    sent = await q.edit_message_text(text, parse_mode="HTML")
    # Track the confirmation as a morphing card: the watch poller paints
    # download progress onto THIS message, and lifecycle webhooks finish it.
    try:
        store: UserStore = ctx.bot_data["store"]
        await store.add_request_watch(
            chat_id=update.effective_chat.id,
            message_id=getattr(sent, "message_id", None) or q.message.message_id,
            user_id=update.effective_user.id,
            media_type=media["type"],
            tmdb_id=media["tmdb_id"],
            label=label,
            is4k=is4k,
            status=("grabbing" if created.status == REQUEST_STATUS_APPROVED
                    else "waiting"),
            timeout_hours=REQUEST_WATCH_TIMEOUT_HOURS,
        )
    except Exception:
        logger.exception("couldn't enqueue request watch (card stays static)")
    _clear_rq_state(ctx)
    return ConversationHandler.END


def _clear_rq_state(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    for key in _RQ_KEYS:
        ctx.user_data.pop(key, None)


async def _resume_submit_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                                 payload: dict) -> None:
    """Relink-resume executor. The draft is still in user_data unless the
    user started a new /request meanwhile."""
    if user_in_conversation(ctx, update, "request"):
        await update.effective_message.reply_text(
            "You've started a new /request since then, so I didn't auto-submit "
            "the interrupted one. Finish the current one instead."
        )
        return
    media = ctx.user_data.get("rq_media")
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
        seasons = sorted(ctx.user_data.get("rq_selected") or ()) or None
        if not seasons:
            await update.effective_message.reply_text(
                "The interrupted request lost its season selection. "
                "/request to start over.")
            return
    is4k = bool(ctx.user_data.get("rq_is4k"))
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
    if created.status == REQUEST_STATUS_APPROVED:
        msg = f"✅ Approved: {label} is being grabbed."
    else:
        msg = f"📨 Requested: {label}. Waiting for approval."
    await update.effective_message.reply_text(msg)
    _clear_rq_state(ctx)


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
        await q.edit_message_text("Cancelled. /request to start over.")
    else:
        await update.effective_message.reply_text("Cancelled. /request to start over.")
    _clear_rq_state(ctx)
    return ConversationHandler.END
