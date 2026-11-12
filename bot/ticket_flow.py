"""/issue (new Ticket) conversation: search, pick media, pick season/episode, pick type,
take description, optional auto-fix offer, submit to Seerr. Entered by typed
/issue or by the Tickets Hub's "Report a problem" button; a report started from
the Hub ends with "⬅️ Tickets" (plus Try again on errors), except when a
revoked Plex token shows the relink prompt instead."""
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
from radarr import RadarrClient
from seerr import CreatedIssue, PlexTokenInvalidError, SeerrClient
from settings import SettingsStore
from sonarr import SonarrClient
from store import UserStore

from bot.callback_prefixes import (
    TICKET_AUTOFIX_CONFIRM,
    TICKET_AUTOFIX_OFFER,
    TICKET_CANCEL,
    TICKET_EPISODE,
    TICKET_EPISODE_PAGE,
    TICKET_MEDIA,
    TICKET_RESEARCH_PARENT,
    TICKET_SEASON,
    ISSUE_TYPE,
    RQ_FROM_TICKET,
    TICKETS_REPORT,
)
from bot.hub import (back_button, end_flow, end_markup, hub_origin,
                     reply_end, set_hub_origin)
from bot.tickets_hub import TICKETS_HUB
from bot.shared import (
    DECRYPT_FAILED_MSG,
    KEYCAP_DIGITS,
    AUTOFIX_ELIGIBLE_TYPES,
    CONFIRM_AUTOFIX,
    DESCRIPTION,
    ISSUE_TYPES,
    OFFER_AUTOFIX,
    PICK_EPISODE,
    PICK_MEDIA,
    PICK_SEASON,
    PICK_TYPE,
    TITLE,
    RELINK_RESUME_EXECUTORS,
    format_media_label,
    require_seerr,
    edit_or_send,
    end_action,
    force_end_flow_conversations,
    send_typing,
    media_action_key,
    prompt_plex_relink,
    record_btn,
    try_begin_action,
    user_in_conversation,
)
from bot.tickets import _run_arr_action
from const import (
    AUTOFIX_TIMEOUT_HOURS,
    EPISODE_BUTTONS_PER_ROW,
    EPISODE_PICKER_PAGE_SIZE,
    TICKET_FLOW_TIMEOUT_S,
    KB_BUTTONS_PER_ROW,
    SEARCH_RESULT_LIMIT,
    SEASON_BUTTONS_PER_ROW,
    TYPE_BUTTONS_PER_ROW,
)

logger = logging.getLogger("usher")

# New Ticket conversation


def _tickets_origin(ctx) -> Optional[str]:
    """The Tickets Hub when this report was started from it, else None.
    Another Hub's mark (left by a flow such as the relink) never counts."""
    return TICKETS_HUB if hub_origin(ctx) == TICKETS_HUB else None


# Keys the /issue flow owns. Never clear() user_data: the request, invite and
# discover version counters and relink_resume must survive, or a restarted
# counter lets an older confirm card pass the version check.
_ISSUE_KEYS = ("media", "search_results", "seasons", "season", "episode",
               "issue_type", "description", "research_parent",
               "search_version", "submitting_ticket", "rq_jump_media",
               "_pending_autofix_id", "hub_origin")


def _clear_issue_state(ctx) -> None:
    for key in _ISSUE_KEYS:
        ctx.user_data.pop(key, None)
    # A resume marker for this flow's own submit is stale once the flow ends;
    # markers other flows left (a Request, a Ticket reply) stay.
    marker = ctx.user_data.get("relink_resume")
    if isinstance(marker, dict) and marker.get("kind") == "submit_ticket":
        ctx.user_data.pop("relink_resume", None)


