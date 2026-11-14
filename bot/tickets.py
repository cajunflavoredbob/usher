"""Ticket management: /tickets, the tk_* callback family, _apply_fix, and the
reply ConversationHandler.

Public entry points:
  cmd_tickets          /tickets: sends the Tickets Hub (bot.tickets_hub)
  tickets_hub_list     the Hub's list button and a detail card's "⬅️ Tickets"
  tk_open, tk_back, tk_close_menu, tk_close_direct, tk_fix,
    tk_fix_redownload, tk_fix_mark_failed                callback handlers
  tk_reply_start, tk_close_with_comment_start, tk_reply_text  reply convo
  _ticket_conversation()                                      conversation
  _run_arr_action(action="fix" | "mark_failed")               arr orchestrator
"""
from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass
from typing import Literal, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from fix_result import FixResult
from http_util import user_friendly_message
from radarr import RadarrClient
from seerr import ISSUE_STATUS_RESOLVED, PlexTokenInvalidError, SeerrClient
from sonarr import SonarrClient
from store import UserStore

from bot.callback_prefixes import (
    TICKETS_LIST,
    TK_BACK,
    TK_BACK_DM,
    TK_BACK_LIST,
    TK_CLOSE_DIRECT,
    TK_CLOSE_WITH_COMMENT,
    TK_FIX,
    TK_FIX_MARK_FAILED,
    TK_FIX_REDOWNLOAD,
    TK_OPEN,
    TK_REPLY,
)
from const import TICKET_BUTTONS_PER_ROW, TICKET_REPLY_TIMEOUT_S
from bot.hub import (back_button, came_from_hub, end_markup,
                     keyboard_callbacks, send_hub, try_again_button)
from bot.tickets_hub import TICKETS_HUB, list_back_button
from bot.shared import (
    truncate_message,
    DECRYPT_FAILED_MSG,
    AWAIT_TICKET_REPLY,
    RELINK_RESUME_EXECUTORS,
    ISSUE_TYPES,
    edit_or_send,
    format_age,
    format_media_label,
    record_btn,
    require_seerr,
    ticket_detail_kb,
    token_for,
    end_action,
    flow_step_is_stale,
    force_end_flow_conversations,
    media_action_key,
    new_ticket_dm_kb,
    prompt_plex_relink,
    send_typing,
    try_begin_action,
    user_in_conversation,
)

logger = logging.getLogger("usher")

# Cap the reply thread rendered in a ticket detail view so a long-running
# conversation can't push the message past Telegram's ~4096-char limit.
MAX_THREAD_COMMENTS = 20


async def _require_admin(
    q, ctx: ContextTypes.DEFAULT_TYPE, *, action_label: str,
) -> bool:
    """Gate an admin-only callback. Returns True if the caller is the admin;
    otherwise toasts 'Admin only.' and writes an `admin_callback_blocked`
    audit entry. Replaces silent no-op rejection so admins notice and
    non-admins know why nothing happened."""
    user = getattr(q, "from_user", None)
    user_id = getattr(user, "id", None)
    if user_id == ctx.bot_data.get("admin_id"):
        return True
    try:
        await q.answer("Admin only.", show_alert=False)
    except TelegramError as exc:
        logger.debug("callback answer failed", exc_info=exc)
    # Local import to avoid a top-level auth_util dep just for this entry.
    from auth_util import audit
    audit("admin_callback_blocked",
          user=str(user_id) if user_id is not None else "-",
          action=action_label)
    return False


# _truncate_message moved to shared.truncate_message in 0.15.0 (the
# /requests list needs the same cap).


