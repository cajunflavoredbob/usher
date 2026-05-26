"""Seerr issue-reporting Telegram bot with optional Radarr/Sonarr auto-fix.

Conversation:
  /issue
    -> ask title -> show search hits -> pick one
       if TV: -> pick season -> pick episode (or "Whole season")
    -> pick issue type
    -> ask description
    if issue type in (Video|Audio|Subtitles) AND user is on the auto-fix allowlist
       AND auto-fix budget remains for today:
       -> offer auto-fix [Yes] [No]
       if Yes: -> confirm [Yes, do it] [No, just report]
    -> submit issue to Seerr (always)
    if auto-fix confirmed: -> tell Radarr/Sonarr to delete + research
"""
from __future__ import annotations

import html
import logging
import os
import sys
from typing import Final, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from seerr import SeerrClient, CreatedIssue
from store import UserStore
from radarr import RadarrClient
from sonarr import SonarrClient

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("hermes")

# Conversation states
TITLE, PICK_MEDIA, PICK_SEASON, PICK_EPISODE, PICK_TYPE, DESCRIPTION, OFFER_AUTOFIX, CONFIRM_AUTOFIX = range(8)

ISSUE_TYPES: Final = {
    1: ("🎥", "Video"),
    2: ("🔊", "Audio"),
    3: ("📝", "Subtitles"),
    4: ("❓", "Other"),
}

AUTOFIX_ELIGIBLE_TYPES = {1, 2, 3}
DAILY_AUTOFIX_LIMIT = 3


# --- App setup ---------------------------------------------------------------

def _parse_allowlist(raw: str) -> set[int]:
    if not raw:
        return set()
    out: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            out.add(int(chunk))
    return out


def _build_app() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    seerr_url = os.environ["SEERR_URL"]
    seerr_key = os.environ["SEERR_API_KEY"]
    admin_id = int(os.environ["ADMIN_TELEGRAM_ID"])
    db_path = os.environ.get("STORE_PATH", "/data/mappings.sqlite")

    # Optional auto-fix configuration
    radarr_url = os.environ.get("RADARR_URL", "").strip()
    radarr_key = os.environ.get("RADARR_API_KEY", "").strip()
    sonarr_url = os.environ.get("SONARR_URL", "").strip()
    sonarr_key = os.environ.get("SONARR_API_KEY", "").strip()
    allowlist = _parse_allowlist(os.environ.get("ALLOWED_AUTOFIX_TELEGRAM_IDS", ""))
    if not allowlist:
        # Default: only the admin can use auto-fix unless explicitly broadened
        allowlist = {admin_id}
        logger.info("ALLOWED_AUTOFIX_TELEGRAM_IDS unset; defaulting to admin only")

    seerr = SeerrClient(seerr_url, seerr_key)
    store = UserStore(db_path)
    radarr = RadarrClient(radarr_url, radarr_key) if (radarr_url and radarr_key) else None
    sonarr = SonarrClient(sonarr_url, sonarr_key) if (sonarr_url and sonarr_key) else None

    if not radarr:
        logger.info("Radarr not configured; auto-fix for movies disabled")
    if not sonarr:
        logger.info("Sonarr not configured; auto-fix for TV disabled")
    logger.info("Auto-fix allowlist size: %d", len(allowlist))

    app = Application.builder().token(token).post_init(_post_init).build()
    app.bot_data["seerr"] = seerr
    app.bot_data["store"] = store
    app.bot_data["radarr"] = radarr
    app.bot_data["sonarr"] = sonarr
    app.bot_data["allowlist"] = allowlist
    app.bot_data["admin_id"] = admin_id

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("link", cmd_link))
    app.add_handler(CommandHandler("unlink", cmd_unlink))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(_issue_conversation())
    app.add_error_handler(on_error)

    # Periodic poller for pending auto-fixes
    if radarr or sonarr:
        app.job_queue.run_repeating(
            poll_pending_autofixes,
            interval=60,
            first=30,
            name="autofix_poller",
        )

    return app