async def _ticket_timeout(update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Conversation_timeout handler. Clears every user_data key the new-Ticket
    flow can populate so an abandoned conversation doesn't leak state for
    the life of the process (a stale research_parent otherwise resurfaces
    as a wrong search suggestion in the next /issue)."""
    hub = _tickets_origin(ctx)
    for key in ("media", "search_results", "seasons", "season", "episode",
                "issue_type", "description", "research_parent",
                "search_version"):
        ctx.user_data.pop(key, None)
    # Without a notice, text typed after the timeout hits a bot that forgot
    # the conversation and answers nothing at all.
    try:
        if update is not None and update.effective_chat is not None:
            if hub is None:
                await ctx.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⏱️ The /issue flow timed out. Start again with /issue.",
                )
            else:
                sent = await ctx.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⏱️ The report timed out.",
                    reply_markup=end_markup(hub, TICKETS_REPORT),
                )
                record_btn(ctx.application, update.effective_user.id, sent)
    except Exception as exc:
        logger.debug("new-Ticket timeout notice failed (non-fatal)", exc_info=exc)
    return ConversationHandler.END


async def _ticket_expect_text_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Non-text (photo/sticker/voice) sent where a title is expected. Without
    this the message vanished with no response at all."""
    await update.effective_message.reply_text(
        "I can only read text here: type the title, or /cancel to stop.")
    return TITLE


async def _ticket_expect_text_description(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Non-text sent where the description is expected (screenshots are the
    common case)."""
    await update.effective_message.reply_text(
        "I can only read text here: describe the problem in words "
        "(screenshots don't reach Seerr), or /cancel to stop.")
    return DESCRIPTION


def _new_ticket_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("issue", ticket_start),
            CallbackQueryHandler(ticket_hub_report, pattern=fr"^{TICKETS_REPORT}$"),
        ],
        states={
            TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ticket_title),
                MessageHandler(~filters.COMMAND, _ticket_expect_text_title),
            ],
            PICK_MEDIA: [
                CallbackQueryHandler(ticket_pick_media, pattern=fr"^{TICKET_MEDIA}:"),
                CallbackQueryHandler(ticket_research_parent, pattern=fr"^{TICKET_RESEARCH_PARENT}$"),
            ],
            PICK_SEASON: [CallbackQueryHandler(ticket_pick_season, pattern=fr"^{TICKET_SEASON}:")],
            PICK_EPISODE: [
                CallbackQueryHandler(ticket_episode_page, pattern=fr"^{TICKET_EPISODE_PAGE}:"),
                CallbackQueryHandler(ticket_pick_episode, pattern=fr"^{TICKET_EPISODE}:"),
            ],
            PICK_TYPE: [CallbackQueryHandler(ticket_pick_type, pattern=fr"^{ISSUE_TYPE}:")],
            DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ticket_description),
                MessageHandler(~filters.COMMAND, _ticket_expect_text_description),
            ],
            OFFER_AUTOFIX: [CallbackQueryHandler(ticket_offer_autofix, pattern=fr"^{TICKET_AUTOFIX_OFFER}:")],
            CONFIRM_AUTOFIX: [CallbackQueryHandler(ticket_confirm_autofix, pattern=fr"^{TICKET_AUTOFIX_CONFIRM}:")],
            # TypeHandler, not MessageHandler(filters.ALL): PTB's message filters
            # reject callback-query updates, so a MessageHandler TIMEOUT
            # never fires when the flow was abandoned at a button step.
            ConversationHandler.TIMEOUT: [TypeHandler(Update, _ticket_timeout)],
        },
        fallbacks=[
            CommandHandler("cancel", ticket_cancel),
            CallbackQueryHandler(ticket_cancel, pattern=fr"^{TICKET_CANCEL}$"),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
        name="issue",
        persistent=False,
        conversation_timeout=TICKET_FLOW_TIMEOUT_S,
    )


async def ticket_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    # A typed /issue is never a Hub flow, even if a Hub left its mark behind.
    set_hub_origin(ctx, None)
    if await require_seerr(update, ctx) is None:
        return ConversationHandler.END
    # Full link gate at flow ENTRY: the old check only tested
    # for a Link, so a decrypt-failed or token-less one sailed
    # through the whole flow and died at submit with a misleading "lost
    # conversation state", discarding the typed description.
    store: UserStore = ctx.bot_data["store"]
    link = await store.get(update.effective_user.id)
    if link is None or (not link.plex_token
                        and not link.plex_token_decrypt_failed):
        await update.effective_message.reply_text(
            "DM me /link first so reports are filed as you. It's a quick "
            "Plex sign-in - no username needed."
        )
        return ConversationHandler.END
    if link.plex_token_decrypt_failed:
        await update.effective_message.reply_text(
            DECRYPT_FAILED_MSG
        )
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "What movie or show is the issue with? (Reply with the title.)"
    )
    return TITLE


async def ticket_hub_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Tickets Hub "Report a problem": the /issue title prompt, edited into
    the tapped Hub message. Same gate as typed /issue; its refusals carry the
    back button."""
    q = update.callback_query
    await q.answer()
    force_end_flow_conversations(ctx, update)
    set_hub_origin(ctx, TICKETS_HUB)
    if await require_seerr(update, ctx,
                           reply_markup=end_markup(TICKETS_HUB)) is None:
        return ConversationHandler.END
    store: UserStore = ctx.bot_data["store"]
    link = await store.get(update.effective_user.id)
    if link is None or (not link.plex_token
                        and not link.plex_token_decrypt_failed):
        await end_flow(update, ctx,
                       "DM me /link first so reports are filed as you. It's a quick "
                       "Plex sign-in - no username needed.", TICKETS_HUB)
        return ConversationHandler.END
    if link.plex_token_decrypt_failed:
        await end_flow(update, ctx, DECRYPT_FAILED_MSG, TICKETS_HUB)
        return ConversationHandler.END
    await edit_or_send(q, "What movie or show is the issue with? (Reply with the title.)")
    return TITLE


def _derive_parent_name(query: str) -> Optional[str]:
    """If query contains a title separator (' - ', ' — ', ' | ', ': '),
    return the part before the first one. Used to suggest a parent-show
    search when a movie title query returned no library matches."""
    for sep in [" - ", " — ", " | ", ": "]:
        if sep in query:
            parent = query.split(sep, 1)[0].strip()
            if len(parent) >= 3 and parent.lower() != query.strip().lower():
                return parent
    return None


async def _show_search_results(
    reply_method,
    ctx: ContextTypes.DEFAULT_TYPE,
    query: str,
    *,
    user_id: int,
) -> int:
    """Run a Seerr search for `query`, render the results as a list of
    title-buttons, and append a parent-show re-search button if no result
    is in Seerr's library. `reply_method` is an awaitable accepting
    (text, reply_markup=...) -- usually `message.reply_text` for new
    messages or `query.edit_message_text` for edits. `user_id` is needed
    to record the menu in the button gate's history. A search failure in a
    report started from the Tickets Hub carries Try again and the back
    button."""
    seerr: SeerrClient = ctx.bot_data["seerr"]
    try:
        results = await seerr.search(query, limit=SEARCH_RESULT_LIMIT)
    except Exception as exc:
        logger.exception("search failed")
        text = f"Search failed. {user_friendly_message(exc)}"
        hub = _tickets_origin(ctx)
        await reply_end(ctx, reply_method, text, hub, TICKETS_REPORT,
                        user_id=user_id)
        return ConversationHandler.END
    if not results:
        await reply_method(
            f'No matches for "{query}". TMDB search is strict about '
            "spelling: try fewer words or a corrected title, or /cancel.")
        return TITLE

    # Build the message: numbered list with full titles, since Telegram can't
    # show long titles in inline buttons reliably (no line-wrap on most clients).
    lines = ["Pick which one:", ""]
    for i, r in enumerate(results, start=1):
        type_emoji = "🎬" if r.media_type == "movie" else "📺"
        line = f"{i}. {type_emoji} {r.title}"
        if r.year:
            line += f" ({r.year})"
        lines.append(line)

    # Bump the search-results version. callback_data carries it so a rapid
    # /issue reentry that overwrites search_results doesn't make the
    # in-flight pick pull the wrong tmdb_id from the new dict.
    version = (ctx.user_data.get("search_version") or 0) + 1
    ctx.user_data["search_version"] = version

    # Build the keyboard: keycap-emoji buttons (1️⃣ 2️⃣ …), 3 per row max.
    # Three per row keeps each button wide enough to tap comfortably while
    # also keeping the keyboard compact (5 results → 3+2 grid).
    rows: list[list[InlineKeyboardButton]] = []
    btn_row: list[InlineKeyboardButton] = []
    for i, r in enumerate(results):
        keycap = KEYCAP_DIGITS[i] if i < len(KEYCAP_DIGITS) else str(i + 1)
        btn_row.append(InlineKeyboardButton(
            keycap, callback_data=f"{TICKET_MEDIA}:{version}:{r.media_type}:{r.tmdb_id}",
        ))
        if len(btn_row) == KB_BUTTONS_PER_ROW:
            rows.append(btn_row)
            btn_row = []
    last_partial_row = btn_row  # may be empty or have 1-2 buttons

    # Parent-show re-search hint when nothing matched the library and the
    # query has an obvious separator. Kept on its own full-width row since
    # its label is much longer than a keycap.
    parent = None
    if all(r.seerr_media_id is None for r in results):
        parent = _derive_parent_name(query)
        if parent:
            ctx.user_data["research_parent"] = parent

    cancel_btn = InlineKeyboardButton("🛑 Cancel", callback_data=TICKET_CANCEL)
    if parent:
        # Flush the last partial row, then parent on its own row, then Cancel.
        if last_partial_row:
            rows.append(last_partial_row)
        rows.append([InlineKeyboardButton(
            f'🔍 Search "{parent}" instead',
            callback_data=TICKET_RESEARCH_PARENT,
        )])
        rows.append([cancel_btn])
    else:
        # No parent button -- append Cancel to the last partial row if there's
        # room, else give it its own row.
        if last_partial_row and len(last_partial_row) < KB_BUTTONS_PER_ROW:
            last_partial_row.append(cancel_btn)
            rows.append(last_partial_row)
        else:
            if last_partial_row:
                rows.append(last_partial_row)
            rows.append([cancel_btn])

    ctx.user_data["search_results"] = {
        "version": version,
        "by_key": {(r.media_type, r.tmdb_id): r for r in results},
    }
    # Record the menu with the button gate, or it rejects every tap as stale
    # for any user who already has recorded button messages (e.g. ticket DMs).
    sent = await reply_method("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
    record_btn(ctx.application, user_id, sent)
    return PICK_MEDIA


async def ticket_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.effective_message.text.strip()
    await send_typing(update, ctx)  # search can take seconds on a big library
    return await _show_search_results(
        update.effective_message.reply_text, ctx, query,
        user_id=update.effective_user.id,
    )


async def ticket_research_parent(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Re-run the search with the parent show name derived from the prior query."""
    q = update.callback_query
    await q.answer()
    parent = ctx.user_data.get("research_parent")
    if not parent:
        await end_flow(update, ctx, "Lost search context.", _tickets_origin(ctx), TICKETS_REPORT,
                       typed_text="Lost search context. /issue to start over.")
        return ConversationHandler.END
    return await _show_search_results(
        q.edit_message_text, ctx, parent,
        user_id=update.effective_user.id,
    )


async def ticket_pick_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    try:
        _, version_s, media_type, tmdb_id_s = q.data.split(":")
        version = int(version_s)
        tmdb_id = int(tmdb_id_s)
    except (ValueError, AttributeError):
        await end_flow(update, ctx, "Couldn't parse selection.", _tickets_origin(ctx), TICKETS_REPORT,
                       typed_text="Couldn't parse selection. /issue to start over.")
        return ConversationHandler.END
    # Verify the embedded search version matches the current one. If the user
    # kicked off a new /issue search since this keyboard was built (allow_reentry
    # makes that easy), the in-flight pick mustn't resolve against the new
    # search_results dict.
    current = ctx.user_data.get("search_results") or {}
    if current.get("version") != version:
        await end_flow(
            update, ctx, "Search context changed since this keyboard appeared.",
            _tickets_origin(ctx), TICKETS_REPORT,
            typed_text=("Search context changed (you started a new /issue search "
                        "since this keyboard appeared). /issue to pick again."))
        return ConversationHandler.END
    selected = (current.get("by_key") or {}).get((media_type, tmdb_id))
    if selected is None or selected.seerr_media_id is None:
        parent = ctx.user_data.get("research_parent")
        text = "That title isn't in Seerr's library yet (no Plex match / no prior request)."
        rows: list[list[InlineKeyboardButton]] = []
        if parent:
            text += (
                "\n\nIf this might be a special or movie of an existing show, "
                "try searching for the show:"
            )
            rows.append([InlineKeyboardButton(
                f'🔍 Search "{parent}" instead',
                callback_data=TICKET_RESEARCH_PARENT,
            )])
        # Not in the library is exactly what /request exists for: offer the
        # jump. The tap enters the request conversation (its entry point
        # force-ends this one); the title travels via rq_jump_media so the
        # request flow doesn't re-fetch what we already know.
        if selected is not None:
            ctx.user_data["rq_jump_media"] = {
                "tmdb_id": tmdb_id,
                "title": selected.title,
                "year": selected.year,
            }
            rows.append([InlineKeyboardButton(
                "📥 Request it instead",
                callback_data=f"{RQ_FROM_TICKET}:{media_type}:{tmdb_id}",
            )])
        hub = _tickets_origin(ctx)
        if hub is None:
            text += "\n\nOr /issue to start over."
        else:
            rows.append([back_button(hub)])
        sent = await edit_or_send(
            q, text,
            reply_markup=InlineKeyboardMarkup(rows) if rows else None,
        )
        if rows:
            # Keep the re-search / request buttons live in the gate's history.
            record_btn(ctx.application, update.effective_user.id, sent)
        # END unless the parent re-search (which needs this conversation's
        # state) is offered. The "Request it instead" button is a request-
        # conversation ENTRY POINT, so it works after END too; staying
        # parked here just to host it produced a spurious "/issue timed
        # out" notice ten minutes after a terminal message.
        return PICK_MEDIA if parent else ConversationHandler.END
    ctx.user_data["media"] = {
        "type": media_type,
        "tmdb_id": tmdb_id,
        "seerr_media_id": selected.seerr_media_id,
        "title": selected.title,
        "year": selected.year,
    }
    if media_type == "tv":
        return await _show_season_picker(update, ctx)
    return await _show_type_picker(update, ctx)


async def _show_season_picker(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    seerr: SeerrClient = ctx.bot_data["seerr"]
    tmdb_id = ctx.user_data["media"]["tmdb_id"]
    try:
        seasons, tvdb_id = await seerr.get_tv_seasons(tmdb_id)
    except Exception as exc:
        logger.exception("get_tv_seasons failed")
        await end_flow(update, ctx, f"Couldn't fetch seasons. {user_friendly_message(exc)}",
                       _tickets_origin(ctx), TICKETS_REPORT)
        return ConversationHandler.END
    if not seasons:
        await end_flow(update, ctx, "No seasons found for this show.", _tickets_origin(ctx))
        return ConversationHandler.END
    ctx.user_data["media"]["tvdb_id"] = tvdb_id
    ctx.user_data["seasons"] = {s.season_number: s for s in seasons}
    rows = []
    row = []
    for s in seasons:
        # Season 0 is the Specials bucket; "S0" reads like a glitch.
        btn_label = "Specials" if s.season_number == 0 else f"S{s.season_number}"
        row.append(InlineKeyboardButton(btn_label, callback_data=f"{TICKET_SEASON}:{s.season_number}"))
        if len(row) == SEASON_BUTTONS_PER_ROW:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🛑 Cancel", callback_data=TICKET_CANCEL)])
    label = ctx.user_data["media"]["title"]
    if ctx.user_data["media"]["year"]:
        label += f" ({ctx.user_data['media']['year']})"
    # Re-record each flow step's edit so the menu stays in the newest slot of
    # the gate's history (a webhook DM burst mid-flow could otherwise evict it).
    # HTML + escape, never Markdown: a title like M*A*S*H or [REC] makes
    # Telegram reject the edit ("can't parse entities") and wipes the flow.
    sent = await q.edit_message_text(
        f"Selected: <b>{html.escape(label)}</b>\n\nWhich season?",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="HTML",
    )
    record_btn(ctx.application, update.effective_user.id, sent)
    return PICK_SEASON


async def ticket_pick_season(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    try:
        season = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        await end_flow(update, ctx, "Couldn't parse season.", _tickets_origin(ctx), TICKETS_REPORT,
                       typed_text="Couldn't parse season. /issue to start over.")
        return ConversationHandler.END
    ctx.user_data["season"] = season
    season_obj = ctx.user_data["seasons"].get(season)
    ep_count = season_obj.episode_count if season_obj else 0
    if ep_count <= 0:
        # No episode list available (announced season, or Seerr/Sonarr didn't
        # report a count): a picker would show only "Whole season" + Cancel,
        # so skip straight to the whole-season selection.
        ctx.user_data["episode"] = None
        return await _show_type_picker(update, ctx)
    return await _show_episode_page(update, ctx, season, ep_count, page=0)


def _episode_page_keyboard(season: int, ep_count: int, page: int) -> InlineKeyboardMarkup:
    """One page of episode buttons. Paginated: Telegram rejects reply markup
    past 100 buttons, and long-running seasons (anime, soaps) can top that,
    which used to kill the flow and strand a dead season keyboard."""
    last_page = max(0, (ep_count - 1) // EPISODE_PICKER_PAGE_SIZE)
    page = max(0, min(page, last_page))
    start = page * EPISODE_PICKER_PAGE_SIZE + 1
    end = min(ep_count, start + EPISODE_PICKER_PAGE_SIZE - 1)
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for ep in range(start, end + 1):
        row.append(InlineKeyboardButton(f"E{ep}", callback_data=f"{TICKET_EPISODE}:{ep}"))
        if len(row) == EPISODE_BUTTONS_PER_ROW:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if last_page > 0:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                "⬅️ Earlier", callback_data=f"{TICKET_EPISODE_PAGE}:{page - 1}"))
        if page < last_page:
            nav.append(InlineKeyboardButton(
                "➡️ Later", callback_data=f"{TICKET_EPISODE_PAGE}:{page + 1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton("📦 Whole season", callback_data=f"{TICKET_EPISODE}:0")])
    rows.append([InlineKeyboardButton("🛑 Cancel", callback_data=TICKET_CANCEL)])
    return InlineKeyboardMarkup(rows)


async def _show_episode_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                             season: int, ep_count: int, page: int) -> int:
    q = update.callback_query
    season_label = "Specials" if season == 0 else f"Season {season}"
    sent = await q.edit_message_text(
        f"{season_label} - which episode?",
        reply_markup=_episode_page_keyboard(season, ep_count, page),
    )
    record_btn(ctx.application, update.effective_user.id, sent)
    return PICK_EPISODE


async def ticket_episode_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    try:
        page = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        await end_flow(update, ctx, "Couldn't parse selection.", _tickets_origin(ctx), TICKETS_REPORT,
                       typed_text="Couldn't parse selection. /issue to start over.")
        return ConversationHandler.END
    season = ctx.user_data.get("season")
    if season is None:
        # State evaporated (error-handler cleanup, restart) between pages.
        await end_flow(update, ctx, "That selection expired.", _tickets_origin(ctx), TICKETS_REPORT,
                       typed_text="That selection expired. /issue to start over.")
        return ConversationHandler.END
    season_obj = (ctx.user_data.get("seasons") or {}).get(season)
    ep_count = season_obj.episode_count if season_obj else 0
    return await _show_episode_page(update, ctx, season, ep_count, page)


async def ticket_pick_episode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    try:
        ep = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        await end_flow(update, ctx, "Couldn't parse episode.", _tickets_origin(ctx), TICKETS_REPORT,
                       typed_text="Couldn't parse episode. /issue to start over.")
        return ConversationHandler.END
    # ep=0 means whole season
    ctx.user_data["episode"] = ep if ep > 0 else None
    return await _show_type_picker(update, ctx)


async def _show_type_picker(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    type_buttons = [
        InlineKeyboardButton(f"{e} {n}", callback_data=f"{ISSUE_TYPE}:{i}")
        for i, (e, n) in ISSUE_TYPES.items()
    ]
    rows = [type_buttons[i:i + TYPE_BUTTONS_PER_ROW]
            for i in range(0, len(type_buttons), TYPE_BUTTONS_PER_ROW)]
    rows.append([InlineKeyboardButton("🛑 Cancel", callback_data=TICKET_CANCEL)])
    media = ctx.user_data["media"]
    label = media["title"]
    if media.get("year"):
        label += f" ({media['year']})"
    if media["type"] == "tv":
        season = ctx.user_data.get("season")
        ep = ctx.user_data.get("episode")
        season_label = "Specials" if season == 0 else f"S{int(season or 0):02d}"
        if ep is None:
            label += f" - {season_label} (whole season)"
        else:
            # Zero-padded like format_media_label so the same selection
            # doesn't change format between consecutive screens.
            label += f" - S{int(season or 0):02d}E{int(ep):02d}"
    # HTML + escape for the same reason as the season screen: raw titles
    # break Markdown entity parsing and kill the flow mid-edit.
    sent = await q.edit_message_text(
        f"Selected: <b>{html.escape(label)}</b>\n\nWhat kind of issue?",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="HTML",
    )
    record_btn(ctx.application, update.effective_user.id, sent)
    return PICK_TYPE


async def ticket_pick_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    try:
        issue_type = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        await end_flow(update, ctx, "Couldn't parse selection.", _tickets_origin(ctx), TICKETS_REPORT,
                       typed_text="Couldn't parse selection. /issue to start over.")
        return ConversationHandler.END
    if issue_type not in ISSUE_TYPES:
        await end_flow(update, ctx, "Unknown issue type.", _tickets_origin(ctx), TICKETS_REPORT,
                       typed_text="Unknown issue type. /issue to start over.")
        return ConversationHandler.END
    ctx.user_data["issue_type"] = issue_type
    emoji, name = ISSUE_TYPES[issue_type]
    await q.edit_message_text(
        f"Type: {emoji} *{name}*\n\nNow briefly describe what's wrong:",
        parse_mode="Markdown",
    )
    return DESCRIPTION


async def ticket_description(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    description = update.effective_message.text.strip()
    if not description:
        await update.effective_message.reply_text(
            "Description can't be empty. Send a short message, or /cancel."
        )
        return DESCRIPTION
    ctx.user_data["description"] = description
    # Decide whether to offer auto-fix
    issue_type = ctx.user_data.get("issue_type")
    # Snapshot the allowlist at handler entry so a mid-handler settings
    # reload can't shift the eligibility check to a stale set between this
    # read and the await on store.count_autofix_24h below.
    allowlist_snapshot: frozenset[int] = frozenset(ctx.bot_data.get("allowlist") or ())
    allow_all = bool(ctx.bot_data.get("autofix_allow_all"))
    store: UserStore = ctx.bot_data["store"]
    tg_id = update.effective_user.id
    media = ctx.user_data.get("media", {})
    episode = ctx.user_data.get("episode")
    # Whole-season TV picks are not auto-fixable; only individual episodes or
    # movies are. (The bot offers one episode or a whole season, with no
    # all-seasons button; a whole-show Ticket is made in Seerr's own UI.)
    is_whole_season = media.get("type") == "tv" and not episode
    eligible = (
        issue_type in AUTOFIX_ELIGIBLE_TYPES
        and (allow_all or tg_id in allowlist_snapshot)
        and _has_arr_for_media(ctx)
        and not is_whole_season
    )
    if not eligible:
        return await _submit_ticket(update, ctx, autofix=False)
    # Admin bypasses the daily rate limit
    is_admin = tg_id == ctx.bot_data.get("admin_id")
    if not is_admin:
        settings_store: SettingsStore = ctx.bot_data["settings_store"]
        s = settings_store.settings
        if s.daily_autofix_unlimited:
            remaining_msg = ""
        else:
            daily_limit = s.daily_autofix_limit
            used = await store.count_autofix_24h(tg_id)
            if used >= daily_limit:
                await update.effective_message.reply_text(
                    f"(You've used your {daily_limit} auto-fixes today; "
                    f"submitting the Ticket without auto-fix.)"
                )
                return await _submit_ticket(update, ctx, autofix=False)
            remaining_msg = f"\n(Auto-fixes remaining today: {daily_limit - used})"
    else:
        remaining_msg = ""
    rows = [[
        InlineKeyboardButton("✅ Try auto-fix", callback_data=f"{TICKET_AUTOFIX_OFFER}:yes"),
        InlineKeyboardButton("📨 Just report", callback_data=f"{TICKET_AUTOFIX_OFFER}:no"),
    ]]
    sent = await update.effective_message.reply_text(
        f"Try to auto-fix? This will delete the file and trigger a new search.{remaining_msg}",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    record_btn(ctx.application, update.effective_user.id, sent)
    return OFFER_AUTOFIX


def _has_arr_for_media(ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    media = ctx.user_data.get("media", {})
    if media.get("type") == "movie":
        return ctx.bot_data.get("radarr") is not None
    if media.get("type") == "tv":
        return ctx.bot_data.get("sonarr") is not None and media.get("tvdb_id") is not None
    return False


async def ticket_offer_autofix(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    choice = q.data.split(":")[1]
    if choice == "no":
        await q.edit_message_text("Got it. Submitting your report without auto-fix.")
        return await _submit_ticket(update, ctx, autofix=False)
    rows = [[
        InlineKeyboardButton("⚠️ Yes, delete & re-search", callback_data=f"{TICKET_AUTOFIX_CONFIRM}:yes"),
        InlineKeyboardButton("No, just report", callback_data=f"{TICKET_AUTOFIX_CONFIRM}:no"),
    ]]
    sent = await q.edit_message_text(
        "⚠️ This will *delete the current file* from disk and trigger a new download. Confirm?",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown",
    )
    record_btn(ctx.application, update.effective_user.id, sent)
    return CONFIRM_AUTOFIX


async def ticket_confirm_autofix(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    choice = q.data.split(":")[1]
    if choice == "no":
        await q.edit_message_text("Skipping auto-fix. Submitting your report.")
        return await _submit_ticket(update, ctx, autofix=False)
    await q.edit_message_text("Submitting your report and triggering auto-fix...")
    return await _submit_ticket(update, ctx, autofix=True)


async def _submit_ticket(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    autofix: bool,
) -> int:
    """Double-tap guard around the real submit: with
    concurrent_updates(True), a second confirm tap runs in parallel and
    would file a duplicate Seerr Ticket. user_data is shared across the
    user's updates and there is no await between check and set."""
    if ctx.user_data.get("submitting_ticket"):
        return ConversationHandler.END
    ctx.user_data["submitting_ticket"] = True
    try:
        return await _submit_ticket_inner(update, ctx, autofix=autofix)
    finally:
        ctx.user_data.pop("submitting_ticket", None)


async def _submit_ticket_inner(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    autofix: bool,
) -> int:
    await send_typing(update, ctx)  # Seerr create + optional Arr calls are slow
    store: UserStore = ctx.bot_data["store"]
    seerr: SeerrClient = ctx.bot_data["seerr"]
    radarr: Optional[RadarrClient] = ctx.bot_data.get("radarr")
    sonarr: Optional[SonarrClient] = ctx.bot_data.get("sonarr")

    media = ctx.user_data.get("media", {})
    issue_type = ctx.user_data.get("issue_type")
    description = ctx.user_data.get("description", "")
    season = ctx.user_data.get("season")
    episode = ctx.user_data.get("episode")

    hub = _tickets_origin(ctx)
    link = await store.get(update.effective_user.id)
    if not (link and link.plex_token and issue_type and media):
        await end_flow(
            update, ctx, "Lost conversation state or your Plex link is incomplete.",
            hub, TICKETS_REPORT, edit=False,
            typed_text=("Lost conversation state or your /link is incomplete. "
                        "/link then /issue to start over."))
        return ConversationHandler.END

    full_message = description
    if autofix:
        full_message += (
            "\n\n(Auto-fix requested by reporter: the current file was removed and a "
            "fresh download was started automatically. This can take a while to finish "
            "and reappear in Plex.)"
        )

    # 1. Create the Ticket in Seerr first
    try:
        created: CreatedIssue = await seerr.create_issue(
            issue_type=issue_type,
            message=full_message,
            seerr_media_id=media["seerr_media_id"],
            media_type=media["type"],
            problem_season=season if media["type"] == "tv" else None,
            problem_episode=episode if media["type"] == "tv" else None,
            as_plex_token=link.plex_token,
        )
    except PlexTokenInvalidError:
        # The draft (media, type, description) stays in user_data; only the
        # autofix choice needs carrying for the post-relink resume.
        await prompt_plex_relink(update, ctx, resume_kind="submit_ticket",
                                 resume_payload={"autofix": autofix},
                                 back=back_button(hub) if hub else None)
        return ConversationHandler.END
    except Exception as exc:
        logger.exception("create_issue failed")
        await end_flow(update, ctx, f"Couldn't submit the ticket. {user_friendly_message(exc)}",
                       hub, TICKETS_REPORT, edit=False)
        return ConversationHandler.END

    emoji, name = ISSUE_TYPES[issue_type]
    label = format_media_label(
        media["title"], media.get("year") or "",
        season=season if media["type"] == "tv" else None,
        episode=episode,
    )
    # The whole-season "(whole season)" hint is unique to this surface, so
    # tack it on after the canonical label.
    if media["type"] == "tv" and season and not episode:
        label += " (whole season)"

    lines = [
        f"✅ Reported as ticket #{created.id}",
        f"  {emoji} {name} - {label}",
    ]

    # 2. If auto-fix requested, run it. Serialized per title:
    # an admin fix and a user auto-fix racing on the same media must not
    # interleave delete/blocklist/search.
    if autofix:
        # Re-check the daily quota at execution time: the offer-time check
        # can be raced by the same user confirming concurrently in two
        # chats (conversations are per-chat), and the delete+search below
        # is destructive.
        tg_id = update.effective_user.id
        if tg_id != ctx.bot_data.get("admin_id"):
            s_cfg = ctx.bot_data["settings_store"].settings
            if not s_cfg.daily_autofix_unlimited:
                used = await store.count_autofix_24h(tg_id)
                if used >= s_cfg.daily_autofix_limit:
                    autofix = False
                    lines.append(
                        f"⚠️ Auto-fix skipped: you've used your "
                        f"{s_cfg.daily_autofix_limit} auto-fixes today."
                    )
    if autofix:
        media_key = media_action_key(media)
        if not try_begin_action(ctx, media_key):
            lines.append("⚠️ Auto-fix skipped: another fix for this title is already running.")
        else:
            try:
                result = await _run_arr_action(media, season, episode, radarr, sonarr, action="fix")
                if result.status == "failed":
                    lines.append(f"⚠️ Auto-fix didn't run: {result.message}")
                else:
                    # ok or partial: always log the autofix event; only enqueue the
                    # completion poller when search actually ran.
                    await store.log_autofix(
                        update.effective_user.id,
                        media["type"],
                        media["tmdb_id"],
                        season=season,
                        episode=episode,
                    )
                    if result.should_poll:
                        try:
                            poll_info = result.poll_info or {}
                            kwargs = {
                                "chat_id": update.effective_chat.id,
                                "user_id": update.effective_user.id,
                                "media_type": media["type"],
                                "label": label,
                                "ticket_id": created.id,
                                "ticket_url": created.url,
                            }
                            if media["type"] == "movie":
                                kwargs["radarr_movie_id"] = poll_info.get("movie_id")
                            else:
                                # poll_info only ever carries series_id/episode_id (the
                                # whole-season workflow died in 0.12.0); the season /
                                # expected-episodes kwargs were always None and are gone.
                                kwargs["sonarr_series_id"] = poll_info.get("series_id")
                                kwargs["sonarr_episode_id"] = poll_info.get("episode_id")
                            ctx.user_data["_pending_autofix_id"] = (
                                await store.add_pending_autofix(**kwargs))
                            prefix = "🔧" if result.ok else "⚠️"
                            lines.append(f"{prefix} Auto-fix: {result.message}")
                            lines.append(
                                "🔔 I'll DM you when the new file finishes "
                                f"downloading (or after {AUTOFIX_TIMEOUT_HOURS}h timeout).")
                        except Exception:
                            logger.exception("failed to enqueue pending autofix")
                            prefix = "🔧" if result.ok else "⚠️"
                            lines.append(f"{prefix} Auto-fix: {result.message}")
                            lines.append("(Couldn't enqueue completion notification.)")
                    else:
                        # No search step ran — there's nothing to poll for.
                        lines.append(f"⚠️ Auto-fix: {result.message}")
            finally:
                end_action(ctx, media_key)

    lines.append("\nUse /tickets to manage it.")
    markup = None if hub is None else end_markup(hub)
    sent = await update.effective_message.reply_text(
        "\n".join(lines), reply_markup=markup)
    if markup is not None:
        record_btn(ctx.application, update.effective_user.id, sent)
    # If a poller row was enqueued this submit, attach the confirmation
    # message so the poll ticks can turn it into a Progress card.
    pending_id = ctx.user_data.pop("_pending_autofix_id", None)
    if pending_id and getattr(sent, "message_id", None):
        try:
            await store.set_autofix_message(
                pending_id, sent.message_id,
                reply_markup=markup.to_json() if markup is not None else "")
        except Exception as exc:
            logger.exception("couldn't attach autofix card message", exc_info=exc)
    _clear_issue_state(ctx)
    return ConversationHandler.END


async def _resume_submit_ticket(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                                payload: dict) -> None:
    """Relink-resume executor: re-run the submit that was gated by a revoked
    token. The draft is still in user_data (the gate didn't clear it), and
    _submit_ticket's own guard handles the case where it got clobbered."""
    # If the user started a NEW /issue while re-linking, user_data now holds
    # that conversation's half-built draft; auto-submitting it would file a
    # partial report.
    if user_in_conversation(ctx, update, "issue"):
        await update.effective_message.reply_text(
            "You've started a new /issue since then, so I didn't auto-submit "
            "the interrupted report. Finish the current one instead."
        )
        return
    await _submit_ticket(update, ctx, autofix=bool(payload.get("autofix")))


RELINK_RESUME_EXECUTORS["submit_ticket"] = _resume_submit_ticket


async def ticket_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer("Cancelled")
    await end_flow(update, ctx, "Cancelled.", _tickets_origin(ctx),
                   typed_text="Cancelled. /issue to start over.")
    _clear_issue_state(ctx)
    return ConversationHandler.END


