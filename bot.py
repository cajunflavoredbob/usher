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
import secrets
import sys
from pathlib import Path
from typing import Final, Optional

import telegram
from telegram import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

import asyncio

from aiohttp import web

from seerr import SeerrClient, CreatedIssue, IssueListItem
from store import UserStore, TokenCrypto
from radarr import RadarrClient
from sonarr import SonarrClient
from plex import PlexClient
from webhook import attach_webhook, start_http_server
from webui import attach_webui
from settings import SettingsStore, load_or_create_session_secret
from fix_result import FixResult
from http_util import (
    APIError,
    NotFoundAPIError,
    TransientAPIError,
    user_friendly_message,
)
from _version import __version__ as HERMES_VERSION

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("hermes")

# Conversation states (issue creation)
TITLE, PICK_MEDIA, PICK_SEASON, PICK_EPISODE, PICK_TYPE, DESCRIPTION, OFFER_AUTOFIX, CONFIRM_AUTOFIX = range(8)
# Conversation states (post-completion follow-up)
AWAIT_COMMENT = 100
# Conversation states (Plex link flow)
AWAIT_LINK_CONSENT = 200
AWAIT_PLATFORM_CHOICE = 201
# Conversation states (ticket management)
AWAIT_TICKET_REPLY = 400

ISSUE_TYPES: Final = {
    1: ("🎥", "Video"),
    2: ("🔊", "Audio"),
    3: ("📝", "Subtitles"),
    4: ("❓", "Other"),
}

AUTOFIX_ELIGIBLE_TYPES = {1, 2, 3}


def _schedule_clean_exit(delay_s: float = 2.0) -> None:
    """Send SIGTERM to self after `delay_s` so PTB's run_polling and aiohttp's
    runner unwind cleanly (closing httpx clients, DB connections, the HTTP
    server). Falls back to os._exit only if the SIGTERM dispatch itself fails.
    """
    import signal
    loop = asyncio.get_running_loop()
    def _kill():
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception:
            logger.exception("SIGTERM dispatch failed; falling back to os._exit")
            os._exit(0)
    loop.call_later(delay_s, _kill)


# --- App setup ---------------------------------------------------------------


def _build_clients_from_settings(app: Application) -> None:
    """(Re)build Seerr/Radarr/Sonarr clients and update the allowlist + webhook
    secret from the current SettingsStore. Used at startup AND on hot reload.

    Closes any prior httpx clients so we don't leak connections.
    """
    settings_store: SettingsStore = app.bot_data["settings_store"]
    s = settings_store.settings
    admin_id: int = app.bot_data["admin_id"]

    # Capture references to the OLD clients BEFORE swapping. If we scheduled
    # a close-task that read bot_data lazily, it would see the NEW clients by
    # the time it ran (race) and close those instead. Capture-then-close is
    # the only safe order.
    old_clients: list[tuple[str, object]] = []
    for key in ("seerr", "radarr", "sonarr"):
        c = app.bot_data.get(key)
        if c is not None and hasattr(c, "close"):
            old_clients.append((key, c))

    seerr = SeerrClient(
        s.seerr_url, s.seerr_api_key,
        public_url=s.seerr_public_url or None,
    ) if (s.seerr_url and s.seerr_api_key) else None
    radarr = RadarrClient(s.radarr_url, s.radarr_api_key) if (s.radarr_url and s.radarr_api_key) else None
    sonarr = SonarrClient(s.sonarr_url, s.sonarr_api_key) if (s.sonarr_url and s.sonarr_api_key) else None

    allowlist = set(s.allowed_autofix_telegram_ids)
    if not allowlist:
        allowlist = {admin_id}

    app.bot_data["seerr"] = seerr
    app.bot_data["radarr"] = radarr
    app.bot_data["sonarr"] = sonarr
    app.bot_data["allowlist"] = allowlist

    # Now close the captured old clients (no race -- bot_data already holds the new ones)
    if old_clients:
        async def _close_old() -> None:
            for key, client in old_clients:
                try:
                    await client.close()  # type: ignore[attr-defined]
                except Exception:
                    logger.exception("Error closing prior %s client", key)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_close_old())
        except RuntimeError:
            # No loop running (startup path) -- nothing to close anyway since
            # old_clients was populated from bot_data which would be empty.
            pass

    logger.info(
        "Clients (re)built: Seerr=%s Radarr=%s Sonarr=%s allowlist=%d",
        "yes" if seerr else "no",
        "yes" if radarr else "no",
        "yes" if sonarr else "no",
        len(allowlist),
    )


def _build_app(settings_store: SettingsStore, session_secret: bytes, user_store: UserStore,
               plex: PlexClient, data_dir: Path, settings_path: Path, db_path: str,
               http_port: int, http_bind: str) -> Application:
    s = settings_store.settings
    token = s.telegram_bot_token
    admin_id = s.admin_telegram_id

    app = (
        Application.builder()
        .token(token)
        .concurrent_updates(True)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data["settings_store"] = settings_store
    app.bot_data["session_secret"] = session_secret
    app.bot_data["data_dir"] = data_dir
    app.bot_data["settings_path"] = settings_path
    app.bot_data["db_path"] = db_path
    app.bot_data["store"] = user_store
    app.bot_data["plex"] = plex
    app.bot_data["admin_id"] = admin_id
    app.bot_data["http_port"] = http_port
    app.bot_data["http_bind"] = http_bind

    _build_clients_from_settings(app)

    # Global gate: drop callbacks from stale button-bearing messages. Group -1
    # so it fires before any normal handler.
    app.add_handler(TypeHandler(Update, _global_btn_gate), group=-1)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(_link_conversation())
    app.add_handler(CommandHandler("unlink", cmd_unlink))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("tickets", cmd_tickets))
    app.add_handler(_issue_conversation())
    app.add_handler(_resolve_conversation())
    # Ticket-management callbacks (must be registered before the conversation
    # so the non-conversation taps -- open / close-menu / close-direct -- work
    # even when no conversation is active)
    app.add_handler(CallbackQueryHandler(tk_open, pattern=r"^tkopen:\d+$"))
    app.add_handler(CallbackQueryHandler(tk_reply_menu, pattern=r"^tkrmenu:\d+$"))
    app.add_handler(CallbackQueryHandler(tk_close_menu, pattern=r"^tkc:\d+$"))
    app.add_handler(CallbackQueryHandler(tk_close_direct, pattern=r"^tkcd:\d+$"))
    app.add_handler(CallbackQueryHandler(tk_fix, pattern=r"^tkf:\d+$"))
    app.add_handler(CallbackQueryHandler(tk_fix_redownload, pattern=r"^tkfd:\d+$"))
    app.add_handler(CallbackQueryHandler(tk_fix_mark_failed, pattern=r"^tkfm:\d+$"))
    app.add_handler(CallbackQueryHandler(tk_back, pattern=r"^tkback:\d+$"))
    app.add_handler(_ticket_conversation())
    # Link "Didn't work?" / "Having trouble?" callback. Fires outside the
    # link ConversationHandler so it can interrupt an in-progress poll.
    app.add_handler(CallbackQueryHandler(cmd_link_didnt_work, pattern=r"^tklhelp$"))
    app.add_error_handler(on_error)

    # Periodic poller for pending auto-fixes (runs only when needed -- the job
    # itself bails out cleanly if Radarr/Sonarr aren't configured at tick time)
    app.job_queue.run_repeating(
        poll_pending_autofixes,
        interval=60,
        first=30,
        name="autofix_poller",
    )

    return app