async def _post_init(app: Application) -> None:
    """Run startup health checks and try to DM the admin a welcome notice."""
    summary = await _check_connections(app)
    logger.info("Startup checks: %s", " | ".join(f"{k}={v}" for k, v in summary.items()))
    admin_id = app.bot_data["admin_id"]
    msg = (
        "👋 Bot is online.\n\n"
        f"{_format_status(summary)}\n\n"
        "If you haven't yet, DM me `/link <your-seerr-or-plex-username>` to link your account.\n"
        "Then `/issue` from here or any group I'm in."
    )
    try:
        await app.bot.send_message(chat_id=admin_id, text=msg, parse_mode="Markdown")
    except Exception:
        logger.info(
            "Couldn't DM admin %d on startup (likely never started a conversation with the bot). "
            "Admin should send /start to see the welcome.",
            admin_id,
        )


async def _check_connections(app: Application) -> dict[str, str]:
    """Probe configured services. Returns dict of service -> status string."""
    out: dict[str, str] = {}
    seerr: SeerrClient = app.bot_data["seerr"]
    try:
        r = await seerr._client.get("/status")
        r.raise_for_status()
        out["Seerr"] = f"✅ {r.json().get('version', 'ok')}"
    except Exception as exc:
        out["Seerr"] = f"❌ {exc}"
    radarr: Optional[RadarrClient] = app.bot_data.get("radarr")
    if radarr:
        try:
            r = await radarr._client.get("/system/status")
            r.raise_for_status()
            out["Radarr"] = f"✅ {r.json().get('version', 'ok')}"
        except Exception as exc:
            out["Radarr"] = f"❌ {exc}"
    else:
        out["Radarr"] = "— not configured"
    sonarr: Optional[SonarrClient] = app.bot_data.get("sonarr")
    if sonarr:
        try:
            r = await sonarr._client.get("/system/status")
            r.raise_for_status()
            out["Sonarr"] = f"✅ {r.json().get('version', 'ok')}"
        except Exception as exc:
            out["Sonarr"] = f"❌ {exc}"
    else:
        out["Sonarr"] = "— not configured"
    return out


def _format_status(summary: dict[str, str]) -> str:
    return "\n".join(f"  • *{k}*: {v}" for k, v in summary.items())


# --- Simple commands ---------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    admin_id = ctx.bot_data.get("admin_id")
    store: UserStore = ctx.bot_data["store"]
    is_admin = user_id == admin_id
    is_linked = store.get(user_id) is not None

    lines = ["Hi! I forward issue reports to Seerr."]
    if is_admin:
        summary = await _check_connections(ctx.application)
        lines.append("\n*Connection status:*")
        lines.append(_format_status(summary))
    if not is_linked:
        lines.append(
            "\nGet started by DMing me:\n"
            "  `/link <your seerr or plex username>`"
        )
    lines.append(
        "\n*Commands*\n"
        "  /link <username> — link your Telegram to Seerr (DM only)\n"
        "  /unlink — remove your link\n"
        "  /issue — report a problem with a movie or TV show\n"
        + ("  /status — connection diagnostics (admin only)\n" if is_admin else "")
        + "  /help — show this"
    )
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode="Markdown"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ctx.bot_data.get("admin_id"):
        await update.effective_message.reply_text("Admin only.")
        return
    summary = await _check_connections(ctx.application)
    await update.effective_message.reply_text(
        f"*Connection status:*\n{_format_status(summary)}",
        parse_mode="Markdown",
    )


async def cmd_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg.chat.type != ChatType.PRIVATE:
        await msg.reply_text("Please DM me to link your account.")
        return
    if not ctx.args:
        await msg.reply_text("Usage: /link <seerr-or-plex-username>")
        return
    query = " ".join(ctx.args).strip()
    seerr: SeerrClient = ctx.bot_data["seerr"]
    store: UserStore = ctx.bot_data["store"]
    user = await seerr.find_user(query)
    if user is None:
        await msg.reply_text(
            f"No Seerr user matched '{html.escape(query)}'. "
            "Try your Plex username or the name shown in Seerr."
        )
        return
    store.link(update.effective_user.id, user.id, user.display_name)
    await msg.reply_text(
        f"✅ Linked to Seerr user *{user.display_name}* (id={user.id}).",
        parse_mode="Markdown",
    )


