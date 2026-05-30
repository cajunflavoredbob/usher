"""/issue conversation: search, pick media, pick season/episode, pick type,
take description, optional auto-fix offer, submit to Seerr."""
from __future__ import annotations

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
    filters,
)

from http_util import user_friendly_message
from radarr import RadarrClient
from seerr import CreatedIssue, SeerrClient
from settings import SettingsStore
from sonarr import SonarrClient
from store import UserStore

from bot.callback_prefixes import (
    ISSUE_AUTOFIX_CONFIRM,
    ISSUE_AUTOFIX_OFFER,
    ISSUE_CANCEL,
    ISSUE_EPISODE,
    ISSUE_MEDIA,
    ISSUE_RESEARCH_PARENT,
    ISSUE_SEASON,
    ISSUE_TYPE,
)
from bot.shared import (
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
    _format_media_label,
    _require_seerr,
)
from bot.tickets import _run_arr_action
from const import ISSUE_FLOW_TIMEOUT_S, KB_BUTTONS_PER_ROW, SEARCH_RESULT_LIMIT

logger = logging.getLogger("hermes")

# --- Issue conversation ------------------------------------------------------

async def _issue_timeout(update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Conversation_timeout handler. Clears every user_data key the issue
    flow can populate so an abandoned conversation doesn't leak state for
    the life of the process."""
    for key in ("media", "search_results", "seasons", "season", "episode",
                "issue_type", "description", "autofix"):
        ctx.user_data.pop(key, None)
    return ConversationHandler.END


def _issue_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("issue", issue_start)],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, issue_title)],
            PICK_MEDIA: [
                CallbackQueryHandler(issue_pick_media, pattern=fr"^{ISSUE_MEDIA}:"),
                CallbackQueryHandler(issue_research_parent, pattern=fr"^{ISSUE_RESEARCH_PARENT}$"),
            ],
            PICK_SEASON: [CallbackQueryHandler(issue_pick_season, pattern=fr"^{ISSUE_SEASON}:")],
            PICK_EPISODE: [CallbackQueryHandler(issue_pick_episode, pattern=fr"^{ISSUE_EPISODE}:")],
            PICK_TYPE: [CallbackQueryHandler(issue_pick_type, pattern=fr"^{ISSUE_TYPE}:")],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, issue_description)],
            OFFER_AUTOFIX: [CallbackQueryHandler(issue_offer_autofix, pattern=fr"^{ISSUE_AUTOFIX_OFFER}:")],
            CONFIRM_AUTOFIX: [CallbackQueryHandler(issue_confirm_autofix, pattern=fr"^{ISSUE_AUTOFIX_CONFIRM}:")],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, _issue_timeout)],
        },
        fallbacks=[
            CommandHandler("cancel", issue_cancel),
            CallbackQueryHandler(issue_cancel, pattern=fr"^{ISSUE_CANCEL}$"),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
        name="issue",
        persistent=False,
        conversation_timeout=ISSUE_FLOW_TIMEOUT_S,
    )


async def issue_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if await _require_seerr(update, ctx) is None:
        return ConversationHandler.END
    store: UserStore = ctx.bot_data["store"]
    if (await store.get(update.effective_user.id)) is None:
        await update.effective_message.reply_text(
            "You need to link your Seerr account first. DM me /link <username>."
        )
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "What movie or show is the issue with? (Reply with the title.)"
    )
    return TITLE


_KEYCAP_DIGITS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


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
) -> int:
    """Run a Seerr search for `query`, render the results as a list of
    title-buttons, and append a parent-show re-search button if no result
    is in Seerr's library. `reply_method` is an awaitable accepting
    (text, reply_markup=...) -- usually `message.reply_text` for new
    messages or `query.edit_message_text` for edits."""
    seerr: SeerrClient = ctx.bot_data["seerr"]
    try:
        results = await seerr.search(query, limit=SEARCH_RESULT_LIMIT)
    except Exception as exc:
        logger.exception("search failed")
        await reply_method(f"Search failed. {user_friendly_message(exc)}")
        return ConversationHandler.END
    if not results:
        await reply_method(f'No matches for "{query}". Try a different title, or /cancel.')
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

    # Build the keyboard: keycap-emoji buttons (1️⃣ 2️⃣ …), 3 per row max.
    # Three per row keeps each button wide enough to tap comfortably while
    # also keeping the keyboard compact (5 results → 3+2 grid).
    rows: list[list[InlineKeyboardButton]] = []
    btn_row: list[InlineKeyboardButton] = []
    for i, r in enumerate(results):
        keycap = _KEYCAP_DIGITS[i] if i < len(_KEYCAP_DIGITS) else str(i + 1)
        btn_row.append(InlineKeyboardButton(
            keycap, callback_data=f"{ISSUE_MEDIA}:{r.media_type}:{r.tmdb_id}",
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

    cancel_btn = InlineKeyboardButton("Cancel", callback_data=ISSUE_CANCEL)
    if parent:
        # Flush the last partial row, then parent on its own row, then Cancel.
        if last_partial_row:
            rows.append(last_partial_row)
        rows.append([InlineKeyboardButton(
            f'🔍 Search "{parent}" instead',
            callback_data=ISSUE_RESEARCH_PARENT,
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

    ctx.user_data["search_results"] = {(r.media_type, r.tmdb_id): r for r in results}
    await reply_method("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
    return PICK_MEDIA


async def issue_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.effective_message.text.strip()
    return await _show_search_results(update.effective_message.reply_text, ctx, query)


async def issue_research_parent(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Re-run the search with the parent show name derived from the prior query."""
    q = update.callback_query
    await q.answer()
    parent = ctx.user_data.get("research_parent")
    if not parent:
        await q.edit_message_text("Lost search context. /issue to start over.")
        return ConversationHandler.END
    return await _show_search_results(q.edit_message_text, ctx, parent)


async def issue_pick_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    try:
        _, media_type, tmdb_id_s = q.data.split(":")
        tmdb_id = int(tmdb_id_s)
    except (ValueError, AttributeError):
        await q.edit_message_text("Couldn't parse selection. /issue to start over.")
        return ConversationHandler.END
    selected = ctx.user_data.get("search_results", {}).get((media_type, tmdb_id))
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
                callback_data=ISSUE_RESEARCH_PARENT,
            )])
        text += "\n\nOr /issue to start over."
        await q.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(rows) if rows else None,
        )
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
        await q.edit_message_text(f"Couldn't fetch seasons. {user_friendly_message(exc)}")
        return ConversationHandler.END
    if not seasons:
        await q.edit_message_text("No seasons found for this show.")
        return ConversationHandler.END
    ctx.user_data["media"]["tvdb_id"] = tvdb_id
    ctx.user_data["seasons"] = {s.season_number: s for s in seasons}
    # Lay out season buttons in rows of 4
    rows = []
    row = []
    for s in seasons:
        row.append(InlineKeyboardButton(f"S{s.season_number}", callback_data=f"{ISSUE_SEASON}:{s.season_number}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Cancel", callback_data=ISSUE_CANCEL)])
    label = ctx.user_data["media"]["title"]
    if ctx.user_data["media"]["year"]:
        label += f" ({ctx.user_data['media']['year']})"
    await q.edit_message_text(
        f"Selected: *{label}*\n\nWhich season?",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown",
    )
    return PICK_SEASON