async def _post_init(app: Application) -> None:
    """Run startup checks, start the HTTP server (webhook + webui), DM admin."""
    summary = await _check_connections(app)
    logger.info("Startup checks: %s", " | ".join(f"{k}={v}" for k, v in summary.items()))

    # Build a single aiohttp app that serves both webhook and webui
    web_app = web.Application(client_max_size=32 * 1024 * 1024)  # 32 MB for backup restores

    async def _on_comment(payload: dict) -> None:
        await handle_seerr_comment(app, payload)

    async def _on_resolved(payload: dict) -> None:
        await handle_seerr_resolved(app, payload)

    async def _on_reported(payload: dict) -> None:
        await handle_seerr_reported(app, payload)

    def _secret_provider() -> str:
        settings_store: SettingsStore = app.bot_data["settings_store"]
        return settings_store.settings.webhook_secret or ""

    # Capture the bootstrap values so we can detect post-save changes that
    # require a container restart (bot token, admin id).
    settings_store: SettingsStore = app.bot_data["settings_store"]
    boot_token = settings_store.settings.telegram_bot_token
    boot_admin_id = settings_store.settings.admin_telegram_id

    async def _on_settings_changed() -> None:
        s = settings_store.settings
        if s.telegram_bot_token != boot_token or s.admin_telegram_id != boot_admin_id:
            logger.info("Bot token or admin id changed; exiting in 2s to restart")
            _schedule_clean_exit(2.0)
            return
        logger.info("Settings changed; rebuilding clients")
        _build_clients_from_settings(app)

    attach_webhook(
        web_app,
        on_comment=_on_comment,
        on_resolved=_on_resolved,
        on_reported=_on_reported,
        secret_provider=_secret_provider,
    )
    attach_webui(
        web_app,
        settings_store=app.bot_data["settings_store"],
        session_secret=app.bot_data["session_secret"],
        data_dir=app.bot_data["data_dir"],
        settings_path=app.bot_data["settings_path"],
        db_path=Path(app.bot_data["db_path"]),
        on_settings_changed=_on_settings_changed,
    )

    runner = await start_http_server(
        web_app,
        host=app.bot_data["http_bind"],
        port=app.bot_data["http_port"],
    )
    app.bot_data["http_runner"] = runner

    admin_id = app.bot_data["admin_id"]
    settings_store: SettingsStore = app.bot_data["settings_store"]
    base = (settings_store.settings.hermes_public_url or "").strip().rstrip("/")
    if base:
        # Tolerate users pasting in the full /admin URL
        if base.endswith("/admin"):
            base = base[: -len("/admin")]
        admin_url = f"{base}/admin"
    else:
        admin_url = f"http://<host>:{app.bot_data['http_port']}/admin"
    msg = (
        "👋 Bot is online.\n\n"
        f"{_format_status(summary)}\n\n"
        f"Admin UI: {admin_url}\n"
        "Run `/link` to authorize with Plex (per-user issue attribution)."
    )
    try:
        await app.bot.send_message(chat_id=admin_id, text=msg, parse_mode="Markdown")
    except Exception:
        logger.info(
            "Couldn't DM admin %d on startup (likely never started a conversation with the bot). "
            "Admin should send /start to see the welcome.",
            admin_id,
        )

    # If the encryption key rotated, any user_mapping rows with a stored
    # ciphertext will no longer decrypt. Alert the admin so they know which
    # linked users will need to /unlink and /link again.
    store: UserStore = app.bot_data["store"]
    try:
        n_failed = await store.count_decrypt_failures()
    except Exception:
        logger.exception("count_decrypt_failures failed at startup")
        n_failed = 0
    if n_failed:
        plural = "s" if n_failed != 1 else ""
        warn = (
            f"⚠️ {n_failed} stored Plex link{plural} can't be decrypted with the "
            "current encryption key. Affected users will see a 'link broken' "
            "message and need to /unlink + /link again. Likely cause: the "
            "encryption key rotated or HERMES_ENCRYPTION_KEY changed."
        )
        try:
            await app.bot.send_message(chat_id=admin_id, text=warn)
        except Exception:
            logger.warning("Couldn't DM admin %d about %d decrypt failure(s)", admin_id, n_failed)


async def _post_shutdown(app: Application) -> None:
    runner = app.bot_data.get("http_runner")
    if runner is not None:
        try:
            await runner.cleanup()
            logger.info("HTTP server stopped")
        except Exception:
            logger.exception("HTTP server cleanup failed")


async def handle_seerr_comment(app: Application, payload: dict) -> None:
    """Process an ISSUE_COMMENT webhook from Seerr and DM the reporter."""
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
    if commenter_username and commenter_username.lower() == reporter_username.lower():
        # Don't echo the user's own comment back at them
        logger.info("Webhook comment on issue #%d: commenter == reporter; skipping", issue_id)
        return
    if not comment_text:
        logger.info("Webhook comment on issue #%d: empty comment; dropping", issue_id)
        return

    store: UserStore = app.bot_data["store"]
    mapping = await store.find_by_plex_username(reporter_username)
    if mapping is None:
        logger.info(
            "Webhook comment on issue #%d: reporter '%s' not linked in Hermes; dropping",
            issue_id, reporter_username,
        )
        return

    # Fetch media context for the message (best-effort)
    title_line = ""
    seerr: SeerrClient = app.bot_data["seerr"]
    media_type = media.get("media_type") or ""
    tmdb_raw = media.get("tmdbId")
    try:
        tmdb_id = int(tmdb_raw) if tmdb_raw not in (None, "") else 0
    except (TypeError, ValueError):
        tmdb_id = 0
    if media_type in ("movie", "tv") and tmdb_id:
        try:
            title, year = await seerr.get_media_title(media_type, tmdb_id)
            emoji = "🎬" if media_type == "movie" else "📺"
            title_line = f"{emoji} {title}"
            if year:
                title_line += f" ({year})"
            se_bits = []
            if issue.get("problemSeason") is not None:
                try:
                    s = int(issue["problemSeason"])
                    e_raw = issue.get("problemEpisode")
                    e = int(e_raw) if e_raw not in (None, "") else None
                    se_bits.append(f"S{s:02d}E{e:02d}" if e else f"S{s:02d}")
                except (TypeError, ValueError):
                    pass
            if se_bits:
                title_line += " — " + " ".join(se_bits)
        except Exception:
            logger.exception("Failed to fetch media title for issue #%d", issue_id)

    safe_comment = html.escape(comment_text)
    safe_commenter = html.escape(commenter_username or "Seerr")
    safe_title = html.escape(title_line) if title_line else ""

    lines = [f"💬 New comment on issue #{issue_id}"]
    if safe_title:
        lines.append(safe_title)
    lines.append("")
    lines.append(f"<b>From:</b> {safe_commenter}")
    lines.append("")
    lines.append(f"<b>Comment:</b>")
    lines.append(f"<i>\"{safe_comment}\"</i>")

    # Offer an inline Reply button when the ticket is still open
    issue_status = (issue.get("issue_status") or "").upper()
    reply_kb = None
    if issue_status == "OPEN":
        reply_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("💬 Reply", callback_data=f"tkr:{issue_id}"),
        ]])

    try:
        sent = await app.bot.send_message(
            chat_id=mapping.telegram_id,
            text="\n".join(lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_kb,
        )
        if reply_kb is not None:
            _record_btn(app, mapping.telegram_id, sent)
        logger.info(
            "Notified telegram_id=%d of comment on issue #%d from '%s'",
            mapping.telegram_id, issue_id, commenter_username,
        )
    except Exception:
        logger.exception(
            "Failed to DM telegram_id=%d about issue #%d comment",
            mapping.telegram_id, issue_id,
        )


async def handle_seerr_resolved(app: Application, payload: dict) -> None:
    """Process an ISSUE_RESOLVED webhook from Seerr and DM the reporter."""
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
            "Webhook resolved on issue #%d: reporter '%s' not linked in Hermes",
            issue_id, reporter_username,
        )

    # Best-effort media context for a richer message
    title_line = ""
    seerr: Optional[SeerrClient] = app.bot_data.get("seerr")
    media_type = media.get("media_type") or ""
    tmdb_raw = media.get("tmdbId")
    try:
        tmdb_id = int(tmdb_raw) if tmdb_raw not in (None, "") else 0
    except (TypeError, ValueError):
        tmdb_id = 0
    if seerr and media_type in ("movie", "tv") and tmdb_id:
        try:
            title, year = await seerr.get_media_title(media_type, tmdb_id)
            emoji = "🎬" if media_type == "movie" else "📺"
            title_line = f"{emoji} {title}"
            if year:
                title_line += f" ({year})"
            if issue.get("problemSeason") is not None:
                try:
                    s = int(issue["problemSeason"])
                    e_raw = issue.get("problemEpisode")
                    e = int(e_raw) if e_raw not in (None, "") else None
                    title_line += " — " + (f"S{s:02d}E{e:02d}" if e else f"S{s:02d}")
                except (TypeError, ValueError):
                    pass
        except Exception:
            logger.exception("Failed to fetch media title for resolved #%d", issue_id)

    safe_title = html.escape(title_line) if title_line else ""
    safe_reporter = html.escape(reporter_username)
    admin_id = app.bot_data.get("admin_id")

    # DM the reporter (if they're linked)
    if mapping is not None:
        reporter_lines = [f"✅ Your issue #{issue_id} was resolved."]
        if safe_title:
            reporter_lines.append(safe_title)
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
        admin_lines = [f"✅ Issue #{issue_id} resolved"]
        if safe_title:
            admin_lines.append(safe_title)
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
            logger.exception(
                "Failed to DM admin about resolved issue #%d",
                issue_id,
            )