async def cmd_unlink(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    store: UserStore = ctx.bot_data["store"]
    removed = store.unlink(update.effective_user.id)
    if removed:
        await update.effective_message.reply_text("🔓 Unlinked.")
    else:
        await update.effective_message.reply_text("You weren't linked.")


# --- Issue conversation ------------------------------------------------------

def _issue_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("issue", issue_start)],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, issue_title)],
            PICK_MEDIA: [CallbackQueryHandler(issue_pick_media, pattern=r"^media:")],
            PICK_SEASON: [CallbackQueryHandler(issue_pick_season, pattern=r"^season:")],
            PICK_EPISODE: [CallbackQueryHandler(issue_pick_episode, pattern=r"^ep:")],
            PICK_TYPE: [CallbackQueryHandler(issue_pick_type, pattern=r"^type:")],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, issue_description)],
            OFFER_AUTOFIX: [CallbackQueryHandler(issue_offer_autofix, pattern=r"^autofix:")],
            CONFIRM_AUTOFIX: [CallbackQueryHandler(issue_confirm_autofix, pattern=r"^confirm:")],
        },
        fallbacks=[
            CommandHandler("cancel", issue_cancel),
            CallbackQueryHandler(issue_cancel, pattern=r"^cancel$"),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
        name="issue",
        persistent=False,
    )


async def issue_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    store: UserStore = ctx.bot_data["store"]
    if store.get(update.effective_user.id) is None:
        await update.effective_message.reply_text(
            "You need to link your Seerr account first. DM me /link <username>."
        )
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "What movie or show is the issue with? (Reply with the title.)"
    )
    return TITLE