async def issue_pick_season(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    try:
        season = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        await q.edit_message_text("Couldn't parse season. /issue to start over.")
        return ConversationHandler.END
    ctx.user_data["season"] = season
    season_obj = ctx.user_data["seasons"].get(season)
    ep_count = season_obj.episode_count if season_obj else 0
    rows = []
    row = []
    # Up to ep_count buttons; "Whole season" option last
    for ep in range(1, ep_count + 1):
        row.append(InlineKeyboardButton(f"E{ep}", callback_data=f"{ISSUE_EPISODE}:{ep}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("📦 Whole season", callback_data=f"{ISSUE_EPISODE}:0")])
    rows.append([InlineKeyboardButton("Cancel", callback_data=ISSUE_CANCEL)])
    await q.edit_message_text(
        f"Season {season} — which episode?",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return PICK_EPISODE


async def issue_pick_episode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    try:
        ep = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        await q.edit_message_text("Couldn't parse episode. /issue to start over.")
        return ConversationHandler.END
    # ep=0 means whole season
    ctx.user_data["episode"] = ep if ep > 0 else None
    return await _show_type_picker(update, ctx)


async def _show_type_picker(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    rows = [[
        InlineKeyboardButton(f"{e} {n}", callback_data=f"{ISSUE_TYPE}:{i}")
        for i, (e, n) in ISSUE_TYPES.items()
    ]]
    rows.append([InlineKeyboardButton("Cancel", callback_data=ISSUE_CANCEL)])
    media = ctx.user_data["media"]
    label = media["title"]
    if media.get("year"):
        label += f" ({media['year']})"
    if media["type"] == "tv":
        season = ctx.user_data.get("season")
        ep = ctx.user_data.get("episode")
        if ep is None:
            label += f" — S{season} (whole season)"
        else:
            label += f" — S{season}E{ep}"
    await q.edit_message_text(
        f"Selected: *{label}*\n\nWhat kind of issue?",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown",
    )
    return PICK_TYPE


async def issue_pick_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    try:
        issue_type = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        await q.edit_message_text("Couldn't parse selection. /issue to start over.")
        return ConversationHandler.END
    if issue_type not in ISSUE_TYPES:
        await q.edit_message_text("Unknown issue type. /issue to start over.")
        return ConversationHandler.END
    ctx.user_data["issue_type"] = issue_type
    emoji, name = ISSUE_TYPES[issue_type]
    await q.edit_message_text(
        f"Type: {emoji} *{name}*\n\nNow briefly describe what's wrong:",
        parse_mode="Markdown",
    )
    return DESCRIPTION


async def issue_description(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
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
    store: UserStore = ctx.bot_data["store"]
    tg_id = update.effective_user.id
    media = ctx.user_data.get("media", {})
    episode = ctx.user_data.get("episode")
    # Whole-season / whole-show TV picks are not auto-fixable; only individual
    # episodes or movies are.
    is_whole_season = media.get("type") == "tv" and not episode
    eligible = (
        issue_type in AUTOFIX_ELIGIBLE_TYPES
        and tg_id in allowlist_snapshot
        and _has_arr_for_media(ctx)
        and not is_whole_season
    )
    if not eligible:
        return await _submit_issue(update, ctx, autofix=False)
    # Admin bypasses the daily rate limit
    is_admin = tg_id == ctx.bot_data.get("admin_id")
    if not is_admin:
        settings_store: SettingsStore = ctx.bot_data["settings_store"]
        daily_limit = settings_store.settings.daily_autofix_limit
        used = await store.count_autofix_24h(tg_id)
        if used >= daily_limit:
            await update.effective_message.reply_text(
                f"(You've used your {daily_limit} auto-fixes today; "
                f"submitting issue without auto-fix.)"
            )
            return await _submit_issue(update, ctx, autofix=False)
        remaining_msg = f"\n(Auto-fixes remaining today: {daily_limit - used})"
    else:
        remaining_msg = ""
    rows = [[
        InlineKeyboardButton("✅ Try auto-fix", callback_data=f"{ISSUE_AUTOFIX_OFFER}:yes"),
        InlineKeyboardButton("📨 Just report", callback_data=f"{ISSUE_AUTOFIX_OFFER}:no"),
    ]]
    await update.effective_message.reply_text(
        f"Try to auto-fix? This will delete the file and trigger a new search.{remaining_msg}",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return OFFER_AUTOFIX


def _has_arr_for_media(ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    media = ctx.user_data.get("media", {})
    if media.get("type") == "movie":
        return ctx.bot_data.get("radarr") is not None
    if media.get("type") == "tv":
        return ctx.bot_data.get("sonarr") is not None and media.get("tvdb_id") is not None
    return False


async def issue_offer_autofix(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    choice = q.data.split(":")[1]
    if choice == "no":
        await q.edit_message_text("Got it. Submitting issue without auto-fix.")
        return await _submit_issue(update, ctx, autofix=False)
    rows = [[
        InlineKeyboardButton("⚠️ Yes, delete & re-search", callback_data=f"{ISSUE_AUTOFIX_CONFIRM}:yes"),
        InlineKeyboardButton("No, just report", callback_data=f"{ISSUE_AUTOFIX_CONFIRM}:no"),
    ]]
    await q.edit_message_text(
        "⚠️ This will *delete the current file* from disk and trigger a new download. Confirm?",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown",
    )
    return CONFIRM_AUTOFIX


async def issue_confirm_autofix(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    choice = q.data.split(":")[1]
    if choice == "no":
        await q.edit_message_text("Skipping auto-fix. Submitting issue.")
        return await _submit_issue(update, ctx, autofix=False)
    await q.edit_message_text("Submitting issue and triggering auto-fix...")
    return await _submit_issue(update, ctx, autofix=True)


async def _submit_issue(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    autofix: bool,
) -> int:
    store: UserStore = ctx.bot_data["store"]
    seerr: SeerrClient = ctx.bot_data["seerr"]
    radarr: Optional[RadarrClient] = ctx.bot_data.get("radarr")
    sonarr: Optional[SonarrClient] = ctx.bot_data.get("sonarr")

    media = ctx.user_data.get("media", {})
    issue_type = ctx.user_data.get("issue_type")
    description = ctx.user_data.get("description", "")
    season = ctx.user_data.get("season")
    episode = ctx.user_data.get("episode")

    mapping = await store.get(update.effective_user.id)
    if not (mapping and mapping.plex_token and issue_type and media):
        await update.effective_message.reply_text(
            "Lost conversation state or your /link is incomplete. /link then /issue to start over."
        )
        return ConversationHandler.END

    full_message = description
    if autofix:
        full_message += "\n\n(Auto-fix triggered by reporter.)"

    # 1. Create issue in Seerr first
    try:
        created: CreatedIssue = await seerr.create_issue(
            issue_type=issue_type,
            message=full_message,
            seerr_media_id=media["seerr_media_id"],
            media_type=media["type"],
            problem_season=season if media["type"] == "tv" else None,
            problem_episode=episode if media["type"] == "tv" else None,
            as_plex_token=mapping.plex_token,
        )
    except Exception as exc:
        logger.exception("create_issue failed")
        await update.effective_message.reply_text(f"Failed to create issue. {user_friendly_message(exc)}")
        return ConversationHandler.END

    emoji, name = ISSUE_TYPES[issue_type]
    label = _format_media_label(
        media["title"], media.get("year") or "",
        season=season if media["type"] == "tv" else None,
        episode=episode,
    )
    # The whole-season "(whole season)" hint is unique to this surface, so
    # tack it on after the canonical label.
    if media["type"] == "tv" and season and not episode:
        label += " (whole season)"

    lines = [
        f"✅ Reported as issue #{created.id}",
        f"  {emoji} {name} — {label}",
    ]

    # 2. If auto-fix requested, run it
    if autofix:
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
                        "issue_id": created.id,
                        "issue_url": created.url,
                    }
                    if media["type"] == "movie":
                        kwargs["radarr_movie_id"] = poll_info.get("movie_id")
                    else:
                        kwargs["sonarr_series_id"] = poll_info.get("series_id")
                        kwargs["sonarr_episode_id"] = poll_info.get("episode_id")
                        kwargs["sonarr_season"] = poll_info.get("season")
                        kwargs["expected_episode_ids"] = poll_info.get("expected_episode_ids") or []
                    await store.add_pending_autofix(**kwargs)
                    prefix = "🔧" if result.ok else "⚠️"
                    lines.append(f"{prefix} Auto-fix: {result.message}")
                    lines.append("🔔 I'll DM you when the new file finishes downloading (or after 6h timeout).")
                except Exception:
                    logger.exception("failed to enqueue pending autofix")
                    prefix = "🔧" if result.ok else "⚠️"
                    lines.append(f"{prefix} Auto-fix: {result.message}")
                    lines.append("(Couldn't enqueue completion notification.)")
            else:
                # No search step ran — there's nothing to poll for.
                lines.append(f"⚠️ Auto-fix: {result.message}")

    lines.append("\nUse /tickets to manage it.")
    await update.effective_message.reply_text("\n".join(lines))
    ctx.user_data.clear()
    return ConversationHandler.END


async def issue_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer("Cancelled")
        await update.callback_query.edit_message_text("Cancelled. /issue to start over.")
    else:
        await update.effective_message.reply_text("Cancelled. /issue to start over.")
    ctx.user_data.clear()
    return ConversationHandler.END