# Maps Seerr's issueType enum string to a (emoji, label) pair.
_ISSUE_TYPE_LABELS = {
    "VIDEO": ("🎥", "Video"),
    "AUDIO": ("🔊", "Audio"),
    "SUBTITLES": ("📝", "Subtitle"),
    "SUBTITLE": ("📝", "Subtitle"),
    "OTHER": ("❓", "Other"),
}


async def handle_seerr_reported(app: Application, payload: dict) -> None:
    """Process an ISSUE_REPORTED webhook and DM the admin (if admin didn't
    file it themselves)."""
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

    # Build media title line
    seerr: Optional[SeerrClient] = app.bot_data.get("seerr")
    media_type = media.get("media_type") or ""
    try:
        tmdb_id = int(media.get("tmdbId") or 0)
    except (TypeError, ValueError):
        tmdb_id = 0
    title_line = ""
    if seerr and media_type in ("movie", "tv") and tmdb_id:
        try:
            title, year = await seerr.get_media_title(media_type, tmdb_id)
            emoji = "🎬" if media_type == "movie" else "📺"
            title_line = f"{emoji} {title}"
            if year:
                title_line += f" ({year})"
        except Exception:
            logger.exception("Failed to fetch media title for reported #%d", issue_id)

    season_raw = issue.get("problemSeason")
    episode_raw = issue.get("problemEpisode")
    if title_line and season_raw is not None:
        try:
            s = int(season_raw)
            e = int(episode_raw) if episode_raw not in (None, "") else None
            title_line += f" — S{s:02d}E{e:02d}" if e else f" — S{s:02d}"
        except (TypeError, ValueError):
            pass

    issue_type_str = (issue.get("issue_type") or "OTHER").upper()
    type_emoji, type_label = _ISSUE_TYPE_LABELS.get(issue_type_str, ("❓", "Other"))

    safe_reporter = html.escape(reporter_username)
    safe_desc = html.escape(description) if description else "(no description)"
    safe_title = html.escape(title_line) if title_line else "(unknown media)"

    lines = [
        f"🆕 New issue <b>#{issue_id}</b>",
        "",
        safe_title,
        "",
        f"<b>Issue type:</b> {type_emoji} {type_label}",
        f"<b>Reported by:</b> {safe_reporter}",
        f"<b>Status:</b> Open",
        "",
        f"<b>Description:</b>",
        f"<i>\"{safe_desc}\"</i>",
    ]

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 Reply", callback_data=f"tkr:{issue_id}"),
        InlineKeyboardButton("🔧 Fix", callback_data=f"tkf:{issue_id}"),
        InlineKeyboardButton("✅ Close", callback_data=f"tkc:{issue_id}"),
    ]])

    try:
        sent = await app.bot.send_message(
            chat_id=admin_id,
            text="\n".join(lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb,
        )
        _record_btn(app, admin_id, sent)
        logger.info("Notified admin of new issue #%d from '%s'", issue_id, reporter_username)
    except Exception:
        logger.exception("Failed to DM admin about new issue #%d", issue_id)


async def _check_connections(app: Application) -> dict[str, str]:
    """Probe configured services. Returns dict of service -> status string."""
    out: dict[str, str] = {"Hermes": f"✅ {HERMES_VERSION}"}
    seerr: Optional[SeerrClient] = app.bot_data.get("seerr")
    if seerr is None:
        out["Seerr"] = "— not configured"
    else:
        try:
            r = await seerr._client.get("/status")
            r.raise_for_status()
            out["Seerr"] = f"✅ {r.json().get('version', 'ok')}"
        except Exception as exc:
            out["Seerr"] = f"❌ {user_friendly_message(exc)}"
    radarr: Optional[RadarrClient] = app.bot_data.get("radarr")
    if radarr:
        try:
            r = await radarr._client.get("/system/status")
            r.raise_for_status()
            out["Radarr"] = f"✅ {r.json().get('version', 'ok')}"
        except Exception as exc:
            out["Radarr"] = f"❌ {user_friendly_message(exc)}"
    else:
        out["Radarr"] = "— not configured"
    sonarr: Optional[SonarrClient] = app.bot_data.get("sonarr")
    if sonarr:
        try:
            r = await sonarr._client.get("/system/status")
            r.raise_for_status()
            out["Sonarr"] = f"✅ {r.json().get('version', 'ok')}"
        except Exception as exc:
            out["Sonarr"] = f"❌ {user_friendly_message(exc)}"
    else:
        out["Sonarr"] = "— not configured"
    return out


def _format_status(summary: dict[str, str]) -> str:
    return "\n".join(f"  • *{k}*: {v}" for k, v in summary.items())


async def _require_seerr(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Optional[SeerrClient]:
    """Bail out gracefully if Seerr isn't configured yet."""
    seerr: Optional[SeerrClient] = ctx.bot_data.get("seerr")
    if seerr is None:
        port = ctx.bot_data.get("http_port", 8765)
        await update.effective_message.reply_text(
            f"Hermes isn't configured yet. The admin needs to fill in Seerr settings at "
            f"http://<host>:{port}/admin",
        )
    return seerr


# --- Simple commands ---------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    admin_id = ctx.bot_data.get("admin_id")
    store: UserStore = ctx.bot_data["store"]
    is_admin = user_id == admin_id
    is_linked = (await store.get(user_id)) is not None

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
        "  /link — sign in with Plex (DM only)\n"
        "  /unlink — remove your link\n"
        "  /issue — report a problem with a movie or TV show\n"
        "  /tickets — list your open tickets\n"
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


def _format_age(created_at_iso: str) -> str:
    from datetime import datetime, timezone
    try:
        created = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    delta = datetime.now(timezone.utc) - created
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    if secs < 7 * 86400:
        return f"{secs // 86400}d ago"
    return created.strftime("%Y-%m-%d")


async def cmd_tickets(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    seerr = await _require_seerr(update, ctx)
    if seerr is None:
        return
    user_id = update.effective_user.id
    admin_id = ctx.bot_data.get("admin_id")
    is_admin = user_id == admin_id
    store: UserStore = ctx.bot_data["store"]
    mapping = await store.get(user_id)

    # Eligibility check for non-admin users
    if not is_admin:
        if not mapping or not mapping.plex_token:
            await update.effective_message.reply_text(
                "DM me /link first so I know which Plex account is yours."
            )
            return

    # Fetch issues
    try:
        issues = await seerr.list_issues(
            filter="open",
            take=25,
            as_plex_token=None if is_admin else mapping.plex_token,
        )
    except Exception as exc:
        logger.exception("list_issues failed")
        await update.effective_message.reply_text(f"Couldn't fetch tickets. {user_friendly_message(exc)}")
        return

    if not issues:
        await update.effective_message.reply_text(
            ("No open tickets across all users. 🎉" if is_admin else "No open tickets! 🎉")
        )
        return

    # Resolve media titles in parallel
    title_tasks = [seerr.get_media_title(i.media_type, i.tmdb_id) for i in issues]
    title_results = await asyncio.gather(*title_tasks, return_exceptions=True)

    header = f"📋 {'All open tickets' if is_admin else 'Your open tickets'} ({len(issues)}):"
    lines = [header, ""]
    for issue, tr in zip(issues, title_results):
        emoji, _ = ISSUE_TYPES.get(issue.issue_type, ("❓", "Other"))
        if isinstance(tr, Exception):
            media_label = f"TMDb {issue.tmdb_id}"
        else:
            title, year = tr
            media_label = title + (f" ({year})" if year else "")
        if issue.media_type == "tv" and issue.problem_season:
            if issue.problem_episode:
                media_label += f" S{int(issue.problem_season):02d}E{int(issue.problem_episode):02d}"
            else:
                media_label += f" S{int(issue.problem_season):02d}"
        age = _format_age(issue.created_at)
        line = f"#{issue.id} {emoji} {media_label} — {age}"
        if is_admin and issue.created_by:
            line += f" — {issue.created_by}"
        lines.append(line)
    lines.append("")
    lines.append("Tap a ticket number below to manage it.")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n…(truncated)"

    # Inline keyboard: one button per ticket (#N), 4 per row
    button_rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []
    for issue in issues:
        current.append(InlineKeyboardButton(f"#{issue.id}", callback_data=f"tkopen:{issue.id}"))
        if len(current) == 4:
            button_rows.append(current)
            current = []
    if current:
        button_rows.append(current)

    msg = await update.effective_message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(button_rows) if button_rows else None,
    )
    _record_btn(ctx.application, update.effective_user.id, msg)


# --- Ticket management (reply / close from inside Telegram) ----------------

async def _edit_or_send(q, text: str, **kwargs) -> None:
    """Edit the callback's message; if Telegram rejects (e.g., the user
    edited or deleted the source message), send a new message in the same
    chat so the response isn't silently dropped.
    """
    try:
        await q.edit_message_text(text, **kwargs)
        return
    except telegram.error.BadRequest:
        pass
    except Exception:
        logger.exception("edit_message_text failed unexpectedly; falling back to send")
    try:
        await q.message.reply_text(text, **kwargs)
    except Exception:
        logger.exception("reply_text fallback also failed")


async def _token_for(ctx: ContextTypes.DEFAULT_TYPE, tg_id: int) -> tuple[bool, Optional[str], bool]:
    """Return (is_admin, plex_token_or_None, decrypt_failed).

    For admin, token is None (we want admin-key attribution via SeerrClient
    bare _client). For non-admin, token is their Plex token; caller must
    bail if it's None (user isn't linked) OR distinguish decrypt_failed=True
    (link exists but the encryption key changed) so the user can be told to
    re-run /link.
    """
    admin_id = ctx.bot_data.get("admin_id")
    if tg_id == admin_id:
        return True, None, False
    store: UserStore = ctx.bot_data["store"]
    mapping = await store.get(tg_id)
    if mapping is None:
        return False, None, False
    if mapping.plex_token_decrypt_failed:
        return False, None, True
    if not mapping.plex_token:
        return False, None, False
    return False, mapping.plex_token, False


# --- Interactive-button bookkeeping ----------------------------------------
# Maximum age before a button-bearing message's buttons stop working.
BTN_TTL_SECONDS = 6 * 3600  # 6 hours


def _record_btn(app, user_id: int, message) -> None:
    """Record `message` as the most recent button-bearing bot message for `user_id`.
    Used by the global button gate to dismiss callbacks from older messages."""
    from datetime import datetime, timezone
    if message is None:
        return
    app.bot_data.setdefault("btn_msgs", {})[user_id] = {
        "chat_id": message.chat_id,
        "message_id": message.message_id,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }


async def _global_btn_gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """TypeHandler at group=-1. Runs before any callback handler. If the callback
    is from a stale message (not the most recent button-bearing message for
    this user, or older than BTN_TTL_SECONDS), strip the buttons, answer with
    an explanation, and raise ApplicationHandlerStop so no other handler runs.
    """
    q = update.callback_query
    if q is None or q.message is None or q.from_user is None:
        return
    user_id = q.from_user.id
    latest = ctx.application.bot_data.get("btn_msgs", {}).get(user_id)
    if latest is None:
        return  # no record yet -- allow (gradual rollout)
    stale = False
    reason = ""
    if latest.get("message_id") != q.message.message_id:
        stale = True
        reason = "Use the most recent message — this menu is from an older one."
    else:
        from datetime import datetime, timezone
        try:
            sent = datetime.fromisoformat(latest["sent_at"])
        except (KeyError, ValueError):
            sent = None
        if sent is None or (datetime.now(timezone.utc) - sent).total_seconds() > BTN_TTL_SECONDS:
            stale = True
            reason = "This menu has expired. Run the command again."
    if stale:
        try:
            await q.answer(reason, show_alert=False)
        except Exception:
            pass
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        raise ApplicationHandlerStop


def _ticket_detail_kb(issue_id: int, is_admin: bool) -> InlineKeyboardMarkup:
    # Reply always goes straight to reply input for everyone (no submenu).
    # Only Close and Fix have submenus, since they have multiple action variants.
    row = [InlineKeyboardButton("💬 Reply", callback_data=f"tkr:{issue_id}")]
    if is_admin:
        row.append(InlineKeyboardButton("🔧 Fix", callback_data=f"tkf:{issue_id}"))
        row.append(InlineKeyboardButton("✅ Close", callback_data=f"tkc:{issue_id}"))
    return InlineKeyboardMarkup([row])


async def tk_reply_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin tapped top-level [Reply]. Opens [Reply] [Close] sub-menu."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    if update.effective_user.id != ctx.bot_data.get("admin_id"):
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 Reply", callback_data=f"tkr:{issue_id}"),
        InlineKeyboardButton("✅ Close", callback_data=f"tkcd:{issue_id}"),
    ]])
    await q.edit_message_text(f"Reply to ticket #{issue_id}?", reply_markup=kb)