async def issue_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.effective_message.text.strip()
    seerr: SeerrClient = ctx.bot_data["seerr"]
    try:
        results = await seerr.search(query, limit=5)
    except Exception as exc:
        logger.exception("search failed")
        await update.effective_message.reply_text(f"Search failed: {exc}")
        return ConversationHandler.END
    if not results:
        await update.effective_message.reply_text(
            "No matches. Try a different title, or /cancel."
        )
        return TITLE
    rows = []
    for r in results:
        emoji = "🎬" if r.media_type == "movie" else "📺"
        label = f"{emoji} {r.title}" + (f" ({r.year})" if r.year else "")
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"media:{r.media_type}:{r.tmdb_id}")])
    rows.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    ctx.user_data["search_results"] = {(r.media_type, r.tmdb_id): r for r in results}
    await update.effective_message.reply_text(
        "Pick which one:",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return PICK_MEDIA


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
        await q.edit_message_text(
            "That title isn't in Seerr's library yet (no Plex match / no prior request). "
            "Request it via Seerr first, then come back. /issue to start over."
        )
        return ConversationHandler.END
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
        await q.edit_message_text(f"Couldn't fetch seasons: {exc}")
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
        row.append(InlineKeyboardButton(f"S{s.season_number}", callback_data=f"season:{s.season_number}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
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
        row.append(InlineKeyboardButton(f"E{ep}", callback_data=f"ep:{ep}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("📦 Whole season", callback_data="ep:0")])
    rows.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
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
        InlineKeyboardButton(f"{e} {n}", callback_data=f"type:{i}")
        for i, (e, n) in ISSUE_TYPES.items()
    ]]
    rows.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
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
    allowlist: set[int] = ctx.bot_data.get("allowlist") or set()
    store: UserStore = ctx.bot_data["store"]
    tg_id = update.effective_user.id
    eligible = (
        issue_type in AUTOFIX_ELIGIBLE_TYPES
        and tg_id in allowlist
        and _has_arr_for_media(ctx)
    )
    if not eligible:
        return await _submit_issue(update, ctx, autofix=False)
    used = store.count_autofix_24h(tg_id)
    if used >= DAILY_AUTOFIX_LIMIT:
        await update.effective_message.reply_text(
            f"(You've used your {DAILY_AUTOFIX_LIMIT} auto-fixes today; "
            f"submitting issue without auto-fix.)"
        )
        return await _submit_issue(update, ctx, autofix=False)
    rows = [[
        InlineKeyboardButton("✅ Try auto-fix", callback_data="autofix:yes"),
        InlineKeyboardButton("📨 Just report", callback_data="autofix:no"),
    ]]
    remaining = DAILY_AUTOFIX_LIMIT - used
    await update.effective_message.reply_text(
        f"Try to auto-fix? This will delete the file and trigger a new search.\n"
        f"(Auto-fixes remaining today: {remaining})",
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
        InlineKeyboardButton("⚠️ Yes, delete & re-search", callback_data="confirm:yes"),
        InlineKeyboardButton("No, just report", callback_data="confirm:no"),
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

    mapping = store.get(update.effective_user.id)
    if not (mapping and issue_type and media):
        await update.effective_message.reply_text("Lost conversation state. /issue to start over.")
        return ConversationHandler.END

    tg_name = update.effective_user.full_name or update.effective_user.username or "Telegram user"
    full_message = f"[from Telegram: {tg_name} ↔ {mapping.seerr_display}]\n\n{description}"
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
        )
    except Exception as exc:
        logger.exception("create_issue failed")
        await update.effective_message.reply_text(f"Failed to create issue: {exc}")
        return ConversationHandler.END

    emoji, name = ISSUE_TYPES[issue_type]
    label = media["title"] + (f" ({media['year']})" if media.get("year") else "")
    if media["type"] == "tv":
        label += f" — S{season}E{episode}" if episode else f" — S{season} (whole season)"

    lines = [
        f"✅ Reported as issue #{created.id}",
        f"  {emoji} {name} — {label}",
    ]

    # 2. If auto-fix requested, run it
    if autofix:
        ok, detail, poll_info = await _run_autofix(media, season, episode, radarr, sonarr)
        if ok:
            store.log_autofix(
                update.effective_user.id,
                media["type"],
                media["tmdb_id"],
                season=season,
                episode=episode,
            )
            # Enqueue notification-tracking record
            try:
                kwargs = {
                    "chat_id": update.effective_chat.id,
                    "user_id": update.effective_user.id,
                    "media_type": media["type"],
                    "label": label,
                    "issue_id": created.id,
                    "issue_url": created.url,
                }
                if media["type"] == "movie" and poll_info:
                    kwargs["radarr_movie_id"] = poll_info.get("movie_id")
                elif media["type"] == "tv" and poll_info:
                    kwargs["sonarr_series_id"] = poll_info.get("series_id")
                    kwargs["sonarr_episode_id"] = poll_info.get("episode_id")
                    kwargs["sonarr_season"] = poll_info.get("season")
                    kwargs["expected_episode_ids"] = poll_info.get("expected_episode_ids") or []
                store.add_pending_autofix(**kwargs)
                lines.append(f"🔧 Auto-fix: {detail}")
                lines.append("🔔 I'll DM you when the new file finishes downloading (or after 6h timeout).")
            except Exception:
                logger.exception("failed to enqueue pending autofix")
                lines.append(f"🔧 Auto-fix: {detail}")
                lines.append("(Couldn't enqueue completion notification.)")
        else:
            lines.append(f"⚠️ Auto-fix didn't run: {detail}")

    lines.append(f"\nView: {created.url}")
    await update.effective_message.reply_text("\n".join(lines))
    ctx.user_data.clear()
    return ConversationHandler.END


async def _run_autofix(
    media: dict,
    season: Optional[int],
    episode: Optional[int],
    radarr: Optional[RadarrClient],
    sonarr: Optional[SonarrClient],
) -> tuple[bool, str, Optional[dict]]:
    """Returns (ok, message, poll_info). poll_info on success has keys
    needed to track completion: movie_id for movies; series_id +
    (episode_id or season + expected_episode_ids) for TV.
    """
    try:
        if media["type"] == "movie":
            if not radarr:
                return False, "Radarr not configured.", None
            ok, msg, movie_id = await radarr.auto_fix(media["tmdb_id"])
            return ok, msg, ({"movie_id": movie_id} if ok else None)
        if media["type"] == "tv":
            if not sonarr:
                return False, "Sonarr not configured.", None
            tvdb_id = media.get("tvdb_id")
            if not tvdb_id:
                return False, "Couldn't find TVDb ID for this show.", None
            if episode:
                return await sonarr.auto_fix_episode(tvdb_id, season, episode)
            return await sonarr.auto_fix_season(tvdb_id, season)
    except Exception as exc:
        logger.exception("auto_fix failed")
        return False, str(exc), None
    return False, "Unknown media type.", None


async def issue_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer("Cancelled")
        await update.callback_query.edit_message_text("Cancelled. /issue to start over.")
    else:
        await update.effective_message.reply_text("Cancelled. /issue to start over.")
    ctx.user_data.clear()
    return ConversationHandler.END


# --- Pending autofix poller --------------------------------------------------

async def poll_pending_autofixes(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Check on each pending auto-fix; notify when complete or timed out."""
    store: UserStore = ctx.bot_data["store"]
    radarr: Optional[RadarrClient] = ctx.bot_data.get("radarr")
    sonarr: Optional[SonarrClient] = ctx.bot_data.get("sonarr")
    pending = store.list_pending_autofixes()
    if not pending:
        return
    logger.debug("Polling %d pending auto-fixes", len(pending))
    from datetime import datetime, timezone

    for fix in pending:
        # Check timeout first
        try:
            timeout_at = datetime.fromisoformat(fix.timeout_at.replace(" ", "T")).replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= timeout_at:
                await _notify_timeout(ctx, fix)
                store.mark_autofix_status(fix.id, "timeout")
                continue
        except Exception:
            logger.exception("timeout parse failed for fix %d", fix.id)

        # Poll for completion
        try:
            done = False
            extra = ""
            if fix.media_type == "movie" and radarr and fix.radarr_movie_id:
                done = await radarr.movie_has_file(fix.radarr_movie_id)
            elif fix.media_type == "tv" and sonarr:
                if fix.sonarr_episode_id:
                    done = await sonarr.episode_has_file(fix.sonarr_episode_id)
                elif fix.sonarr_series_id and fix.sonarr_season and fix.expected_episode_ids:
                    present, total = await sonarr.season_files_present(
                        fix.sonarr_series_id, fix.sonarr_season, fix.expected_episode_ids
                    )
                    done = present >= total and total > 0
                    extra = f" ({present}/{total} episodes)"
            if done:
                await _notify_complete(ctx, fix, extra)
                store.mark_autofix_status(fix.id, "complete")
        except Exception:
            logger.exception("poll failed for fix %d", fix.id)


async def _notify_complete(ctx: ContextTypes.DEFAULT_TYPE, fix, extra: str = "") -> None:
    text = (
        f"🎉 Auto-fix complete: *{fix.label}* downloaded{extra}.\n"
        f"Original issue: {fix.issue_url}"
    )
    try:
        await ctx.bot.send_message(chat_id=fix.chat_id, text=text, parse_mode="Markdown")
    except Exception:
        logger.exception("notify_complete send_message failed for fix %d", fix.id)


async def _notify_timeout(ctx: ContextTypes.DEFAULT_TYPE, fix) -> None:
    text = (
        f"⏱️ Auto-fix timed out (6h) for *{fix.label}*.\n"
        f"No new file was imported. Check Sonarr/Radarr to see if a release was grabbed.\n"
        f"Original issue: {fix.issue_url}"
    )
    try:
        await ctx.bot.send_message(chat_id=fix.chat_id, text=text, parse_mode="Markdown")
    except Exception:
        logger.exception("notify_timeout send_message failed for fix %d", fix.id)


# --- Error handler -----------------------------------------------------------

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: %s", ctx.error)


# --- Main --------------------------------------------------------------------

def main() -> None:
    for var in ("TELEGRAM_BOT_TOKEN", "SEERR_URL", "SEERR_API_KEY", "ADMIN_TELEGRAM_ID"):
        if not os.environ.get(var):
            print(f"Missing required env var: {var}", file=sys.stderr)
            sys.exit(1)
    try:
        int(os.environ["ADMIN_TELEGRAM_ID"])
    except ValueError:
        print("ADMIN_TELEGRAM_ID must be an integer Telegram user ID.", file=sys.stderr)
        sys.exit(1)
    app = _build_app()
    logger.info("Starting bot (polling)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
