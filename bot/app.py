"""Application wiring: build the PTB Application, register handlers, start
the HTTP server, run the polling loop."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from aiohttp import web
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from http_util import user_friendly_message
from plex import PlexClient
from radarr import RadarrClient
from seerr import SeerrClient
from settings import SettingsStore, load_or_create_session_secret
from sonarr import SonarrClient
from store import TokenCrypto, UserStore
from webhook import attach_webhook, start_http_server
from webui import attach_webui
from _version import __version__ as HERMES_VERSION

from bot.autofix_poll import poll_pending_autofixes
from bot.issue_flow import _issue_conversation
from bot.link_flow import _link_conversation, cmd_link_didnt_work, cmd_unlink
from bot.resolve_flow import _resolve_conversation
from bot.shared import (
    _format_status,
    _global_btn_gate,
    _schedule_clean_exit,
)
from bot.tickets import (
    _ticket_conversation,
    cmd_tickets,
    tk_back,
    tk_close_direct,
    tk_close_menu,
    tk_fix,
    tk_fix_mark_failed,
    tk_fix_redownload,
    tk_open,
    tk_reply_menu,
)
from bot.webhook_handlers import (
    handle_seerr_comment,
    handle_seerr_reported,
    handle_seerr_resolved,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("hermes")


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

    if old_clients:
        async def _close_old() -> None:
            for key, client in old_clients:
                try:
                    await client.close()  # type: ignore[attr-defined]
                except Exception:
                    logger.exception("Error closing prior %s client", key)
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(_close_old())
            # Keep a strong reference so the task isn't garbage-collected
            # mid-aclose() (CPython 3.12+ logs "Task was destroyed while
            # it is pending!"). _post_shutdown awaits any still-pending
            # entries before closing current clients.
            pending_closes = app.bot_data.setdefault("_pending_closes", [])
            pending_closes.append(task)
            # Prune finished tasks opportunistically so the list doesn't
            # grow unbounded across many reloads.
            app.bot_data["_pending_closes"] = [t for t in pending_closes if not t.done()]
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


async def _check_connections(app: Application) -> dict[str, str]:
    """Probe configured services via each client's `ping()`. No private-attr
    access. Returns dict of service -> status string."""
    out: dict[str, str] = {"Hermes": f"✅ {HERMES_VERSION}"}
    for key, label in (("seerr", "Seerr"), ("radarr", "Radarr"), ("sonarr", "Sonarr")):
        client = app.bot_data.get(key)
        if client is None:
            out[label] = "— not configured"
            continue
        try:
            version = await client.ping()
            out[label] = f"✅ {version}"
        except Exception as exc:
            logger.exception("%s ping failed", label)
            out[label] = f"❌ {user_friendly_message(exc)}"
    return out


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
    app.add_handler(CallbackQueryHandler(cmd_link_didnt_work, pattern=r"^tklhelp$"))
    app.add_error_handler(on_error)

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

    web_app = web.Application(client_max_size=32 * 1024 * 1024)

    async def _on_comment(payload: dict) -> None:
        await handle_seerr_comment(app, payload)

    async def _on_resolved(payload: dict) -> None:
        await handle_seerr_resolved(app, payload)

    async def _on_reported(payload: dict) -> None:
        await handle_seerr_reported(app, payload)

    def _secret_provider() -> str:
        settings_store: SettingsStore = app.bot_data["settings_store"]
        return settings_store.settings.webhook_secret or ""

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
        settings_store=settings_store,
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
    base = (settings_store.settings.hermes_public_url or "").strip().rstrip("/")
    if base:
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
    # Drain any prior settings-reload close tasks that haven't finished.
    pending_closes = app.bot_data.get("_pending_closes") or []
    if pending_closes:
        try:
            await asyncio.gather(*pending_closes, return_exceptions=True)
        except Exception:
            logger.exception("draining pending close tasks failed")

    # Close current API clients explicitly so httpx connection pools don't
    # leak. PlexClient is built once at startup and stashed under "plex";
    # the arr clients are managed by _build_clients_from_settings.
    for key in ("seerr", "radarr", "sonarr", "plex"):
        client = app.bot_data.get(key)
        if client is not None and hasattr(client, "close"):
            try:
                await client.close()
            except Exception:
                logger.exception("Error closing %s on shutdown", key)

    runner = app.bot_data.get("http_runner")
    if runner is not None:
        try:
            await runner.cleanup()
            logger.info("HTTP server stopped")
        except Exception:
            logger.exception("HTTP server cleanup failed")


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: %s", ctx.error)


def _migrate_legacy_env_into_settings(settings_store: SettingsStore) -> bool:
    """One-time migration: copy bot token + admin id from env into settings.json
    if missing. Returns True if anything was written."""
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
    container restarts and main() picks up the configured-mode path)."""
    logger.warning(
        "Bot is NOT configured (telegram_bot_token / admin_telegram_id missing). "
        "Running in SETUP-ONLY mode. Open http://<host>:%d/admin to finish setup.",
        http_port,
    )
    web_app = web.Application(client_max_size=32 * 1024 * 1024)

    async def _on_settings_changed() -> None:
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