async def tk_open(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Tapped [#N] from /tickets list. Sends a NEW detail message with the
    ticket's context (type, media, S/E, reporter, age) so the admin/user knows
    what they're acting on without having to scroll back to the list."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    tg_id = update.effective_user.id
    is_admin, token, decrypt_failed = await _token_for(ctx, tg_id)
    if not is_admin and decrypt_failed:
        await q.message.reply_text(
            "Your Plex link can't be decrypted (the encryption key may have rotated). "
            "Run /unlink then /link to reconnect."
        )
        return
    if not is_admin and token is None:
        await q.message.reply_text("DM me /link first so I can act on tickets as you.")
        return

    text, kb = await _build_ticket_detail(ctx, issue_id, is_admin, token)
    msg = await q.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=kb,
    )
    _record_btn(ctx.application, tg_id, msg)


async def _build_ticket_detail(
    ctx: ContextTypes.DEFAULT_TYPE,
    issue_id: int,
    is_admin: bool,
    token: Optional[str],
) -> tuple[str, InlineKeyboardMarkup]:
    """Render the ticket detail message text + keyboard. Shared between tk_open
    (sending a new message) and tk_back (editing an existing one)."""
    seerr: SeerrClient = ctx.bot_data["seerr"]
    media_label = ""
    type_emoji = "📝"
    type_name = "Issue"
    reporter = ""
    age = ""
    description = ""
    try:
        issue = await seerr.get_issue(issue_id, as_plex_token=None if is_admin else token)
        type_emoji, type_name = ISSUE_TYPES.get(issue.issue_type, ("❓", "Other"))
        reporter = issue.created_by or ""
        age = _format_age(issue.created_at) if issue.created_at else ""
        description = (issue.description or "").strip()
        if issue.media_type in ("movie", "tv") and issue.tmdb_id:
            try:
                title, year = await seerr.get_media_title(issue.media_type, issue.tmdb_id)
                m_emoji = "🎬" if issue.media_type == "movie" else "📺"
                media_label = f"{m_emoji} {title}" + (f" ({year})" if year else "")
                if issue.media_type == "tv" and issue.problem_season:
                    if issue.problem_episode:
                        media_label += f" — S{int(issue.problem_season):02d}E{int(issue.problem_episode):02d}"
                    else:
                        media_label += f" — S{int(issue.problem_season):02d}"
            except Exception:
                logger.exception("get_media_title failed for #%d", issue_id)
    except Exception:
        logger.exception("get_issue failed for #%d", issue_id)
    lines = [f"<b>Ticket #{issue_id}</b>"]
    if media_label:
        lines.append(html.escape(media_label))
    lines.append(f"<b>Issue:</b> {type_emoji} {type_name}")
    if reporter:
        lines.append(f"<b>Reported by:</b> {html.escape(reporter)}")
    if age:
        lines.append(f"<b>Age:</b> {age}")
    if description:
        lines.append("")
        lines.append("<b>Description:</b>")
        lines.append(f"<i>\"{html.escape(description)}\"</i>")
    return "\n".join(lines), _ticket_detail_kb(issue_id, is_admin)


async def tk_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel from a sub-menu -- edit the message back to the ticket detail view."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    tg_id = update.effective_user.id
    is_admin, token, _decrypt_failed = await _token_for(ctx, tg_id)
    text, kb = await _build_ticket_detail(ctx, issue_id, is_admin, token)
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        logger.exception("tk_back edit failed for #%d", issue_id)
        return
    # The same message_id is the active one; refresh sent_at so the 6h timer resets.
    _record_btn(ctx.application, tg_id, q.message)


async def tk_close_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin tapped [Close] -- show the with/without comment options."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    if update.effective_user.id != ctx.bot_data.get("admin_id"):
        return
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💬 Comment", callback_data=f"tkcc:{issue_id}"),
            InlineKeyboardButton("✓ No comment", callback_data=f"tkcd:{issue_id}"),
        ],
        [InlineKeyboardButton("⬅️ Cancel", callback_data=f"tkback:{issue_id}")],
    ])
    await q.edit_message_text(f"Close ticket #{issue_id}?", reply_markup=kb)
    _record_btn(ctx.application, update.effective_user.id, q.message)