async def cmd_tickets(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/tickets opens the Tickets Hub. It needs no gate: each button runs its
    own checks."""
    await send_hub(update, ctx, TICKETS_HUB)


async def tickets_hub_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """The Hub's list button ("My tickets" / "Open tickets") and a detail
    card's "⬅️ Tickets": edit the tapped message into the open-ticket list,
    which ends with "⬅️ Tickets" back to the Hub."""
    q = update.callback_query
    await q.answer()
    force_end_flow_conversations(ctx, update)
    back = end_markup(TICKETS_HUB)

    async def show(text, markup):
        sent = await edit_or_send(q, text, reply_markup=markup)
        record_btn(ctx.application, update.effective_user.id, sent)

    seerr = await require_seerr(update, ctx, reply_markup=back)
    if seerr is None:
        return
    await send_typing(update, ctx)  # list + title fetches can take seconds
    user_id = update.effective_user.id
    admin_id = ctx.bot_data.get("admin_id")
    is_admin = user_id == admin_id
    store: UserStore = ctx.bot_data["store"]
    link = await store.get(user_id)

    # Eligibility check for non-admin users. decrypt_failed is
    # distinguished like every sibling gate: telling that user to "/link
    # first" is the wrong remedy (they need /unlink then /link).
    if not is_admin:
        if link and link.plex_token_decrypt_failed:
            await show(DECRYPT_FAILED_MSG, back)
            return
        if not link or not link.plex_token:
            await show("DM me /link first so I know which Plex account is yours.",
                       back)
            return

    # Fetch tickets
    try:
        tickets, total = await seerr.list_issues(
            filter="open",
            as_plex_token=None if is_admin else link.plex_token,
            created_by=None if is_admin else link.seerr_id,
        )
    except PlexTokenInvalidError:
        await prompt_plex_relink(update, ctx, back=back_button(TICKETS_HUB))
        return
    except Exception as exc:
        logger.exception("list_issues failed")
        await show(f"Couldn't fetch tickets. {user_friendly_message(exc)}",
                   end_markup(TICKETS_HUB, TICKETS_LIST))
        return

    if not tickets:
        await show(
            ("No open tickets across all users. 🎉" if is_admin else "No open tickets! 🎉"),
            back)
        return

    # Resolve media titles in parallel
    title_tasks = [seerr.get_media_title(i.media_type, i.tmdb_id) for i in tickets]
    title_results = await asyncio.gather(*title_tasks, return_exceptions=True)

    # Honest truncation: the header used to claim "All open
    # tickets (25)" while tickets 26+ were silently invisible.
    scope = "All open tickets" if is_admin else "Your open tickets"
    if total > len(tickets):
        header = f"📋 {scope} (showing {len(tickets)} of {total}; manage the rest in Seerr):"
    else:
        header = f"📋 {scope} ({len(tickets)}):"
    lines = [header, ""]
    for ticket, tr in zip(tickets, title_results):
        emoji, _ = ISSUE_TYPES.get(ticket.issue_type, ("❓", "Other"))
        if isinstance(tr, Exception):
            media_label = f"TMDb {ticket.tmdb_id}"
        else:
            title, year = tr
            media_label = format_media_label(
                title, year,
                season=ticket.problem_season if ticket.media_type == "tv" else None,
                episode=ticket.problem_episode,
            )
        age = format_age(ticket.created_at)
        line = f"#{ticket.id} {emoji} {media_label} - {age}"
        if is_admin and ticket.created_by:
            line += f" - {ticket.created_by}"
        lines.append(line)
    lines.append("")
    lines.append("Tap a ticket number below to manage it.")
    text = truncate_message("\n".join(lines), html=False)

    # Inline keyboard: one button per ticket (#N)
    button_rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []
    for ticket in tickets:
        current.append(InlineKeyboardButton(f"#{ticket.id}", callback_data=f"{TK_OPEN}:{ticket.id}"))
        if len(current) == TICKET_BUTTONS_PER_ROW:
            button_rows.append(current)
            current = []
    if current:
        button_rows.append(current)
    button_rows.append([back_button(TICKETS_HUB)])
    await show(text, InlineKeyboardMarkup(button_rows))


def _opened_from_list(message) -> bool:
    """True when `message` belongs to a detail card opened from the list: the
    card or a Reply or Close result carries "⬅️ Tickets" (`TICKETS_LIST`),
    and a Close or Fix sub-menu, or a Fix that couldn't start or didn't run,
    carries a `TK_BACK_LIST` button."""
    return any(cb == TICKETS_LIST or cb.startswith(f"{TK_BACK_LIST}:")
               for cb in keyboard_callbacks(message))


def _opened_from_dm(message) -> bool:
    """True when `message` carries a "📜 History" button (`TK_OPEN`) or a
    `TK_BACK_DM` button, and is not opened from the list. Only the new-Ticket
    DM's card, its Close or Fix sub-menu, or a Fix that couldn't start or
    didn't run, reach it with a Fix or Close to act on."""
    if _opened_from_list(message):
        return False
    return any(cb.startswith(f"{TK_OPEN}:") or cb.startswith(f"{TK_BACK_DM}:")
               for cb in keyboard_callbacks(message))


def _result_markup(from_list: bool,
                   retry: Optional[str] = None) -> Optional[InlineKeyboardMarkup]:
    """Keyboard for a Reply or Close result: none unless the detail card
    was opened from the list, else "⬅️ Tickets" back to the list, with Try
    again (callback `retry`) before it when the step failed."""
    if not from_list:
        return None
    row = [list_back_button()]
    if retry is not None:
        row.insert(0, try_again_button(retry))
    return InlineKeyboardMarkup([row])


async def _edit_result(q, ctx, text: str,
                       markup: Optional[InlineKeyboardMarkup]):
    """Edit the tapped message into a result; a keyboard is recorded with the
    button gate."""
    sent = await edit_or_send(q, text, reply_markup=markup)
    if markup is not None:
        record_btn(ctx.application, q.from_user.id, sent)
    return sent


async def _reply_result(update, ctx, text: str,
                        markup: Optional[InlineKeyboardMarkup]) -> None:
    """Reply with a result; a keyboard is recorded with the button gate."""
    sent = await update.effective_message.reply_text(text, reply_markup=markup)
    if markup is not None:
        record_btn(ctx.application, update.effective_user.id, sent)


def _with_list_back(kb: Optional[InlineKeyboardMarkup], ticket_id: int,
                    *, retry: bool) -> InlineKeyboardMarkup:
    """Add the "⬅️ Tickets" (back to the list) row to a detail keyboard. A
    detail that failed to load (kb None) gets Try again beside it when
    `retry` is set, else the back button alone."""
    if kb is None:
        row = [list_back_button()]
        if retry:
            row.insert(0, try_again_button(f"{TK_OPEN}:{ticket_id}"))
        return InlineKeyboardMarkup([row])
    return InlineKeyboardMarkup(
        [list(r) for r in kb.inline_keyboard] + [[list_back_button()]])


async def tk_open(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Tapped [#N] in the ticket list, or [📜 History] in a ticket DM. Shows
    the ticket's context (type, media, S/E, reporter, age) so the admin/user
    knows what they're acting on. From the list (the tapped message carries
    the Hub's back button, or the list-back button of a detail that failed to
    load) the detail replaces the tapped message and ends with "⬅️ Tickets"
    back to the list; from a DM it is a new message without that row."""
    q = update.callback_query
    await q.answer()
    try:
        ticket_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        logger.warning("Unparseable callback data: %r", q.data)
        return
    tg_id = update.effective_user.id
    is_admin, token, decrypt_failed = await token_for(ctx, tg_id)
    if not is_admin and decrypt_failed:
        await q.message.reply_text(
            DECRYPT_FAILED_MSG
        )
        return
    if not is_admin and token is None:
        await q.message.reply_text("DM me /link first so I can act on tickets as you.")
        return

    from_list = (came_from_hub(q.message, TICKETS_HUB)
                 or TICKETS_LIST in keyboard_callbacks(q.message))
    try:
        text, kb = await _build_ticket_detail(ctx, ticket_id, is_admin, token)
    except PlexTokenInvalidError:
        await prompt_plex_relink(
            update, ctx, back=list_back_button() if from_list else None)
        return
    if from_list:
        msg = await edit_or_send(
            q, text, parse_mode="HTML",
            reply_markup=_with_list_back(kb, ticket_id, retry=True))
        record_btn(ctx.application, tg_id, msg)
        return
    msg = await q.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=kb,
    )
    record_btn(ctx.application, tg_id, msg)


async def _build_ticket_detail(
    ctx: ContextTypes.DEFAULT_TYPE,
    ticket_id: int,
    is_admin: bool,
    token: Optional[str],
) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """Render the ticket detail message text + keyboard. Shared between tk_open
    (sending a new message) and tk_back (editing an existing one). Keyboard is
    None when the ticket couldn't be loaded (no actions over unknown state)."""
    seerr: SeerrClient = ctx.bot_data["seerr"]
    media_label = ""
    type_emoji = "📝"
    type_name = "Issue"
    reporter = ""
    age = ""
    description = ""
    comments: list = []
    resolved = False
    try:
        ticket = await seerr.get_issue(ticket_id, as_plex_token=None if is_admin else token)
        resolved = ticket.status == ISSUE_STATUS_RESOLVED
        type_emoji, type_name = ISSUE_TYPES.get(ticket.issue_type, ("❓", "Other"))
        reporter = ticket.created_by or ""
        age = format_age(ticket.created_at) if ticket.created_at else ""
        description = (ticket.description or "").strip()
        comments = ticket.comments or []
        if ticket.media_type in ("movie", "tv") and ticket.tmdb_id:
            try:
                title, year = await seerr.get_media_title(ticket.media_type, ticket.tmdb_id)
                m_emoji = "🎬" if ticket.media_type == "movie" else "📺"
                bare = format_media_label(
                    title, year,
                    season=ticket.problem_season if ticket.media_type == "tv" else None,
                    episode=ticket.problem_episode,
                )
                media_label = f"{m_emoji} {bare}"
            except Exception as exc:
                logger.exception("get_media_title failed for #%d", ticket_id, exc_info=exc)
    except PlexTokenInvalidError:
        raise  # callers show the re-link prompt; a degraded view can't help
    except Exception:
        # Honest failure state: the old path rendered a
        # healthy-looking detail with live Fix/Close buttons over data it
        # never loaded.
        logger.exception("get_issue failed for #%d", ticket_id)
        return (f"<b>Ticket #{ticket_id}</b>\n"
                "Couldn't load the ticket details right now. "
                "Try again in a moment.", None)
    lines = [f"<b>Ticket #{ticket_id}</b>"]
    if media_label:
        lines.append(html.escape(media_label))
    lines.append(f"<b>Issue type:</b> {type_emoji} {type_name}")
    lines.append(f"<b>Status:</b> {'Resolved' if resolved else 'Open'}")
    if reporter:
        lines.append(f"<b>Reported by:</b> {html.escape(reporter)}")
    if age:
        lines.append(f"<b>Age:</b> {age}")
    if description:
        lines.append("")
        lines.append("<b>Description:</b>")
        lines.append(f"<i>\"{html.escape(description)}\"</i>")
    if comments:
        lines.append("")
        shown = comments[-MAX_THREAD_COMMENTS:]
        if len(comments) > MAX_THREAD_COMMENTS:
            lines.append(f"<b>Replies</b> (last {MAX_THREAD_COMMENTS} of {len(comments)}):")
        else:
            lines.append("<b>Replies:</b>")
        for c in shown:
            age_c = format_age(c.created_at) if c.created_at else ""
            head = f"<b>{html.escape(c.author or '?')}</b>"
            if age_c:
                head += f" · {age_c}"
            lines.append(f"{head}: <i>\"{html.escape(c.message)}\"</i>")
    return truncate_message("\n".join(lines)), ticket_detail_kb(ticket_id, is_admin, resolved)


async def tk_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Back from a sub-menu -- edit the message back to the ticket detail view.
    `tkbl:<id>` (a sub-menu of a detail opened from the list) keeps the
    detail's "⬅️ Tickets" row; `tkbd:<id>` (a sub-menu of a new-Ticket DM)
    brings back the DM's buttons, History included; `tkback:<id>` does
    neither."""
    q = update.callback_query
    await q.answer()
    from_list = q.data.startswith(f"{TK_BACK_LIST}:")
    from_dm = q.data.startswith(f"{TK_BACK_DM}:")
    try:
        ticket_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        logger.warning("Unparseable callback data: %r", q.data)
        return
    tg_id = update.effective_user.id
    is_admin, token, decrypt_failed = await token_for(ctx, tg_id)
    # Same gate as tk_open: a non-admin without a usable token must not fall
    # through to _build_ticket_detail's admin-key fetch.
    if not is_admin and decrypt_failed:
        await q.message.reply_text(
            DECRYPT_FAILED_MSG
        )
        return
    if not is_admin and token is None:
        await q.message.reply_text("DM me /link first so I can act on tickets as you.")
        return
    try:
        text, kb = await _build_ticket_detail(ctx, ticket_id, is_admin, token)
    except PlexTokenInvalidError:
        await prompt_plex_relink(
            update, ctx, back=list_back_button() if from_list else None)
        return
    if from_list:
        kb = _with_list_back(kb, ticket_id, retry=False)
    elif from_dm:
        kb = (new_ticket_dm_kb(ticket_id, _kb_is_resolved(kb)) if kb is not None and is_admin
              else kb or InlineKeyboardMarkup([[_ticket_back_button(ticket_id, False, True)]]))
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        logger.exception("tk_back edit failed for #%d", ticket_id)
        return
    # The same message_id is the active one; refresh sent_at so the 6h timer resets.
    record_btn(ctx.application, tg_id, q.message)


def _back_prefix(q) -> str:
    """The sub-menu Back callback: keep the list-back row when the detail card
    it was opened from carries it, and the new-Ticket DM's History row when
    the card is that DM."""
    if _opened_from_dm(q.message):
        return TK_BACK_DM
    return TK_BACK_LIST if TICKETS_LIST in keyboard_callbacks(q.message) else TK_BACK


async def tk_close_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin tapped [Close] -- show the with/without comment options."""
    q = update.callback_query
    await q.answer()
    try:
        ticket_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        logger.warning("Unparseable callback data: %r", q.data)
        return
    if not await _require_admin(q, ctx, action_label="tk_close_menu"):
        return
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💬 Comment", callback_data=f"{TK_CLOSE_WITH_COMMENT}:{ticket_id}"),
            InlineKeyboardButton("✓ No comment", callback_data=f"{TK_CLOSE_DIRECT}:{ticket_id}"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"{_back_prefix(q)}:{ticket_id}")],
    ])
    await q.edit_message_text(f"Close ticket #{ticket_id}?", reply_markup=kb)
    record_btn(ctx.application, update.effective_user.id, q.message)