async def tk_fix(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin tapped [Fix]. Opens the [Redownload] [Mark Failed] [Close] submenu."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    if update.effective_user.id != ctx.bot_data.get("admin_id"):
        return
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Redownload", callback_data=f"tkfd:{issue_id}"),
            InlineKeyboardButton("🚫 Mark Failed", callback_data=f"tkfm:{issue_id}"),
        ],
        [InlineKeyboardButton("⬅️ Cancel", callback_data=f"tkback:{issue_id}")],
    ])
    await q.edit_message_text(f"🔧 Fix #{issue_id} — how?", reply_markup=kb)
    _record_btn(ctx.application, update.effective_user.id, q.message)


async def tk_fix_redownload(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete current file + trigger search."""
    await _apply_fix(update, ctx, strategy="redownload")


async def tk_fix_mark_failed(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark the most recent grab as failed → Radarr/Sonarr blocklists + re-searches."""
    await _apply_fix(update, ctx, strategy="mark_failed")


async def _apply_fix(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, strategy: str) -> None:
    """Shared admin-fix path. strategy is 'redownload' or 'mark_failed'."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    if update.effective_user.id != ctx.bot_data.get("admin_id"):
        return
    seerr: SeerrClient = ctx.bot_data["seerr"]
    radarr: Optional[RadarrClient] = ctx.bot_data.get("radarr")
    sonarr: Optional[SonarrClient] = ctx.bot_data.get("sonarr")
    store: UserStore = ctx.bot_data["store"]

    try:
        issue = await seerr.get_issue(issue_id)
    except Exception as exc:
        logger.exception("get_issue failed for #%d", issue_id)
        await _edit_or_send(q, f"Couldn't fetch ticket #{issue_id}. {user_friendly_message(exc)}")
        return

    media_type = issue.media_type
    tmdb_id = issue.tmdb_id
    season = issue.problem_season
    episode = issue.problem_episode
    action_name = "Redownload" if strategy == "redownload" else "Mark Failed"

    if media_type == "tv" and not episode:
        await _edit_or_send(q,
            f"{action_name} only works on individual episodes or movies — not whole "
            f"seasons or shows. For #{issue_id}, fix it in Sonarr directly."
        )
        return

    media: dict = {"type": media_type, "tmdb_id": tmdb_id}
    label_title = ""
    label_year = ""
    try:
        label_title, label_year = await seerr.get_media_title(media_type, tmdb_id)
    except Exception:
        logger.exception("get_media_title failed for #%d", issue_id)
    if media_type == "tv":
        try:
            _seasons, tvdb_id = await seerr.get_tv_seasons(tmdb_id)
            media["tvdb_id"] = tvdb_id
        except Exception:
            logger.exception("get_tv_seasons failed for #%d", issue_id)

    if strategy == "redownload":
        result = await _run_autofix(media, season, episode, radarr, sonarr)
    else:
        result = await _run_mark_failed(media, season, episode, radarr, sonarr)

    label = label_title + (f" ({label_year})" if label_year else "")
    if media_type == "tv" and season:
        label += (
            f" — S{int(season):02d}E{int(episode):02d}"
            if episode else f" — S{int(season):02d}"
        )

    if result.status == "failed":
        await _edit_or_send(q, f"⚠️ {action_name} for #{issue_id} didn't run: {result.message}")
        return

    # ok or partial. If a search was triggered, enqueue the completion poller.
    if result.should_poll:
        try:
            kwargs: dict = {
                "chat_id": q.message.chat_id,
                "user_id": update.effective_user.id,
                "media_type": media_type,
                "label": label or f"#{issue_id}",
                "issue_id": issue_id,
                "issue_url": "",
            }
            poll_info = result.poll_info or {}
            if media_type == "movie":
                kwargs["radarr_movie_id"] = poll_info.get("movie_id")
            else:
                kwargs["sonarr_series_id"] = poll_info.get("series_id")
                kwargs["sonarr_episode_id"] = poll_info.get("episode_id")
                kwargs["sonarr_season"] = poll_info.get("season")
                kwargs["expected_episode_ids"] = poll_info.get("expected_episode_ids") or []
            await store.add_pending_autofix(**kwargs)
            await store.log_autofix(update.effective_user.id, media_type, tmdb_id,
                                    season=season, episode=episode)
        except Exception:
            logger.exception("failed to enqueue pending autofix for #%d", issue_id)
            prefix = "🔧" if result.ok else "⚠️"
            await _edit_or_send(q,
                f"{prefix} {action_name} for #{issue_id} ({result.message}), "
                "but couldn't enqueue completion notification."
            )
            return

    prefix = "🔧" if result.ok else "⚠️"
    tail = "\n\n🔔 I'll DM when the new file finishes downloading." if result.should_poll else ""
    await _edit_or_send(q,
        f"{prefix} {action_name} for #{issue_id}.\n{label}\n\n{result.message}{tail}"
    )
    return


async def tk_close_direct(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin tapped [Without comment]. Resolve immediately."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    if update.effective_user.id != ctx.bot_data.get("admin_id"):
        return
    seerr: SeerrClient = ctx.bot_data["seerr"]
    try:
        await seerr.resolve_issue(issue_id, as_plex_token=None)
    except Exception as exc:
        logger.exception("resolve_issue failed for #%d", issue_id)
        await _edit_or_send(q, f"Couldn't close #{issue_id}. {user_friendly_message(exc)}")
        return
    await _edit_or_send(q, f"✅ Closed ticket #{issue_id}.")


# --- Ticket reply conversation ----------------------------------------------

async def tk_reply_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for `tkr:<id>` -- reply only (no close)."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return ConversationHandler.END
    is_admin, token, decrypt_failed = await _token_for(ctx, update.effective_user.id)
    if not is_admin and decrypt_failed:
        await q.message.reply_text(
            "Your Plex link can't be decrypted (the encryption key may have rotated). "
            "Run /unlink then /link to reconnect."
        )
        return ConversationHandler.END
    if not is_admin and token is None:
        await q.message.reply_text("DM me /link first so I can post comments as you.")
        return ConversationHandler.END
    ctx.user_data["tk_reply_id"] = issue_id
    ctx.user_data["tk_close_after"] = False
    await q.edit_message_text(
        f"Send the reply text for ticket #{issue_id} (or /cancel)."
    )
    return AWAIT_TICKET_REPLY


async def tk_close_with_comment_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for `tkcc:<id>` -- post comment then close (admin only)."""
    q = update.callback_query
    await q.answer()
    try:
        issue_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return ConversationHandler.END
    if update.effective_user.id != ctx.bot_data.get("admin_id"):
        return ConversationHandler.END
    ctx.user_data["tk_reply_id"] = issue_id
    ctx.user_data["tk_close_after"] = True
    await q.edit_message_text(
        f"Send the closing comment for ticket #{issue_id} (or /cancel)."
    )
    return AWAIT_TICKET_REPLY


async def tk_reply_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the reply text, post it, optionally close."""
    issue_id = ctx.user_data.get("tk_reply_id")
    close_after = bool(ctx.user_data.get("tk_close_after"))
    if not issue_id:
        return ConversationHandler.END
    text = (update.effective_message.text or "").strip()
    if not text:
        await update.effective_message.reply_text("Empty message. Send a few words or /cancel.")
        return AWAIT_TICKET_REPLY
    is_admin, token, decrypt_failed = await _token_for(ctx, update.effective_user.id)
    if not is_admin and decrypt_failed:
        await update.effective_message.reply_text(
            "Your Plex link can't be decrypted (the encryption key may have rotated). "
            "Run /unlink then /link to reconnect."
        )
        return ConversationHandler.END
    if not is_admin and token is None:
        await update.effective_message.reply_text("Your /link is gone or incomplete. /link to re-link.")
        ctx.user_data.pop("tk_reply_id", None)
        ctx.user_data.pop("tk_close_after", None)
        return ConversationHandler.END
    seerr: SeerrClient = ctx.bot_data["seerr"]
    try:
        await seerr.add_issue_comment(issue_id, text, as_plex_token=token)
    except Exception as exc:
        logger.exception("add_issue_comment failed for #%d", issue_id)
        await update.effective_message.reply_text(f"Couldn't post comment on #{issue_id}. {user_friendly_message(exc)}")
        ctx.user_data.pop("tk_reply_id", None)
        ctx.user_data.pop("tk_close_after", None)
        return ConversationHandler.END
    if close_after:
        try:
            await seerr.resolve_issue(issue_id, as_plex_token=None)
        except Exception as exc:
            logger.exception("resolve_issue failed for #%d", issue_id)
            await update.effective_message.reply_text(
                f"💬 Comment posted on #{issue_id}, but couldn't close. {user_friendly_message(exc)}"
            )
            ctx.user_data.pop("tk_reply_id", None)
            ctx.user_data.pop("tk_close_after", None)
            return ConversationHandler.END
        await update.effective_message.reply_text(f"💬 Replied and ✅ closed ticket #{issue_id}.")
    else:
        await update.effective_message.reply_text(f"💬 Replied to ticket #{issue_id}.")
    ctx.user_data.pop("tk_reply_id", None)
    ctx.user_data.pop("tk_close_after", None)
    return ConversationHandler.END


async def tk_reply_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.pop("tk_reply_id", None)
    ctx.user_data.pop("tk_close_after", None)
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


def _ticket_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(tk_reply_start, pattern=r"^tkr:\d+$"),
            CallbackQueryHandler(tk_close_with_comment_start, pattern=r"^tkcc:\d+$"),
        ],
        states={
            AWAIT_TICKET_REPLY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, tk_reply_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", tk_reply_cancel)],
        name="ticket_reply",
        persistent=False,
        allow_reentry=True,
    )


def _link_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("link", cmd_link)],
        states={
            AWAIT_LINK_CONSENT: [CallbackQueryHandler(cmd_link_consent, pattern=r"^link_consent:")],
            AWAIT_PLATFORM_CHOICE: [CallbackQueryHandler(cmd_link_platform, pattern=r"^tklplat:")],
        },
        fallbacks=[CommandHandler("cancel", link_cancel)],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
        name="link",
        persistent=False,
    )


async def cmd_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    if msg.chat.type != ChatType.PRIVATE:
        await msg.reply_text("Please DM me to link your account.")
        return ConversationHandler.END
    if await _require_seerr(update, ctx) is None:
        return ConversationHandler.END
    rows = [[
        InlineKeyboardButton("✅ Yes, continue", callback_data="link_consent:yes"),
        InlineKeyboardButton("🛑 Cancel", callback_data="link_consent:no"),
    ]]
    await msg.reply_text(
        "Sign in with Plex so issues you submit are tagged as you.\n\n"
        "You can use /unlink anytime to remove access.\n\n"
        "Continue?",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return AWAIT_LINK_CONSENT


async def cmd_link_consent(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    choice = q.data.split(":")[1]
    if choice == "no":
        await q.edit_message_text("Cancelled. /link to try again later.")
        return ConversationHandler.END
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💻 Desktop", callback_data="tklplat:desktop"),
        InlineKeyboardButton("📱 iOS / Android", callback_data="tklplat:mobile"),
    ]])
    await q.edit_message_text(
        "Where are you using Telegram?",
        reply_markup=kb,
    )
    return AWAIT_PLATFORM_CHOICE


async def _poll_with_cancel(plex: PlexClient, pin_id: int, max_iters: int,
                            ctx: ContextTypes.DEFAULT_TYPE, loop_id: str,
                            chat_id: Optional[int] = None) -> Optional[str]:
    """Poll Plex until a token is returned, this loop is superseded by a newer
    one (link_active_loop changes), or max_iters is exhausted. The loop_id
    pattern is race-free: a new loop overwrites link_active_loop, and any
    older loops bail on their next check. No reset is needed.

    On consecutive failures (Plex API down), backs off from 3s -> 6s -> 12s
    and DMs the user once after 5 in a row so they don't think the bot is
    silently broken.
    """
    consecutive_failures = 0
    warned_user = False
    for _ in range(max_iters):
        if ctx.user_data.get("link_active_loop") != loop_id:
            return None
        sleep_s = min(12.0, 3.0 * (2 ** min(consecutive_failures, 2)))
        await asyncio.sleep(sleep_s)
        try:
            token = await plex.poll_pin(pin_id)
            consecutive_failures = 0
            if token:
                if ctx.user_data.get("link_active_loop") != loop_id:
                    return None
                return token
        except Exception:
            consecutive_failures += 1
            logger.exception("poll_pin failed (will retry; %d in a row)",
                             consecutive_failures)
            if consecutive_failures == 5 and not warned_user and chat_id is not None:
                warned_user = True
                try:
                    await ctx.bot.send_message(
                        chat_id=chat_id,
                        text=("⚠️ Plex's API isn't responding right now. Still trying — "
                              "I'll let you know if a token comes through. If you've "
                              "already approved in Plex, sit tight."),
                    )
                except Exception:
                    logger.exception("couldn't send Plex-down warning to %s", chat_id)
    return None


async def _finalize_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                         auth_token: str) -> None:
    """Shared post-auth path: Seerr login + persist mapping + success/failure reply."""
    plex: PlexClient = ctx.bot_data["plex"]
    seerr: SeerrClient = ctx.bot_data["seerr"]
    store: UserStore = ctx.bot_data["store"]
    chat_id = update.effective_chat.id
    tg_id = update.effective_user.id

    try:
        seerr_id, display, _ = await seerr.login_with_plex(auth_token)
    except Exception as exc:
        logger.exception("seerr login_with_plex failed")
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=(
                "✓ Plex authorized you, but Seerr rejected the sign-in.\n"
                f"{user_friendly_message(exc)}\n\n"
                "Your Plex account probably isn't shared in Seerr yet. "
                "Ask the admin to invite you."
            ),
        )
        return

    try:
        plex_user = await plex.get_user(auth_token)
    except Exception:
        logger.exception("plex get_user failed")
        plex_user = None

    await store.link_with_plex(
        telegram_id=tg_id,
        seerr_id=seerr_id,
        seerr_display=display,
        plex_token=auth_token,
        plex_uuid=plex_user.uuid if plex_user else "",
        plex_username=plex_user.username if plex_user else display,
    )

    await ctx.bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ Linked as *{display}*.\n\n"
            "You can now /issue and /tickets."
        ),
        parse_mode="Markdown",
    )


async def cmd_link_platform(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the platform-choice tap: issues a strong PIN and starts polling."""
    q = update.callback_query
    await q.answer()
    platform = q.data.split(":")[1]  # "desktop" or "mobile"
    plex: PlexClient = ctx.bot_data["plex"]
    try:
        pin = await plex.request_pin(strong=True)
    except Exception as exc:
        logger.exception("plex request_pin failed")
        await q.edit_message_text(f"Couldn't start Plex auth. {user_friendly_message(exc)}")
        return ConversationHandler.END

    loop_id = secrets.token_hex(8)
    ctx.user_data["link_active_loop"] = loop_id

    if platform == "desktop":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Open Plex authorization", url=pin.auth_url)],
            [InlineKeyboardButton("❌ Having trouble?", callback_data="tklhelp")],
        ])
        text = "Authorize Hermes in Plex:\n\nSign in and tap Allow."
    else:  # mobile
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Copy auth link",
                                  copy_text=CopyTextButton(text=pin.auth_url))],
            [InlineKeyboardButton("❌ Didn't work?", callback_data="tklhelp")],
        ])
        text = "Tap to copy the auth link, then paste it into a browser."

    await q.edit_message_text(text, reply_markup=kb)

    # Strong PIN window: ~28 min (560 × 3s, under the 30-min lifetime).
    auth_token = await _poll_with_cancel(plex, pin.id, max_iters=560, ctx=ctx,
                                         loop_id=loop_id,
                                         chat_id=update.effective_chat.id)
    if auth_token is None:
        if ctx.user_data.get("link_active_loop") != loop_id:
            # A newer loop (didnt_work fallback) has taken ownership; exit silently
            return ConversationHandler.END
        await q.edit_message_text("⏱️ Plex auth timed out. /link to try again.")
        return ConversationHandler.END

    await _finalize_link(update, ctx, auth_token)
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    return ConversationHandler.END