async def tk_fix(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin tapped [Fix]. Opens the [Redownload] [Mark Failed] [Back] submenu."""
    q = update.callback_query
    await q.answer()
    try:
        ticket_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        logger.warning("Unparseable callback data: %r", q.data)
        return
    if not await _require_admin(q, ctx, action_label="tk_fix"):
        return
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Redownload", callback_data=f"{TK_FIX_REDOWNLOAD}:{ticket_id}"),
            InlineKeyboardButton("🚫 Mark Failed", callback_data=f"{TK_FIX_MARK_FAILED}:{ticket_id}"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"{_back_prefix(q)}:{ticket_id}")],
    ])
    await q.edit_message_text(
        f"🔧 Fix #{ticket_id} - how?\n"
        "⚠️ Both options delete the current file from disk before searching "
        "for a replacement.",
        reply_markup=kb,
    )
    record_btn(ctx.application, update.effective_user.id, q.message)


async def tk_fix_redownload(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete current file + trigger search."""
    await _apply_fix(update, ctx, strategy="redownload")


async def tk_fix_mark_failed(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark the grab that produced the file as failed → Radarr/Sonarr blocklists + re-searches."""
    await _apply_fix(update, ctx, strategy="mark_failed")


@dataclass
class _FixContext:
    """Resolved context for an admin Fix / Mark Failed action.

    Carries everything _apply_fix needs to dispatch + render: the ticket id
    (for messages + audit-log + poll-tracking row), the media dict the arr
    clients expect, the season/episode (for label + workflow), and the
    pre-built label for the success/failure DM."""
    ticket_id: int
    media: dict
    season: Optional[int]
    episode: Optional[int]
    label: str


# Start of the _resolve_fix_context error that a retry can fix.
_FETCH_FAILED = "Couldn't fetch ticket"


async def _resolve_fix_context(
    seerr: SeerrClient, ticket_id: int, *, action_name: str,
) -> tuple[Optional[_FixContext], Optional[str]]:
    """Fetch the ticket + media title + tvdb_id and return a _FixContext.
    On any unrecoverable failure returns (None, user_facing_error_string).
    Best-effort lookups (title, tvdb_id) don't fail the whole flow."""
    try:
        ticket = await seerr.get_issue(ticket_id)
    except Exception as exc:
        logger.exception("get_issue failed for #%d", ticket_id)
        return None, f"{_FETCH_FAILED} #{ticket_id}. {user_friendly_message(exc)}"

    if ticket.status == ISSUE_STATUS_RESOLVED:
        return None, f"Ticket #{ticket_id} is already resolved, so there is nothing to fix."

    media_type = ticket.media_type
    tmdb_id = ticket.tmdb_id
    season = ticket.problem_season
    episode = ticket.problem_episode

    # season may be None on garbage/fork payloads even when episode is set;
    # letting that through would query Sonarr with an empty seasonNumber
    # filter (= every episode in the series) ahead of a delete workflow.
    if media_type == "tv" and (season is None or not episode):
        return None, (
            f"{action_name} only works on individual episodes or movies, not "
            f"whole seasons or shows. For #{ticket_id}, fix it in Sonarr directly."
        )

    media: dict = {"type": media_type, "tmdb_id": tmdb_id}
    label_title = ""
    label_year = ""
    try:
        label_title, label_year = await seerr.get_media_title(media_type, tmdb_id)
    except Exception as exc:
        logger.exception("get_media_title failed for #%d", ticket_id, exc_info=exc)
    if media_type == "tv":
        try:
            _seasons, tvdb_id = await seerr.get_tv_seasons(tmdb_id)
            media["tvdb_id"] = tvdb_id
        except Exception as exc:
            logger.exception("get_tv_seasons failed for #%d", ticket_id, exc_info=exc)

    label = format_media_label(
        label_title, label_year,
        season=season if media_type == "tv" else None,
        episode=episode,
    )
    return _FixContext(ticket_id=ticket_id, media=media, season=season,
                       episode=episode, label=label), None


async def _enqueue_fix_completion(
    store: UserStore, *, fix: _FixContext, result: FixResult,
    chat_id: int, user_id: int, ticket_url: str, action_name: str = "",
    event_id: Optional[int] = None, auto_blocklisted: bool = False,
) -> int:
    """Build the pending_autofix row from result.poll_info; returns the row
    id so the caller can attach the Fix's Progress card to it. Raises on
    enqueue failure -- caller decides what to tell the user."""
    poll_info = result.poll_info or {}
    kwargs: dict = {
        "chat_id": chat_id,
        "user_id": user_id,
        "media_type": fix.media["type"],
        "label": fix.label or f"#{fix.ticket_id}",
        "ticket_id": fix.ticket_id,
        "ticket_url": ticket_url,
        "action_name": action_name,
        "before_fingerprint": result.before,
        "event_id": event_id,
        "auto_blocklisted": auto_blocklisted,
    }
    if fix.media["type"] == "movie":
        kwargs["radarr_movie_id"] = poll_info.get("movie_id")
    else:
        # poll_info only ever carries series_id/episode_id (the
        # whole-season workflow died in 0.12.0); the season /
        # expected-episodes kwargs were always None and are gone.
        kwargs["sonarr_series_id"] = poll_info.get("series_id")
        kwargs["sonarr_episode_id"] = poll_info.get("episode_id")
    return await store.add_pending_autofix(**kwargs)


def _ticket_back_button(ticket_id: int, from_list: bool,
                        from_dm: bool = False) -> InlineKeyboardButton:
    """"⬅️ Ticket": shows the Ticket again with its buttons (through
    `tk_back`), keeping the "⬅️ Tickets" row when it was opened from the list
    and the History row when it is a new-Ticket DM."""
    prefix = TK_BACK_LIST if from_list else TK_BACK_DM if from_dm else TK_BACK
    return InlineKeyboardButton("⬅️ Ticket", callback_data=f"{prefix}:{ticket_id}")


def _fix_end_markup(ticket_id: int, from_list: bool,
                    retry: Optional[str] = None,
                    from_dm: bool = False) -> InlineKeyboardMarkup:
    """Keyboard for a Fix that couldn't start or didn't run: "⬅️ Ticket",
    with Try again (callback `retry`) before it when the card was opened from
    the list and the step failed."""
    row = [_ticket_back_button(ticket_id, from_list, from_dm)]
    if from_list and retry is not None:
        row.insert(0, try_again_button(retry))
    return InlineKeyboardMarkup([row])


def _kb_is_resolved(kb: InlineKeyboardMarkup) -> bool:
    """True when a detail keyboard carries no Fix button (an Admin's resolved Ticket)."""
    return not any((b.callback_data or "").startswith(f"{TK_FIX}:")
                   for row in kb.inline_keyboard for b in row)


async def _restore_ticket_card(q, ctx, ticket_id: int, from_list: bool,
                               from_dm: bool = False) -> None:
    """Edit the Ticket card back into the Ticket with its buttons, the same
    view its Back shows (plus "⬅️ Tickets" when opened from the list, or
    "📜 History" when it is a new-Ticket DM)."""
    try:
        text, kb = await _build_ticket_detail(ctx, ticket_id, True, None)
    except Exception:
        logger.exception("couldn't rebuild ticket #%d after a fix started", ticket_id)
        text, kb = f"<b>Ticket #{ticket_id}</b>", None
    if from_list:
        kb = _with_list_back(kb, ticket_id, retry=False)
    elif kb is None:
        kb = InlineKeyboardMarkup([[_ticket_back_button(ticket_id, False, from_dm)]])
    elif from_dm:
        kb = new_ticket_dm_kb(ticket_id, _kb_is_resolved(kb))
    try:
        sent = await edit_or_send(q, text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        logger.exception("couldn't restore ticket card #%d", ticket_id)
        return
    record_btn(ctx.application, q.from_user.id, sent)


class _ProgressCard:
    """The Fix's Progress card: a new message in the chat that shows the
    working state, then the result. If the first send fails, the next show()
    sends a new message, which becomes the card."""

    def __init__(self, ctx, chat_id: int):
        self._ctx = ctx
        self._chat_id = chat_id
        self.message_id: Optional[int] = None

    async def show(self, text: str) -> None:
        bot = self._ctx.bot
        try:
            if self.message_id is None:
                sent = await bot.send_message(chat_id=self._chat_id, text=text)
                self.message_id = sent.message_id
            else:
                await bot.edit_message_text(chat_id=self._chat_id,
                                            message_id=self.message_id, text=text)
        except Exception as exc:
            logger.exception("couldn't show the fix progress card", exc_info=exc)

    async def delete(self) -> None:
        if self.message_id is None:
            return
        try:
            await self._ctx.bot.delete_message(chat_id=self._chat_id,
                                               message_id=self.message_id)
        except Exception as exc:
            logger.exception("couldn't delete the fix progress card", exc_info=exc)
        self.message_id = None


async def _apply_fix(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, strategy: str) -> None:
    """Shared admin-fix path. strategy is 'redownload' or 'mark_failed'.

    Once the Fix starts (the ticket resolved and no other fix runs on the
    title), the Ticket card shows the Ticket again with its buttons and the
    working state, result and download progress go on a new Progress card.
    A Fix that can't start leaves its reason and "⬅️ Ticket" on the Ticket
    card; one that starts but whose Arr call didn't run does the same and
    deletes the Progress card."""
    q = update.callback_query
    await q.answer()
    try:
        ticket_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        logger.warning("Unparseable callback data: %r", q.data)
        return
    if not await _require_admin(q, ctx, action_label=f"_apply_fix:{strategy}"):
        return
    # Read before the first edit below strips the keyboard.
    from_list = _opened_from_list(q.message)
    from_dm = _opened_from_dm(q.message)
    retry = q.data
    seerr: SeerrClient = ctx.bot_data["seerr"]
    radarr: Optional[RadarrClient] = ctx.bot_data.get("radarr")
    sonarr: Optional[SonarrClient] = ctx.bot_data.get("sonarr")
    store: UserStore = ctx.bot_data["store"]
    action_name = "Redownload" if strategy == "redownload" else "Mark Failed"

    # Edit BEFORE any network call: strips the Fix choices so they can't be
    # tapped again while the Seerr calls run.
    await edit_or_send(q, f"⏳ Starting {action_name} for #{ticket_id}...")

    fix, err = await _resolve_fix_context(seerr, ticket_id, action_name=action_name)
    if err is not None:
        fetch_failed = err.startswith(_FETCH_FAILED)
        await _edit_result(q, ctx, err, _fix_end_markup(
            ticket_id, from_list, retry if fetch_failed else None, from_dm))
        return
    assert fix is not None

    # Per-media serialization: a second trigger for the same
    # title - a racing double-tap that beat the edit above, or a user autofix
    # running concurrently - must not race delete/blocklist/search.
    media_key = media_action_key(fix.media)
    if not try_begin_action(ctx, media_key):
        await _edit_result(q, ctx,
            f"⏳ Another fix for {fix.label or f'#{ticket_id}'} is already "
            "running. Wait for its result before retrying.",
            _fix_end_markup(ticket_id, from_list, from_dm=from_dm),
        )
        return
    try:
        # The Fix has started: its progress goes on a new Progress card and
        # the Ticket card shows the Ticket again.
        card = _ProgressCard(ctx, q.message.chat_id)
        await card.show(f"⏳ {action_name} for #{ticket_id} - working...")
        await _restore_ticket_card(q, ctx, ticket_id, from_list, from_dm)

        action: FixAction = "fix" if strategy == "redownload" else "mark_failed"
        result = await _run_arr_action(
            fix.media, fix.season, fix.episode, radarr, sonarr, action=action,
        )

        if result.status == "failed":
            await card.delete()
            await _edit_result(
                q, ctx, f"⚠️ {action_name} for #{ticket_id} didn't run: {result.message}",
                _fix_end_markup(ticket_id, from_list, retry, from_dm))
            return

        # ok or partial: always log the autofix event (mirrors _submit_ticket,
        # which logs even when no search ran); enqueue the completion poller
        # iff search ran.
        event_id = await store.log_autofix(
            update.effective_user.id, fix.media["type"], fix.media["tmdb_id"],
            season=fix.season, episode=fix.episode,
            kind="redownload" if strategy == "redownload" else "mark failed")
        pending_id = None
        if result.should_poll:
            try:
                pending_id = await _enqueue_fix_completion(
                    store, fix=fix, result=result,
                    chat_id=q.message.chat_id,
                    user_id=update.effective_user.id,
                    ticket_url=f"{seerr.public_url}/issues/{ticket_id}",
                    action_name=action_name, event_id=event_id,
                )
            except Exception:
                logger.exception("failed to enqueue pending autofix for #%d", ticket_id)
                prefix = "🔧" if result.ok else "⚠️"
                await card.show(
                    f"{prefix} {action_name} for #{ticket_id} ({result.message}), "
                    "but couldn't enqueue completion notification.")
                return

        prefix = "🔧" if result.ok else "⚠️"
        tail = "\n\n🔔 I'll DM when the new file finishes downloading." if result.should_poll else ""
        await card.show(
            f"{prefix} {action_name} for #{ticket_id}.\n{fix.label}\n\n{result.message}{tail}")
        # The poller paints download progress onto the Progress card. It has
        # no keyboard: the Ticket card keeps its buttons, and its "⬅️ Tickets"
        # row when it was opened from the list.
        if pending_id and card.message_id is not None:
            try:
                await store.set_autofix_message(pending_id, card.message_id,
                                                reply_markup="")
            except Exception as exc:
                logger.exception("couldn't attach autofix card for #%d", ticket_id, exc_info=exc)
        return
    finally:
        end_action(ctx, media_key)


async def tk_close_direct(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin tapped [Without comment]. Resolve immediately."""
    q = update.callback_query
    await q.answer()
    try:
        ticket_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        logger.warning("Unparseable callback data: %r", q.data)
        return
    if not await _require_admin(q, ctx, action_label="tk_close_direct"):
        return
    from_list = _opened_from_list(q.message)
    seerr: SeerrClient = ctx.bot_data["seerr"]
    # Double-tap guard: strip the submenu before the network
    # call, and drop a racing second tap that got in before the edit (it
    # would resolve an already-resolved ticket and show a contradictory
    # "Closed" + "Couldn't close" pair on screen).
    close_key = f"close:{ticket_id}"
    if not try_begin_action(ctx, close_key):
        return
    try:
        await edit_or_send(q, f"⏳ Closing #{ticket_id}...")
        try:
            await seerr.resolve_issue(ticket_id, as_plex_token=None)
        except Exception as exc:
            logger.exception("resolve_issue failed for #%d", ticket_id)
            await _edit_result(
                q, ctx, f"Couldn't close #{ticket_id}. {user_friendly_message(exc)}",
                _result_markup(from_list, q.data))
            return
        await _edit_result(q, ctx, f"✅ Closed ticket #{ticket_id}.",
                           _result_markup(from_list))
    finally:
        end_action(ctx, close_key)


# Ticket reply conversation

async def tk_reply_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for `tkr:<id>` -- reply only (no close)."""
    q = update.callback_query
    await q.answer()
    try:
        ticket_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        logger.warning("Unparseable callback data: %r", q.data)
        return ConversationHandler.END
    # Double-tap guard: two concurrent taps would each send a prompt and
    # set the conversation state twice. The slot covers only this start
    # handler (checks, button strip, prompt), not the whole conversation.
    # Reply and Close-with-comment share this key on purpose, so a tap on
    # the sibling button cannot overwrite tk_close_after mid-start.
    start_key = f"tkstart:{ticket_id}"
    if not try_begin_action(ctx, start_key):
        return ConversationHandler.END
    try:
        is_admin, token, decrypt_failed = await token_for(ctx, update.effective_user.id)
        if not is_admin and decrypt_failed:
            await q.message.reply_text(
                DECRYPT_FAILED_MSG
            )
            return ConversationHandler.END
        if not is_admin and token is None:
            await q.message.reply_text("DM me /link first so I can post comments as you.")
            return ConversationHandler.END
        # Same text-capture guard as the resolve comment prompt:
        # an active /issue conversation would swallow the reply as its next input.
        if user_in_conversation(ctx, update, "issue", "resolve", "request", "invite"):
            await q.message.reply_text(
                "You're in the middle of another flow (like /issue or /request). Finish or "
                "/cancel it first, then tap Reply again."
            )
            return ConversationHandler.END
        if flow_step_is_stale(ctx):
            return ConversationHandler.END  # a newer command ended this flow
        ctx.user_data["tk_reply_id"] = ticket_id
        ctx.user_data["tk_close_after"] = False
        ctx.user_data["tk_reply_from_list"] = _opened_from_list(q.message)
        # Strip the inline buttons but keep the original ticket-announcement text,
        # then prompt for the reply text in a separate message.
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception as exc:
            logger.debug("Couldn't clear buttons on ticket #%d message: %s", ticket_id, exc)
        await q.message.reply_text(
            f"Send the reply text for ticket #{ticket_id} (or /cancel)."
        )
        return AWAIT_TICKET_REPLY
    finally:
        end_action(ctx, start_key)


async def tk_close_with_comment_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for `tkcc:<id>` -- post comment then close (admin only)."""
    q = update.callback_query
    await q.answer()
    try:
        ticket_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        logger.warning("Unparseable callback data: %r", q.data)
        return ConversationHandler.END
    # Double-tap guard: two concurrent taps would each send a prompt and
    # set the conversation state twice. The slot covers only this start
    # handler (checks, button strip, prompt), not the whole conversation.
    # Reply and Close-with-comment share this key on purpose, so a tap on
    # the sibling button cannot overwrite tk_close_after mid-start.
    start_key = f"tkstart:{ticket_id}"
    if not try_begin_action(ctx, start_key):
        return ConversationHandler.END
    try:
        if not await _require_admin(q, ctx, action_label="tk_close_with_comment_start"):
            return ConversationHandler.END
        # Same text-capture guard as tk_reply_start.
        if user_in_conversation(ctx, update, "issue", "resolve", "request", "invite"):
            await q.message.reply_text(
                "You're in the middle of another flow (like /issue or /request). Finish or "
                "/cancel it first, then tap the button again."
            )
            return ConversationHandler.END
        if flow_step_is_stale(ctx):
            return ConversationHandler.END  # a newer command ended this flow
        ctx.user_data["tk_reply_id"] = ticket_id
        ctx.user_data["tk_close_after"] = True
        ctx.user_data["tk_reply_from_list"] = _opened_from_list(q.message)
        # Strip the inline buttons but keep the original ticket-announcement text,
        # then prompt for the closing comment in a separate message.
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception as exc:
            logger.debug("Couldn't clear buttons on ticket #%d message: %s", ticket_id, exc)
        await q.message.reply_text(
            f"Send the closing comment for ticket #{ticket_id} (or /cancel)."
        )
        return AWAIT_TICKET_REPLY
    finally:
        end_action(ctx, start_key)


def _clear_reply_state(ctx) -> None:
    for key in ("tk_reply_id", "tk_close_after", "tk_reply_from_list"):
        ctx.user_data.pop(key, None)


def _reply_retry(ctx, ticket_id: int) -> str:
    """Try again for a reply that never posted: start the same reply again."""
    prefix = (TK_CLOSE_WITH_COMMENT if ctx.user_data.get("tk_close_after")
              else TK_REPLY)
    return f"{prefix}:{ticket_id}"


async def tk_reply_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the reply text, post it, optionally close."""
    ticket_id = ctx.user_data.get("tk_reply_id")
    close_after = bool(ctx.user_data.get("tk_close_after"))
    from_list = bool(ctx.user_data.get("tk_reply_from_list"))
    if not ticket_id:
        return ConversationHandler.END
    text = (update.effective_message.text or "").strip()
    if not text:
        await update.effective_message.reply_text("Empty message. Send a few words or /cancel.")
        return AWAIT_TICKET_REPLY
    is_admin, token, decrypt_failed = await token_for(ctx, update.effective_user.id)
    if not is_admin and decrypt_failed:
        await _reply_result(update, ctx, DECRYPT_FAILED_MSG,
                            _result_markup(from_list))
        return ConversationHandler.END
    if not is_admin and token is None:
        await _reply_result(update, ctx,
                            "Your /link is gone or incomplete. /link to re-link.",
                            _result_markup(from_list))
        _clear_reply_state(ctx)
        return ConversationHandler.END
    seerr: SeerrClient = ctx.bot_data["seerr"]
    try:
        await seerr.add_issue_comment(ticket_id, text, as_plex_token=token)
    except PlexTokenInvalidError:
        # Carry the typed reply for the post-relink resume. close_after isn't
        # carried: close-with-comment is admin-only, and the admin (admin-key
        # attribution) can never hit this gate.
        await prompt_plex_relink(update, ctx, resume_kind="ticket_reply",
                                 resume_payload={"ticket_id": ticket_id,
                                                 "text": text},
                                 back=list_back_button() if from_list else None)
        _clear_reply_state(ctx)
        return ConversationHandler.END
    except Exception as exc:
        logger.exception("add_issue_comment failed for #%d", ticket_id)
        await _reply_result(
            update, ctx,
            f"Couldn't post comment on #{ticket_id}. {user_friendly_message(exc)}",
            _result_markup(from_list, _reply_retry(ctx, ticket_id)))
        _clear_reply_state(ctx)
        return ConversationHandler.END
    # If the user started a new reply flow for a different ticket during the
    # add_issue_comment await, our comment still landed on the right ticket
    # (we bound ticket_id at entry) but we mustn't apply the close-after side
    # effect to whatever flow they're now on.
    if ctx.user_data.get("tk_reply_id") != ticket_id:
        await update.effective_message.reply_text(
            f"💬 Reply posted on #{ticket_id}. "
            "(You've started a new reply since then. That one's still active.)"
        )
        # Return None, not END: the shared (chat,user) conversation now belongs
        # to the NEW reply flow; ending it here would kill the flow the user
        # just started and silently swallow their next message.
        return None
    if close_after:
        try:
            await seerr.resolve_issue(ticket_id, as_plex_token=None)
        except Exception as exc:
            logger.exception("resolve_issue failed for #%d", ticket_id)
            await _reply_result(
                update, ctx,
                f"💬 Comment posted on #{ticket_id}, but couldn't close. {user_friendly_message(exc)}",
                _result_markup(from_list, f"{TK_CLOSE_DIRECT}:{ticket_id}"))
            _clear_reply_state(ctx)
            return ConversationHandler.END
        await _reply_result(update, ctx,
                            f"💬 Replied and ✅ closed ticket #{ticket_id}.",
                            _result_markup(from_list))
    else:
        await _reply_result(update, ctx, f"💬 Replied to ticket #{ticket_id}.",
                            _result_markup(from_list))
    _clear_reply_state(ctx)
    return ConversationHandler.END


async def _resume_ticket_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                               payload: dict) -> None:
    """Relink-resume executor: post the ticket reply the revoked token
    blocked, with the text the user had already typed."""
    ticket_id = payload["ticket_id"]
    text = payload["text"]
    is_admin, token, _ = await token_for(ctx, update.effective_user.id)
    if not is_admin and token is None:
        return  # can't happen right after a successful link; guard anyway
    seerr: SeerrClient = ctx.bot_data["seerr"]
    try:
        await seerr.add_issue_comment(ticket_id, text, as_plex_token=token)
    except Exception as exc:
        logger.exception("resumed add_issue_comment failed for #%d", ticket_id)
        await update.effective_message.reply_text(
            f"Couldn't post comment on #{ticket_id}. {user_friendly_message(exc)}")
        return
    await update.effective_message.reply_text(f"💬 Replied to ticket #{ticket_id}.")


RELINK_RESUME_EXECUTORS["ticket_reply"] = _resume_ticket_reply


async def tk_reply_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    markup = _result_markup(bool(ctx.user_data.get("tk_reply_from_list")))
    _clear_reply_state(ctx)
    await _reply_result(update, ctx, "Cancelled.", markup)
    return ConversationHandler.END


async def _tk_reply_timeout(update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Conversation_timeout handler. Clear ticket-reply state so an abandoned
    conversation doesn't leak user_data for the life of the process, and tell
    the user (the flow explicitly solicited text, so silence loses their
    later reply with no explanation)."""
    ticket_id = ctx.user_data.get("tk_reply_id")
    markup = _result_markup(
        bool(ctx.user_data.get("tk_reply_from_list")) and ticket_id is not None,
        _reply_retry(ctx, ticket_id))
    _clear_reply_state(ctx)
    try:
        if update is not None and update.effective_chat is not None:
            sent = await ctx.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⏱️ Timed out waiting for your reply. Tap Reply on the "
                     "ticket again from /tickets.",
                reply_markup=markup,
            )
            if markup is not None and update.effective_user is not None:
                record_btn(ctx.application, update.effective_user.id, sent)
    except Exception as exc:
        logger.debug("tk-reply-timeout notice failed (non-fatal)", exc_info=exc)
    return ConversationHandler.END


async def _tk_reply_expect_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "I can only read text here: type your reply, or /cancel to stop.")
    return AWAIT_TICKET_REPLY


def _ticket_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(tk_reply_start, pattern=fr"^{TK_REPLY}:\d+$"),
            CallbackQueryHandler(tk_close_with_comment_start, pattern=fr"^{TK_CLOSE_WITH_COMMENT}:\d+$"),
        ],
        states={
            AWAIT_TICKET_REPLY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, tk_reply_text),
                MessageHandler(~filters.COMMAND, _tk_reply_expect_text),
            ],
            # TypeHandler, not MessageHandler(filters.ALL): message filters
            # reject callback-query updates, so the timeout never fired when
            # the flow's last update was a button tap.
            ConversationHandler.TIMEOUT: [
                TypeHandler(Update, _tk_reply_timeout),
            ],
        },
        fallbacks=[CommandHandler("cancel", tk_reply_cancel)],
        name="ticket_reply",
        persistent=False,
        allow_reentry=True,
        conversation_timeout=TICKET_REPLY_TIMEOUT_S,  # idle clears tk_reply_id / tk_close_after
    )

FixAction = Literal["fix", "mark_failed"]


async def _run_arr_action(
    media: dict,
    season: Optional[int],
    episode: Optional[int],
    radarr: Optional[RadarrClient],
    sonarr: Optional[SonarrClient],
    *,
    action: FixAction,
) -> FixResult:
    """Run the configured Arr action against the media. `action="fix"` is the
    plain delete+search (Auto-fix); `action="mark_failed"` adds the blocklist
    step (Mark Failed). Returns FixResult — see fix_result.py for the
    ok/partial/failed status semantics and should_poll heuristic."""
    op_label = "Auto-fix" if action == "fix" else "Mark Failed"
    try:
        if media["type"] == "movie":
            if not radarr:
                return FixResult.failed("Radarr not configured.")
            if action == "fix":
                return await radarr.auto_fix(media["tmdb_id"])
            return await radarr.mark_failed(media["tmdb_id"])
        if media["type"] == "tv":
            if not sonarr:
                return FixResult.failed("Sonarr not configured.")
            if not episode:
                # Whole-season / whole-show variants are too destructive.
                return FixResult.failed(
                    f"{op_label} only works on individual episodes, not whole seasons."
                )
            tvdb_id = media.get("tvdb_id")
            if not tvdb_id:
                return FixResult.failed("Couldn't find TVDb ID for this show.")
            if action == "fix":
                return await sonarr.auto_fix_episode(tvdb_id, season, episode)
            return await sonarr.mark_failed_episode(tvdb_id, season, episode)
    except Exception as exc:
        logger.exception("%s failed", op_label)
        return FixResult.failed(user_friendly_message(exc))
    return FixResult.failed("Unknown media type.")