async def cmd_link_didnt_work(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles 'Having trouble?' / 'Didn't work?' button. Fires OUTSIDE the
    conversation so it can interrupt an in-progress poll. Issues a weak PIN
    and starts a fresh poll loop."""
    q = update.callback_query
    await q.answer()

    plex: PlexClient = ctx.bot_data["plex"]
    try:
        pin = await plex.request_pin(strong=False)
    except Exception as exc:
        logger.exception("plex request_pin failed (fallback)")
        await q.edit_message_text(f"Couldn't get a fresh code. {user_friendly_message(exc)}")
        return

    # Claim a new loop ID -- any prior poll will see this and exit on its
    # next check. No race because there's no reset-after-set sequence.
    loop_id = secrets.token_hex(8)
    ctx.user_data["link_active_loop"] = loop_id

    code = pin.code.upper()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Copy plex.tv/link",
                             copy_text=CopyTextButton(text="https://plex.tv/link")),
    ]])
    # Tell the user to tap the copy button. Don't mention plex.tv/link in the
    # body text -- Telegram would auto-link it AND show a preview card. The
    # trailing line gives breathing room above the inline button.
    await q.edit_message_text(
        f"Enter this code in the page from the button below:\n\n"
        f"*{code}*\n\n"
        f"Code expires in 15 minutes.",
        reply_markup=kb,
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )

    # Weak PIN window: ~14 min (280 × 3s, under the 15-min lifetime).
    auth_token = await _poll_with_cancel(plex, pin.id, max_iters=280, ctx=ctx,
                                         loop_id=loop_id,
                                         chat_id=update.effective_chat.id)
    if auth_token is None:
        if ctx.user_data.get("link_active_loop") != loop_id:
            return  # superseded by another loop
        await q.edit_message_text("⏱️ Plex auth timed out. /link to try again.")
        return

    await _finalize_link(update, ctx, auth_token)
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


async def link_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Cancelled. /link to try again later.")
    return ConversationHandler.END


async def cmd_unlink(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    store: UserStore = ctx.bot_data["store"]
    removed = await store.unlink(update.effective_user.id)
    if removed:
        await update.effective_message.reply_text(
            "🔓 Unlinked. I've removed your Plex token from my storage.\n\n"
            "For extra safety, you can also remove 'Hermes' from your Plex "
            "authorized devices at app.plex.tv → Settings → Authorized Devices."
        )
    else:
        await update.effective_message.reply_text("You weren't linked.")


# --- Issue conversation ------------------------------------------------------

def _issue_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("issue", issue_start)],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, issue_title)],
            PICK_MEDIA: [
                CallbackQueryHandler(issue_pick_media, pattern=r"^media:"),
                CallbackQueryHandler(issue_research_parent, pattern=r"^research_parent$"),
            ],
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
        results = await seerr.search(query, limit=5)
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
            keycap, callback_data=f"media:{r.media_type}:{r.tmdb_id}",
        ))
        if len(btn_row) == 3:
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

    cancel_btn = InlineKeyboardButton("Cancel", callback_data="cancel")
    if parent:
        # Flush the last partial row, then parent on its own row, then Cancel.
        if last_partial_row:
            rows.append(last_partial_row)
        rows.append([InlineKeyboardButton(
            f'🔍 Search "{parent}" instead',
            callback_data="research_parent",
        )])
        rows.append([cancel_btn])
    else:
        # No parent button -- append Cancel to the last partial row if there's
        # room, else give it its own row.
        if last_partial_row and len(last_partial_row) < 3:
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
                callback_data="research_parent",
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
    media = ctx.user_data.get("media", {})
    episode = ctx.user_data.get("episode")
    # Whole-season / whole-show TV picks are not auto-fixable; only individual
    # episodes or movies are.
    is_whole_season = media.get("type") == "tv" and not episode
    eligible = (
        issue_type in AUTOFIX_ELIGIBLE_TYPES
        and tg_id in allowlist
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
        InlineKeyboardButton("✅ Try auto-fix", callback_data="autofix:yes"),
        InlineKeyboardButton("📨 Just report", callback_data="autofix:no"),
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
    label = media["title"] + (f" ({media['year']})" if media.get("year") else "")
    if media["type"] == "tv":
        label += (
            f" — S{int(season):02d}E{int(episode):02d}"
            if episode else f" — S{int(season):02d} (whole season)"
        )

    lines = [
        f"✅ Reported as issue #{created.id}",
        f"  {emoji} {name} — {label}",
    ]

    # 2. If auto-fix requested, run it
    if autofix:
        ok, detail, poll_info = await _run_autofix(media, season, episode, radarr, sonarr)
        if ok:
            await store.log_autofix(
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
                await store.add_pending_autofix(**kwargs)
                lines.append(f"🔧 Auto-fix: {detail}")
                lines.append("🔔 I'll DM you when the new file finishes downloading (or after 6h timeout).")
            except Exception:
                logger.exception("failed to enqueue pending autofix")
                lines.append(f"🔧 Auto-fix: {detail}")
                lines.append("(Couldn't enqueue completion notification.)")
        else:
            lines.append(f"⚠️ Auto-fix didn't run: {detail}")

    lines.append("\nUse /tickets to manage it.")
    await update.effective_message.reply_text("\n".join(lines))
    ctx.user_data.clear()
    return ConversationHandler.END


async def _run_autofix(
    media: dict,
    season: Optional[int],
    episode: Optional[int],
    radarr: Optional[RadarrClient],
    sonarr: Optional[SonarrClient],
) -> FixResult:
    """Run delete+search via Radarr/Sonarr. Returns FixResult; the caller
    inspects status (ok/partial/failed) and should_poll to decide whether
    to enqueue the autofix completion poller."""
    try:
        if media["type"] == "movie":
            if not radarr:
                return FixResult.failed("Radarr not configured.")
            return await radarr.auto_fix(media["tmdb_id"])
        if media["type"] == "tv":
            if not sonarr:
                return FixResult.failed("Sonarr not configured.")
            if not episode:
                # Whole-season / whole-show auto-fix is not supported -- too
                # destructive. Episode-only.
                return FixResult.failed(
                    "Auto-fix only works on individual episodes, not whole seasons."
                )
            tvdb_id = media.get("tvdb_id")
            if not tvdb_id:
                return FixResult.failed("Couldn't find TVDb ID for this show.")
            return await sonarr.auto_fix_episode(tvdb_id, season, episode)
    except Exception as exc:
        logger.exception("auto_fix failed")
        return FixResult.failed(user_friendly_message(exc))
    return FixResult.failed("Unknown media type.")


async def _run_mark_failed(
    media: dict,
    season: Optional[int],
    episode: Optional[int],
    radarr: Optional[RadarrClient],
    sonarr: Optional[SonarrClient],
) -> FixResult:
    """Same shape as _run_autofix. Blocklists the most recent grab in
    addition to delete+search."""
    try:
        if media["type"] == "movie":
            if not radarr:
                return FixResult.failed("Radarr not configured.")
            return await radarr.mark_failed(media["tmdb_id"])
        if media["type"] == "tv":
            if not sonarr:
                return FixResult.failed("Sonarr not configured.")
            if not episode:
                return FixResult.failed("Mark Failed only works on individual episodes.")
            tvdb_id = media.get("tvdb_id")
            if not tvdb_id:
                return FixResult.failed("Couldn't find TVDb ID for this show.")
            return await sonarr.mark_failed_episode(tvdb_id, season, episode)
    except Exception as exc:
        logger.exception("mark_failed failed")
        return FixResult.failed(user_friendly_message(exc))
    return FixResult.failed("Unknown media type.")


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
    pending = await store.list_pending_autofixes()
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
                await store.mark_autofix_status(fix.id, "timeout")
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
                await store.mark_autofix_status(fix.id, "complete")
        except NotFoundAPIError:
            # Media was deleted from Sonarr/Radarr between enqueue and poll.
            # Mark failed and DM the user instead of polling forever.
            logger.info("poll: media removed for fix %d; marking failed", fix.id)
            await _notify_media_gone(ctx, fix)
            await store.mark_autofix_status(fix.id, "failed")
        except TransientAPIError:
            # Service hiccup — keep polling next tick.
            logger.debug("poll: transient error for fix %d; will retry next tick", fix.id)
        except Exception:
            logger.exception("poll failed for fix %d", fix.id)


async def _notify_media_gone(ctx: ContextTypes.DEFAULT_TYPE, fix) -> None:
    text = (
        f"⚠️ Auto-fix abandoned for *{fix.label}*.\n"
        "The media was removed from Sonarr/Radarr before the new file landed. "
        f"Original issue: {fix.issue_url}"
    )
    try:
        await ctx.bot.send_message(chat_id=fix.chat_id, text=text, parse_mode="Markdown")
    except Exception:
        logger.exception("notify_media_gone send_message failed for fix %d", fix.id)


async def _notify_complete(ctx: ContextTypes.DEFAULT_TYPE, fix, extra: str = "") -> None:
    text = (
        f"🎉 Auto-fix complete: *{fix.label}* downloaded{extra}.\n"
        f"Original issue: {fix.issue_url}\n\n"
        "Did this resolve the problem?"
    )
    keyboard = [[
        InlineKeyboardButton("✅ Yes, close it", callback_data=f"resolve:{fix.issue_id}:yes"),
        InlineKeyboardButton("💬 No, add a comment", callback_data=f"resolve:{fix.issue_id}:no"),
    ]]
    try:
        await ctx.bot.send_message(
            chat_id=fix.chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception:
        logger.exception("notify_complete send_message failed for fix %d", fix.id)


async def _notify_timeout(ctx: ContextTypes.DEFAULT_TYPE, fix) -> None:
    text = (
        f"⏱️ Auto-fix timed out (6h) for *{fix.label}*.\n"
        f"No new file was imported. Check Sonarr/Radarr to see if a release was grabbed.\n"
        f"Original issue: {fix.issue_url}\n\n"
        "Want to add a comment for the admin to follow up?"
    )
    keyboard = [[
        InlineKeyboardButton("💬 Add a comment", callback_data=f"resolve:{fix.issue_id}:no"),
        InlineKeyboardButton("🙅 No, leave it", callback_data=f"resolve:{fix.issue_id}:skip"),
    ]]
    try:
        await ctx.bot.send_message(
            chat_id=fix.chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception:
        logger.exception("notify_timeout send_message failed for fix %d", fix.id)


# --- Resolve follow-up conversation -----------------------------------------

def _resolve_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(resolve_start, pattern=r"^resolve:")],
        states={
            AWAIT_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, resolve_comment)],
        },
        fallbacks=[CommandHandler("cancel", resolve_cancel)],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
        name="resolve",
        persistent=False,
    )


async def resolve_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    try:
        _, issue_id_s, choice = q.data.split(":")
        issue_id = int(issue_id_s)
    except (ValueError, AttributeError):
        await q.edit_message_text("Couldn't parse selection.")
        return ConversationHandler.END
    if choice == "yes":
        seerr: SeerrClient = ctx.bot_data["seerr"]
        store: UserStore = ctx.bot_data["store"]
        mapping = await store.get(update.effective_user.id)
        token = mapping.plex_token if (mapping and mapping.plex_token) else None
        try:
            await seerr.resolve_issue(issue_id, as_plex_token=token)
        except Exception as exc:
            logger.exception("resolve_issue failed")
            await q.edit_message_text(f"Couldn't close issue #{issue_id}. {user_friendly_message(exc)}")
            return ConversationHandler.END
        await q.edit_message_text(f"✅ Issue #{issue_id} closed. Thanks!")
        return ConversationHandler.END
    if choice == "skip":
        await q.edit_message_text("OK, leaving the issue open.")
        return ConversationHandler.END
    # "no" -> ask for comment
    ctx.user_data["awaiting_comment_for"] = issue_id
    await q.edit_message_text(
        "Sorry it's still broken. What's still wrong? (Send a brief message; admin will see it on the issue.)"
    )
    return AWAIT_COMMENT


async def resolve_comment(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    issue_id = ctx.user_data.get("awaiting_comment_for")
    if not issue_id:
        return ConversationHandler.END
    comment = update.effective_message.text.strip()
    if not comment:
        await update.effective_message.reply_text("Empty message. Send a few words or /cancel.")
        return AWAIT_COMMENT
    store: UserStore = ctx.bot_data["store"]
    seerr: SeerrClient = ctx.bot_data["seerr"]
    mapping = await store.get(update.effective_user.id)
    token = mapping.plex_token if (mapping and mapping.plex_token) else None
    try:
        await seerr.add_issue_comment(issue_id, comment, as_plex_token=token)
    except Exception as exc:
        logger.exception("add_issue_comment failed")
        await update.effective_message.reply_text(f"Couldn't add comment. {user_friendly_message(exc)}")
        ctx.user_data.pop("awaiting_comment_for", None)
        return ConversationHandler.END
    await update.effective_message.reply_text(
        f"💬 Added your comment to issue #{issue_id}. Admin will follow up."
    )
    ctx.user_data.pop("awaiting_comment_for", None)
    return ConversationHandler.END


async def resolve_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.pop("awaiting_comment_for", None)
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


# --- Error handler -----------------------------------------------------------

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: %s", ctx.error)


# --- Main --------------------------------------------------------------------

def _migrate_legacy_env_into_settings(settings_store: SettingsStore) -> bool:
    """One-time migration: if bot token / admin id aren't in settings yet but ARE
    in the environment, copy them in. Returns True if anything was written.
    """
    s = settings_store.settings
    changed = False
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not s.telegram_bot_token and env_token:
        s.telegram_bot_token = env_token
        changed = True
    if not s.admin_telegram_id:
        try:
            env_admin = int(os.environ.get("ADMIN_TELEGRAM_ID", "0") or "0")
        except ValueError:
            env_admin = 0
        if env_admin:
            s.admin_telegram_id = env_admin
            changed = True
    if changed:
        settings_store.save()
        logger.info("Migrated TELEGRAM_BOT_TOKEN/ADMIN_TELEGRAM_ID from env into settings.json")
    return changed


async def _run_setup_only(settings_store: SettingsStore, session_secret: bytes,
                          data_dir: Path, settings_path: Path, db_path: str,
                          http_port: int, http_bind: str) -> None:
    """Run a webui-only server until the user completes setup (then exit so the
    container restarts and main() picks up the configured-mode path).
    """
    logger.warning(
        "Bot is NOT configured (telegram_bot_token / admin_telegram_id missing). "
        "Running in SETUP-ONLY mode. Open http://<host>:%d/admin to finish setup.",
        http_port,
    )
    web_app = web.Application(client_max_size=32 * 1024 * 1024)

    async def _on_settings_changed() -> None:
        # If setup just completed (bot is now configured), exit so the container
        # restarts into configured mode.
        if settings_store.settings.is_bot_configured():
            logger.info("Setup complete; exiting in 2s so container restarts into full mode")
            _schedule_clean_exit(2.0)

    attach_webui(
        web_app,
        settings_store=settings_store,
        session_secret=session_secret,
        data_dir=data_dir,
        settings_path=settings_path,
        db_path=Path(db_path),
        on_settings_changed=_on_settings_changed,
    )
    runner = await start_http_server(web_app, host=http_bind, port=http_port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def main() -> None:
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    db_path = os.environ.get("STORE_PATH", str(data_dir / "mappings.sqlite"))
    settings_path = data_dir / "settings.json"
    http_port = int(os.environ.get("WEBHOOK_PORT", "8765"))
    http_bind = os.environ.get("WEBHOOK_BIND", "0.0.0.0").strip() or "0.0.0.0"

    settings_store = SettingsStore(settings_path)
    _migrate_legacy_env_into_settings(settings_store)
    session_secret = load_or_create_session_secret(data_dir / "session_secret")
    crypto = TokenCrypto(key_path=os.environ.get("ENCRYPTION_KEY_PATH", str(data_dir / "encryption.key")))
    user_store = UserStore(db_path, crypto=crypto)
    plex = PlexClient(
        client_id_path=os.environ.get("PLEX_CLIENT_ID_PATH", str(data_dir / "client_id")),
    )

    if not settings_store.settings.is_bot_configured():
        asyncio.run(_run_setup_only(
            settings_store, session_secret,
            data_dir, settings_path, db_path,
            http_port, http_bind,
        ))
        return

    app = _build_app(
        settings_store=settings_store,
        session_secret=session_secret,
        user_store=user_store,
        plex=plex,
        data_dir=data_dir,
        settings_path=settings_path,
        db_path=db_path,
        http_port=http_port,
        http_bind=http_bind,
    )
    logger.info("Starting bot (polling)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
