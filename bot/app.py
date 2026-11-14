"""Application wiring: build the PTB Application, register handlers, start
the HTTP server, run the polling loop."""
from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
import signal
import sys
from pathlib import Path
from aiohttp import web
import telegram
from telegram import InlineKeyboardMarkup, Update
from telegram.error import InvalidToken
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from auth_util import parse_trusted_proxies
from http_util import user_friendly_message
from plex import PlexClient
from radarr import RadarrClient
from seerr import SeerrClient, REQUIRED_WEBHOOK_TYPES
from settings import SettingsStore, load_or_create_session_secret, public_base_url
from sabnzbd import SabnzbdClient
from tautulli import TautulliClient
from sonarr import SonarrClient
from store import TokenCrypto, UserStore
from webhook import attach_webhook, start_http_server
from webui import attach_webui, set_token_rejected
from _version import __version__ as USHER_VERSION

from bot.autofix_poll import poll_pending_autofixes
from bot.progress_card import poll_progress_cards
from bot.callback_prefixes import (
    ACCESS_ASK,
    ACCESS_CANCEL,
    ACCESS_IGNORE,
    ACCESS_SEND,
    ACCOUNT_NOTICES,
    ACCOUNT_UNLINK,
    ADMIN_INACTIVE,
    ADMIN_SHARES,
    ADMIN_STATUS,
    DIGEST_INFO,
    DOWNLOADS_LIMIT,
    NOTICE_TOGGLE,
    DOWNLOADS_PAUSE,
    DOWNLOADS_PAUSE_30,
    DOWNLOADS_RESUME,
    DOWNLOADS_VIEW,
    STOP_ASK,
    STOP_GO,
    STOP_MSG,
    HUB_OPEN,
    REQUESTS_MINE,
    REQUESTS_SUBSCRIPTIONS,
    MYSTATS,
    REQUESTS_TRENDING,
    TICKETS_LIST,
    DV_CATEGORY,
    DV_INFO,
    DV_PAGE,
    SUBSCRIPTION_ADD,
    FOLLOW_ADD,
    FOLLOW_DEL,
    FOLLOW_REMOVE,
    REQUESTS_FOLLOWS,
    SUBSCRIPTION_DEL,
    UNINV_GO,
    UNINV_KEEP,
    RQ_INFO_DISMISS,
    RQ_LIST_CANCEL,
    RQ_ADMIN_APPROVE,
    RQ_ADMIN_DECLINE,
    RQ_ADMIN_RETRY,
    LINK_HELP,
    SHARE_REMOVE,
    SHARE_MATCHES,
    SHARE_VIEW,
    TK_BACK,
    TK_BACK_DM,
    TK_BACK_LIST,
    TK_CLOSE,
    TK_CLOSE_DIRECT,
    TK_FIX,
    ASK_ADMIN,
    TK_FIX_MARK_FAILED,
    TK_FIX_REDOWNLOAD,
    TK_OPEN,
)
from bot.ticket_flow import _new_ticket_conversation
from bot.admin_hub import ADMIN_HUB, cmd_admin, refuse_outside_admin_dm
from bot.broadcast import (_BC_KEYS, _broadcast_conversation,
                           cancel_broadcast_tasks, resume_broadcasts)
from bot.downloads import downloads
from bot.nowplaying import now_playing_section, stop_ask, stop_confirmed
from bot.alarms import (ALARM_FIRST_DELAY_S, ALARM_INTERVAL_S, check_alarms,
                        status_section as alarms_section)
from bot.access_asks import (access_ask_cancel, access_ask_ignore,
                            access_ask_send, access_ask_start)
from bot.hub import end_markup, hub_open_callback
from bot.link_flow import (_link_conversation, account_unlink, cmd_account,
                           cmd_link_didnt_work, cmd_unlink)
from bot.mystats_cmd import cmd_mystats, mystats_button
from bot.discover_cmd import (cmd_trending, dv_category, dv_info, dv_page,
                              trending_hub)
from bot.invite_flow import (_INV_KEYS, _invite_conversation,
                             admin_inactive, admin_shares, cmd_uninvite,
                             share_matches, share_remove,
                             share_view, uninvite_cancel, uninvite_go)
from bot.digest import (DIGEST_FIRST_DELAY_S, DIGEST_INTERVAL_S,
                        account_notices, digest_info, digest_job,
                        notice_toggle)
from bot.follows import follow_add, follow_del, follow_remove, follows_hub
from bot.auto_follow import (AUTO_FOLLOW_FIRST_DELAY_S,
                             AUTO_FOLLOW_INTERVAL_S, auto_follow_job)
from bot.new_content import (NEW_CONTENT_FIRST_DELAY_S, NEW_CONTENT_INTERVAL_S,
                             poll_new_content)
from bot.subscriptions import (cmd_subscriptions, subscription_add,
                               subscription_del, subscriptions_hub)
from bot.request_flow import (_RQ_KEYS, _request_conversation,
                              request_info_dismiss)
from bot.requests_cmd import cmd_requests, requests_hub_mine, rq_list_cancel
from bot.request_admin import rq_admin_action
from bot.resolve_flow import _resolve_conversation
from bot.ask_admin import ask_admin
from bot.shared import (
    edit_or_send,
    force_end_flow_conversations,
    guard_flow_steps,
    format_status,
    global_btn_gate,
    record_btn,
    reset_stale_flows,
    schedule_clean_exit,
    unmatched_callback,
)
from bot.tickets import (
    _ticket_conversation,
    cmd_tickets,
    tickets_hub_list,
    tk_back,
    tk_close_direct,
    tk_close_menu,
    tk_fix,
    tk_fix_mark_failed,
    tk_fix_redownload,
    tk_open,
)
from bot.webhook_handlers import (
    handle_seerr_comment,
    handle_seerr_media,
    handle_seerr_reported,
    handle_seerr_resolved,
)
from const import (
    ADMIN_UPLOAD_MAX_BYTES,
    AUTOFIX_POLL_FIRST_DELAY_S,
    AUTOFIX_POLL_INTERVAL_S,
    CLIENT_CLOSE_GRACE_S,
    PROGRESS_CARD_POLL_INTERVAL_S,
)

_LOG_SECRETS: set[str] = set()
_REDACTED = "<bot-token>"


def register_log_secret(secret: str) -> None:
    """Have every log handler replace this value with a placeholder from now on."""
    if secret:
        _LOG_SECRETS.add(secret)


def unregister_log_secret(secret: str) -> None:
    _LOG_SECRETS.discard(secret)


def _redact(text: str) -> str:
    for secret in tuple(_LOG_SECRETS):
        text = text.replace(secret, _REDACTED)
    return text


class TokenRedactor(logging.Filter):
    """Strip registered secrets (the bot token) from the message, exception text and
    stack info. python-telegram-bot puts the token in InvalidToken's message."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _LOG_SECRETS:
            return True
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            message = str(record.msg)
        record.msg = _redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = _redact(record.exc_text)
        if record.stack_info:
            record.stack_info = _redact(record.stack_info)
        return True


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    level=logging.INFO,
)
for _handler in logging.getLogger().handlers:
    _handler.addFilter(TokenRedactor())
del _handler
# httpx logs every request URL at INFO; those carry the bot token and the SABnzbd API key.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("usher")


# App setup

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
    for key in ("seerr", "radarr", "sonarr", "sabnzbd", "tautulli"):
        c = app.bot_data.get(key)
        if c is not None and hasattr(c, "close"):
            old_clients.append((key, c))

    # One bad saved URL must not stop the others, or the whole start (the
    # panel that could fix it included): log it and leave that service off.
    def _client(name, factory, url, key):
        if not (url and key):
            return None
        try:
            return factory()
        except Exception:
            logger.error("%s URL in settings is not usable; %s stays off "
                         "until it is fixed in the Admin page", name, name)
            return None

    seerr = _client("Seerr", lambda: SeerrClient(
        s.seerr_url, s.seerr_api_key, public_url=s.seerr_public_url or None),
        s.seerr_url, s.seerr_api_key)
    radarr = _client("Radarr", lambda: RadarrClient(s.radarr_url, s.radarr_api_key),
                     s.radarr_url, s.radarr_api_key)
    sonarr = _client("Sonarr", lambda: SonarrClient(s.sonarr_url, s.sonarr_api_key),
                     s.sonarr_url, s.sonarr_api_key)
    sabnzbd = _client("SABnzbd", lambda: SabnzbdClient(s.sabnzbd_url, s.sabnzbd_api_key),
                      s.sabnzbd_url, s.sabnzbd_api_key)
    tautulli = _client("Tautulli", lambda: TautulliClient(s.tautulli_url, s.tautulli_api_key),
                       s.tautulli_url, s.tautulli_api_key)

    allowlist = set(s.allowed_autofix_telegram_ids)
    if not allowlist:
        allowlist = {admin_id}

    app.bot_data["seerr"] = seerr
    app.bot_data["radarr"] = radarr
    app.bot_data["sonarr"] = sonarr
    app.bot_data["sabnzbd"] = sabnzbd
    app.bot_data["tautulli"] = tautulli
    app.bot_data["allowlist"] = allowlist
    app.bot_data["autofix_allow_all"] = s.autofix_allow_all

    if old_clients:
        async def _close_old() -> None:
            # Grace before closing, mirroring the Seerr user-client cache:
            # an immediate close() kills any webhook handler, poll tick, or
            # bot handler still mid-request on a captured old reference. The
            # sleep is cancellable: _post_shutdown cancels the task and closes
            # these clients directly, so shutdown never blocks the full grace.
            try:
                await asyncio.sleep(CLIENT_CLOSE_GRACE_S)
            except asyncio.CancelledError:
                return  # shutdown path closes old_clients itself
            for key, client in old_clients:
                try:
                    await client.close()  # type: ignore[attr-defined]
                except Exception as exc:
                    logger.exception("Error closing prior %s client", key, exc_info=exc)
            # Grace elapsed and clients closed: drop them from the
            # shutdown-fallback list so it doesn't grow across reloads.
            tracked = app.bot_data.get("_pending_old_clients") or []
            app.bot_data["_pending_old_clients"] = [
                c for c in tracked if c not in {cl for _, cl in old_clients}]
        # Stash the clients too so shutdown can close them if it cancels the
        # grace timer mid-wait.
        app.bot_data.setdefault("_pending_old_clients", []).extend(
            c for _, c in old_clients)
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
        "Clients (re)built: Seerr=%s Radarr=%s Sonarr=%s SABnzbd=%s Tautulli=%s allowlist=%d",
        "yes" if seerr else "no",
        "yes" if radarr else "no",
        "yes" if sonarr else "no",
        "yes" if sabnzbd else "no",
        "yes" if tautulli else "no",
        len(allowlist),
    )


async def _check_connections(app: Application) -> dict[str, str]:
    """Probe configured services via each client's `ping()`. No private-attr
    access. Returns dict of service -> status string."""
    out: dict[str, str] = {"Usher": f"✅ {USHER_VERSION}"}
    for key, label in (("seerr", "Seerr"), ("radarr", "Radarr"),
                       ("sonarr", "Sonarr"), ("sabnzbd", "SABnzbd"),
                       ("tautulli", "Tautulli")):
        client = app.bot_data.get(key)
        if client is None:
            out[label] = "not configured"
            continue
        try:
            version = await client.ping()
            out[label] = f"✅ {version}"
        except Exception as exc:
            logger.exception("%s ping failed", label)
            out[label] = f"❌ {user_friendly_message(exc)}"
    # Webhook-agent check: every Ticket/Request DM depends on Seerr having
    # the webhook agent on with the right event types, and a miss is
    # otherwise SILENT (upgraded installs predate the request events). One
    # line here + the startup DM is the detection.
    seerr = app.bot_data.get("seerr")
    if seerr is not None and out.get("Seerr", "").startswith("✅"):
        try:
            enabled, types = await seerr.get_webhook_notification_settings()
            if not enabled:
                out["Seerr webhook"] = ("❌ webhook agent disabled in Seerr, "
                                        "no ticket/request DMs will arrive")
            else:
                missing = [label for bit, label in
                           REQUIRED_WEBHOOK_TYPES.items() if not types & bit]
                if missing:
                    out["Seerr webhook"] = ("⚠️ events not enabled: "
                                            + ", ".join(missing))
                else:
                    out["Seerr webhook"] = "✅ all events enabled"
        except Exception as exc:
            logger.warning("webhook settings check failed", exc_info=True)
            out["Seerr webhook"] = f"❓ couldn't check ({user_friendly_message(exc)})"
    return out


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    admin_id = ctx.bot_data.get("admin_id")
    is_admin = user_id == admin_id

    if not is_admin:
        # Non-admins see ONLY greeting + commands. No connection diagnostics,
        # no inline "DM me /link" directive (/account is listed below).
        await update.effective_message.reply_text(
            "Hi! I take movie/TV requests and file tickets in Seerr.\n\n"
            "*Commands*\n"
            "  /account - link or unlink your Plex account\n"
            "  /request - new request, your requests, trending, subscriptions\n"
            "  /tickets - report a problem or check on your tickets\n"
            "  /help - show this\n\n"
            "(/link, /unlink, /issue, /requests, /trending and /subscriptions "
            "still work when typed.)",
            parse_mode="Markdown",
        )
        return

    # Admin path: keeps the connection-status block + the /admin line.
    summary = await _check_connections(ctx.application)
    # HTML + escape: format_status interpolates upstream error text (see
    # the startup DM note in _post_init).
    lines = [
        "Hi! I take movie/TV requests and file tickets in Seerr.",
        "",
        "<b>Connection status:</b>",
        format_status(summary),
        "",
        "<b>Commands</b>",
        "  /account - link or unlink your Plex account",
        "  /request - new request, your requests, trending, subscriptions",
        "  /tickets - report a problem or check on your tickets",
        "  /admin - invite someone, see and remove Shares, connection status",
        "  /help - show this",
        "",
        "(/link, /unlink, /issue, /requests, /trending and /subscriptions "
        "still work when typed.)",
        "(/invite, /uninvite and /status still work when typed.)",
    ]
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode="HTML"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await refuse_outside_admin_dm(update, ctx):
        return
    summary = await _check_connections(ctx.application)
    playing, stop_rows = await now_playing_section(ctx)
    playing = await alarms_section(ctx) + playing
    sent = await update.effective_message.reply_text(
        f"<b>Connection status:</b>\n{format_status(summary)}{playing}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(stop_rows) if stop_rows else None,
    )
    if stop_rows:
        record_btn(ctx.application, update.effective_user.id, sent)


async def admin_hub_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin Hub "Status": the /status message, edited into the tapped Hub
    message with "⬅️ Admin". The probes can take a while with a service
    down, so the message says it is checking first."""
    if await refuse_outside_admin_dm(update, ctx):
        return
    q = update.callback_query
    await q.answer()
    force_end_flow_conversations(ctx, update)
    await edit_or_send(q, "📶 Checking connections…")
    summary = await _check_connections(ctx.application)
    playing, stop_rows = await now_playing_section(ctx)
    playing = await alarms_section(ctx) + playing
    sent = await edit_or_send(
        q, f"<b>Connection status:</b>\n{format_status(summary)}{playing}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            stop_rows + list(end_markup(ADMIN_HUB).inline_keyboard)))
    record_btn(ctx.application, update.effective_user.id, sent)


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
    app.bot_data["trusted_proxies"] = parse_trusted_proxies(
        os.environ.get("TRUSTED_PROXIES", ""))

    _build_clients_from_settings(app)

    # Flow reset gate: "most recent command wins". Group -2 so it ends any
    # in-progress conversation BEFORE the new command (and any text that
    # follows) is processed. See bot.shared.reset_stale_flows.
    link_conv = _link_conversation()
    new_ticket_conv = _new_ticket_conversation()
    request_conv = _request_conversation()
    invite_conv = _invite_conversation()
    broadcast_conv = _broadcast_conversation()
    resolve_conv = _resolve_conversation()
    ticket_conv = _ticket_conversation()
    # Registry for shared.user_in_conversation: lets one flow's
    # entry point refuse to start while another text-awaiting flow is active.
    app.bot_data["conversations"] = {
        "link": link_conv,
        "issue": new_ticket_conv,
        "request": request_conv,
        "invite": invite_conv,
        "broadcast": broadcast_conv,
        "resolve": resolve_conv,
        "ticket_reply": ticket_conv,
    }
    app.bot_data["flow_convs"] = [link_conv, new_ticket_conv, request_conv,
                                  invite_conv, broadcast_conv, resolve_conv,
                                  ticket_conv]
    for conv in app.bot_data["flow_convs"]:
        guard_flow_steps(conv)
    app.add_handler(TypeHandler(Update, reset_stale_flows), group=-2)

    # Global gate: drop callbacks from stale button-bearing messages. Group -1
    # so it fires before any normal handler.
    app.add_handler(TypeHandler(Update, global_btn_gate), group=-1)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(link_conv)
    app.add_handler(CommandHandler("unlink", cmd_unlink))
    app.add_handler(CommandHandler("account", cmd_account))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("tickets", cmd_tickets))
    app.add_handler(CommandHandler("requests", cmd_requests))
    app.add_handler(CommandHandler("trending", cmd_trending))
    app.add_handler(CommandHandler("mystats", cmd_mystats))
    app.add_handler(CallbackQueryHandler(mystats_button,
                                         pattern=fr"^{MYSTATS}$"))
    app.add_handler(CommandHandler("subscriptions", cmd_subscriptions))
    app.add_handler(new_ticket_conv)
    app.add_handler(request_conv)
    app.add_handler(invite_conv)
    app.add_handler(broadcast_conv)
    app.add_handler(CommandHandler("uninvite", cmd_uninvite))
    app.add_handler(resolve_conv)
    # Request-list cancel + detail-card dismiss: non-conversation callbacks,
    # live even when no conversation is active.
    app.add_handler(CallbackQueryHandler(rq_list_cancel, pattern=fr"^{RQ_LIST_CANCEL}:\d+$"))
    app.add_handler(CallbackQueryHandler(request_info_dismiss, pattern=fr"^{RQ_INFO_DISMISS}$"))
    # Approve / Decline / Retry on the Admin's Request DMs (webhook).
    app.add_handler(CallbackQueryHandler(
        rq_admin_action,
        pattern=fr"^({RQ_ADMIN_APPROVE}|{RQ_ADMIN_DECLINE}|{RQ_ADMIN_RETRY}):\d+$"))
    # /trending browse callbacks (category switch / paging / detail cards).
    app.add_handler(CallbackQueryHandler(dv_category, pattern=fr"^{DV_CATEGORY}:"))
    app.add_handler(CallbackQueryHandler(dv_page, pattern=fr"^{DV_PAGE}:"))
    app.add_handler(CallbackQueryHandler(dv_info, pattern=fr"^{DV_INFO}:"))
    # Subscription callbacks (detail-card 🔔 + /subscriptions ❌).
    app.add_handler(CallbackQueryHandler(subscription_add,
                                         pattern=fr"^{SUBSCRIPTION_ADD}:"))
    app.add_handler(CallbackQueryHandler(subscription_del,
                                         pattern=fr"^{SUBSCRIPTION_DEL}:"))
    # Follow callbacks (detail card, availability notices, Follow pings, and
    # the Requests Hub's Follows list).
    app.add_handler(CallbackQueryHandler(follow_add,
                                         pattern=fr"^{FOLLOW_ADD}:\d+$"))
    app.add_handler(CallbackQueryHandler(follow_del,
                                         pattern=fr"^{FOLLOW_DEL}:\d+$"))
    app.add_handler(CallbackQueryHandler(follow_remove,
                                         pattern=fr"^{FOLLOW_REMOVE}:\d+$"))
    # /uninvite confirm card (its own prefixes -- never collides with the
    # invite conversation's versioned invx:<v> cancel).
    app.add_handler(CallbackQueryHandler(uninvite_go,
                                         pattern=fr"^{UNINV_GO}:\d+:[ap]$"))
    app.add_handler(CallbackQueryHandler(uninvite_cancel,
                                         pattern=fr"^{UNINV_KEEP}$"))
    # Ticket-management callbacks (must be registered before the conversation
    # so the non-conversation taps -- open / close-menu / close-direct -- work
    # even when no conversation is active)
    app.add_handler(CallbackQueryHandler(tk_open, pattern=fr"^{TK_OPEN}:\d+$"))
    app.add_handler(CallbackQueryHandler(tk_close_menu, pattern=fr"^{TK_CLOSE}:\d+$"))
    app.add_handler(CallbackQueryHandler(tk_close_direct, pattern=fr"^{TK_CLOSE_DIRECT}:\d+$"))
    app.add_handler(CallbackQueryHandler(tk_fix, pattern=fr"^{TK_FIX}:\d+$"))
    app.add_handler(CallbackQueryHandler(tk_fix_redownload, pattern=fr"^{TK_FIX_REDOWNLOAD}:\d+$"))
    app.add_handler(CallbackQueryHandler(tk_fix_mark_failed, pattern=fr"^{TK_FIX_MARK_FAILED}:\d+$"))
    app.add_handler(CallbackQueryHandler(ask_admin, pattern=fr"^{ASK_ADMIN}:\d+$"))
    app.add_handler(CallbackQueryHandler(tk_back, pattern=fr"^({TK_BACK}|{TK_BACK_LIST}|{TK_BACK_DM}):\d+$"))
    app.add_handler(ticket_conv)
    app.add_handler(CallbackQueryHandler(cmd_link_didnt_work, pattern=fr"^{LINK_HELP}$"))
    # Hub back button + Account Hub Unlink (Link is a link-conversation entry).
    app.add_handler(CallbackQueryHandler(hub_open_callback,
                                         pattern=fr"^{HUB_OPEN}:[a-z]+$"))
    app.add_handler(CallbackQueryHandler(account_notices,
                                         pattern=fr"^{ACCOUNT_NOTICES}$"))
    app.add_handler(CallbackQueryHandler(
        notice_toggle, pattern=fr"^{NOTICE_TOGGLE}:(digest|auto)$"))
    app.add_handler(CallbackQueryHandler(
        digest_info, pattern=fr"^{DIGEST_INFO}:(movie|tv):[0-9]+$"))
    app.add_handler(CallbackQueryHandler(account_unlink,
                                         pattern=fr"^{ACCOUNT_UNLINK}$"))
    # Requests Hub list buttons (New request is a request-conversation entry).
    app.add_handler(CallbackQueryHandler(requests_hub_mine,
                                         pattern=fr"^{REQUESTS_MINE}$"))
    app.add_handler(CallbackQueryHandler(trending_hub,
                                         pattern=fr"^{REQUESTS_TRENDING}$"))
    app.add_handler(CallbackQueryHandler(subscriptions_hub,
                                         pattern=fr"^{REQUESTS_SUBSCRIPTIONS}$"))
    app.add_handler(CallbackQueryHandler(follows_hub,
                                         pattern=fr"^{REQUESTS_FOLLOWS}$"))
    # Tickets Hub list button, also a detail card's "⬅️ Tickets" (Report a
    # problem is a new-Ticket conversation entry).
    app.add_handler(CallbackQueryHandler(tickets_hub_list,
                                         pattern=fr"^{TICKETS_LIST}$"))
    # Admin Hub: Shares (list pages, a Share's view, its Remove) and Status
    # (Invite is an invite-conversation entry).
    app.add_handler(CallbackQueryHandler(admin_shares,
                                         pattern=fr"^{ADMIN_SHARES}:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_inactive,
                                         pattern=fr"^{ADMIN_INACTIVE}:\d+$"))
    app.add_handler(CallbackQueryHandler(share_view,
                                         pattern=fr"^{SHARE_VIEW}:\d+$"))
    app.add_handler(CallbackQueryHandler(share_remove,
                                         pattern=fr"^{SHARE_REMOVE}:\d+$"))
    app.add_handler(CallbackQueryHandler(share_matches,
                                         pattern=fr"^{SHARE_MATCHES}:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_hub_status,
                                         pattern=fr"^{ADMIN_STATUS}$"))
    app.add_handler(CallbackQueryHandler(
        downloads,
        pattern=(fr"^({DOWNLOADS_VIEW}|{DOWNLOADS_PAUSE}|{DOWNLOADS_PAUSE_30}"
                 fr"|{DOWNLOADS_RESUME}|{DOWNLOADS_LIMIT}:\d+)$")))
    # Access asks: the person's Ask / Send / Cancel and the Admin DM's Ignore
    # (Approve is an invite-conversation entry).
    app.add_handler(CallbackQueryHandler(access_ask_start,
                                         pattern=fr"^{ACCESS_ASK}$"))
    app.add_handler(CallbackQueryHandler(access_ask_send,
                                         pattern=fr"^{ACCESS_SEND}$"))
    app.add_handler(CallbackQueryHandler(access_ask_cancel,
                                         pattern=fr"^{ACCESS_CANCEL}$"))
    app.add_handler(CallbackQueryHandler(access_ask_ignore,
                                         pattern=fr"^{ACCESS_IGNORE}:\d+$"))
    app.add_handler(CallbackQueryHandler(stop_ask,
                                         pattern=fr"^{STOP_ASK}:\w+$"))
    app.add_handler(CallbackQueryHandler(
        stop_confirmed, pattern=fr"^({STOP_GO}|{STOP_MSG}):\w+$"))
    # MUST stay the last callback handler in the default group:
    # catches taps on keyboards whose conversation has timed out or ended.
    app.add_handler(CallbackQueryHandler(unmatched_callback))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(
        poll_progress_cards,
        interval=PROGRESS_CARD_POLL_INTERVAL_S,
        first=AUTOFIX_POLL_FIRST_DELAY_S,
        name="progress_card_poll",
    )
    app.job_queue.run_repeating(
        poll_pending_autofixes,
        interval=AUTOFIX_POLL_INTERVAL_S,
        first=AUTOFIX_POLL_FIRST_DELAY_S,
        name="autofix_poller",
    )
    app.job_queue.run_repeating(
        check_alarms,
        interval=ALARM_INTERVAL_S,
        first=ALARM_FIRST_DELAY_S,
        name="service_alarms",
    )
    app.job_queue.run_repeating(
        poll_new_content,
        interval=NEW_CONTENT_INTERVAL_S,
        first=NEW_CONTENT_FIRST_DELAY_S,
        name="new_content_poll",
    )
    app.job_queue.run_repeating(
        auto_follow_job,
        interval=AUTO_FOLLOW_INTERVAL_S,
        first=AUTO_FOLLOW_FIRST_DELAY_S,
        name="auto_follow",
    )
    app.job_queue.run_repeating(
        digest_job,
        interval=DIGEST_INTERVAL_S,
        first=DIGEST_FIRST_DELAY_S,
        name="digest",
    )
    # Keep autofix bookkeeping tables from growing for the life of the
    # install (also pruned once at startup in _post_init).
    app.job_queue.run_repeating(
        _prune_autofix_history_job,
        interval=24 * 3600,
        first=24 * 3600,
        name="autofix_history_pruner",
    )

    return app


async def _prune_autofix_history_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    store: UserStore = ctx.application.bot_data["store"]
    try:
        pruned = await store.prune_autofix_history()
        if any(pruned):
            logger.info("Pruned autofix history: %d pending rows, %d events", *pruned)
    except Exception as exc:
        logger.exception("Autofix-history prune failed (will retry tomorrow)", exc_info=exc)


# Telegram's command menu, registered at every startup so it always matches
# the code. Without this the menu is whatever was last typed into BotFather
# by hand and silently goes stale when commands are added (0.15.0 shipped
# /request with no menu entry).
_BOT_COMMANDS = [
    ("account", "your Plex link: link or unlink"),
    ("request", "new request, your requests, trending, subscriptions"),
    ("tickets", "report a problem or check on your tickets"),
    ("help", "show commands and status"),
]
_ADMIN_BOT_COMMANDS = _BOT_COMMANDS + [
    ("admin", "invite someone, Shares, connection status"),
]


async def _register_commands(app: Application) -> None:
    """Publish the command menu: the default scope for everyone, plus an
    admin-chat scope that appends /admin. Failure is non-fatal (the menu
    is cosmetic; commands work regardless)."""
    from telegram import BotCommand, BotCommandScopeChat
    try:
        await app.bot.set_my_commands(
            [BotCommand(c, d) for c, d in _BOT_COMMANDS])
        admin_id = app.bot_data.get("admin_id")
        if admin_id:
            await app.bot.set_my_commands(
                [BotCommand(c, d) for c, d in _ADMIN_BOT_COMMANDS],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        logger.info("Registered %d bot commands (+admin scope)",
                    len(_BOT_COMMANDS))
    except Exception as exc:
        logger.exception("set_my_commands failed (menu may be stale)", exc_info=exc)


async def _post_init(app: Application) -> None:
    """Start the HTTP server (webhook + webui), run startup checks, DM admin.
    Server first: the probes retry with backoff and can take 1-2 minutes
    with all three services down, during which /healthz and the webhook
    receiver would look dead to health checks."""
    await _register_commands(app)
    web_app = web.Application(client_max_size=ADMIN_UPLOAD_MAX_BYTES)

    async def _on_comment(payload: dict) -> None:
        await handle_seerr_comment(app, payload)

    async def _on_resolved(payload: dict) -> None:
        await handle_seerr_resolved(app, payload)

    async def _on_reported(payload: dict) -> None:
        await handle_seerr_reported(app, payload)

    async def _on_media(payload: dict) -> None:
        await handle_seerr_media(app, payload)

    def _secret_provider() -> str:
        settings_store: SettingsStore = app.bot_data["settings_store"]
        return settings_store.settings.webhook_secret or ""

    settings_store: SettingsStore = app.bot_data["settings_store"]
    boot_token = settings_store.settings.telegram_bot_token
    boot_admin_id = settings_store.settings.admin_telegram_id

    async def _on_settings_changed() -> None:
        s = settings_store.settings
        register_log_secret(s.telegram_bot_token)
        if s.telegram_bot_token != boot_token or s.admin_telegram_id != boot_admin_id:
            logger.info("Bot token or admin id changed; exiting in 2s to restart")
            app.bot_data["shutting_down"] = True
            schedule_clean_exit(2.0)
            return
        logger.info("Settings changed; rebuilding clients")
        _build_clients_from_settings(app)

    attach_webhook(
        web_app,
        on_comment=_on_comment,
        on_resolved=_on_resolved,
        on_reported=_on_reported,
        on_media=_on_media,
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
        trusted_proxies=app.bot_data["trusted_proxies"],
        http_port=app.bot_data["http_port"],
        user_store=app.bot_data["store"],
        enc_key_path=_encryption_key_path(app.bot_data["data_dir"]),
    )

    runner = await start_http_server(
        web_app,
        host=app.bot_data["http_bind"],
        port=app.bot_data["http_port"],
    )
    app.bot_data["http_runner"] = runner

    summary = await _check_connections(app)
    logger.info("Startup checks: %s", " | ".join(f"{k}={v}" for k, v in summary.items()))

    store: UserStore = app.bot_data["store"]
    try:
        pruned = await store.prune_autofix_history()
        if any(pruned):
            logger.info("Pruned autofix history: %d pending rows, %d events", *pruned)
    except Exception as exc:
        logger.exception("Startup autofix-history prune failed (non-fatal)", exc_info=exc)

    admin_id = app.bot_data["admin_id"]
    base = public_base_url(settings_store.settings.usher_public_url)
    if base:
        admin_url = f"{base}/admin"
    else:
        admin_url = f"http://<host>:{app.bot_data['http_port']}/admin"
    # HTML + escape like every other dynamic surface: format_status carries
    # upstream error bodies, and one unbalanced Markdown metacharacter in
    # them makes Telegram reject the whole message -- exactly when a
    # service is failing and the admin most needs the DM.
    msg = (
        f"👋 Bot is online (v{html.escape(USHER_VERSION)}).\n\n"
        f"{format_status(summary)}\n\n"
        f"Admin UI: {html.escape(admin_url)}\n"
        "Run /link to authorize with Plex (per-user ticket attribution)."
    )
    try:
        await app.bot.send_message(chat_id=admin_id, text=msg, parse_mode="HTML")
    except telegram.error.Forbidden:
        logger.warning(
            "Couldn't DM admin %d on startup: the bot is blocked by the admin. "
            "Admin must unblock the bot in Telegram (or /start the bot first).",
            admin_id,
        )
    except telegram.error.BadRequest as exc:
        msg_lower = str(exc).lower()
        if "chat not found" in msg_lower:
            logger.warning(
                "Couldn't DM admin %d on startup: chat not found. "
                "admin_telegram_id may be wrong -- it must be a numeric user ID "
                "(from @userinfobot), not a username.",
                admin_id,
            )
        elif "user is deactivated" in msg_lower:
            logger.warning(
                "Couldn't DM admin %d on startup: user is deactivated.",
                admin_id,
            )
        else:
            logger.warning("Couldn't DM admin %d on startup: %s", admin_id, exc)
    except Exception as exc:
        logger.exception("Couldn't DM admin %d on startup (unexpected)", admin_id, exc_info=exc)

    # A restart mid-Broadcast: carry on with the recipients not yet marked.
    await resume_broadcasts(app)

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
            "encryption key rotated or USHER_ENCRYPTION_KEY changed."
        )
        try:
            await app.bot.send_message(chat_id=admin_id, text=warn)
        except Exception as exc:
            logger.warning("Couldn't DM admin %d about %d decrypt failure(s): %s", admin_id, n_failed, exc)


async def _post_shutdown(app: Application) -> None:
    await cancel_broadcast_tasks(app)
    # Cancel any prior settings-reload close tasks: each begins with a 90s
    # grace sleep, and awaiting them here would block shutdown for up to that
    # long (SIGKILL under docker stop's 10s grace). Cancel, settle, then
    # close the retired clients directly since their timers won't fire.
    pending_closes = app.bot_data.get("_pending_closes") or []
    for task in pending_closes:
        task.cancel()
    if pending_closes:
        await asyncio.gather(*pending_closes, return_exceptions=True)
    for client in app.bot_data.get("_pending_old_clients") or []:
        try:
            await client.close()
        except Exception as exc:
            logger.exception("Error closing retired client on shutdown", exc_info=exc)

    # Close current API clients explicitly so httpx connection pools don't
    # leak. PlexClient is built once at startup and stashed under "plex";
    # the arr clients are managed by _build_clients_from_settings.
    for key in ("seerr", "radarr", "sonarr", "sabnzbd", "tautulli", "plex"):
        client = app.bot_data.get(key)
        if client is not None and hasattr(client, "close"):
            try:
                await client.close()
            except Exception as exc:
                logger.exception("Error closing %s on shutdown", key, exc_info=exc)

    runner = app.bot_data.get("http_runner")
    if runner is not None:
        try:
            await runner.cleanup()
            logger.info("HTTP server stopped")
        except Exception as exc:
            logger.exception("HTTP server cleanup failed", exc_info=exc)


# user_data keys populated by the ConversationHandlers (link, new Ticket,
# resolve, ticket-reply, request, invite). Cleared on error so a
# mid-conversation crash doesn't leak half-state into the next flow.
_CONVERSATION_USER_DATA_KEYS = (
    "tk_reply_id", "tk_close_after",
    "link_active_loop", "link_retry_ok", "hub_origin",
    "media", "search_results", "seasons", "season",
    "episode", "issue_type", "description",
    "search_version", "submitting_ticket",
    "research_parent", "_pending_autofix_id",
    "awaiting_comment_for",
) + _RQ_KEYS + (
    # rq_jump_media is ticket_flow's stash for the Request-it-instead jump
    # (request_flow._RQ_KEYS rightly omits it). rq_search_version is
    # deliberately in NEITHER list: the version counter must survive every
    # reset or a still-gated message from a prior flow could collide with a
    # fresh flow's counter restarting at 1.
    "rq_jump_media",
) + _INV_KEYS + _BC_KEYS  # inv_version deliberately excluded (see invite_flow._INV_KEYS)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # A double-tapped button (nav/cancel/skip) makes the second edit raise
    # BadRequest("message is not modified") -- a non-event. Treat it as a
    # no-op: don't log a scary traceback, don't wipe live conversation state
    # (which would strand the flow), don't DM the user about a phantom crash.
    err = ctx.error
    if isinstance(err, telegram.error.BadRequest) and \
            "message is not modified" in str(err).lower():
        return
    logger.exception("Unhandled error: %s", ctx.error)
    # Clear any half-populated conversation state so the next conversation
    # entry sees a clean slate. ctx.user_data may be None on errors that
    # fire outside a per-user context (e.g., job-queue exceptions); guard.
    try:
        if ctx.user_data is not None:
            for key in _CONVERSATION_USER_DATA_KEYS:
                ctx.user_data.pop(key, None)
    except Exception as exc:
        logger.debug("on_error user_data cleanup failed (non-fatal)", exc_info=exc)
    # Last-resort user feedback: without it a crash mid-flow is a silent
    # dead end (the flow evaporates and the next message hits a bot that
    # forgot the conversation). Best-effort only.
    try:
        if (isinstance(update, Update) and update.effective_chat is not None
                and ctx.error is not None):
            await ctx.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Something went wrong on my end. Please run the "
                     "command again.",
            )
    except Exception as exc:
        logger.debug("on_error user notification failed (non-fatal)", exc_info=exc)


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


def _encryption_key_path(data_dir: Path) -> Path:
    """The key file TokenCrypto loads: ENCRYPTION_KEY_PATH, else data_dir/encryption.key."""
    return Path(os.environ.get("ENCRYPTION_KEY_PATH", str(data_dir / "encryption.key")))


async def _run_setup_only(settings_store: SettingsStore, session_secret: bytes,
                          data_dir: Path, settings_path: Path, db_path: str,
                          http_port: int, http_bind: str,
                          token_rejected: bool = False) -> None:
    """Run a webui-only server until the user completes setup (then exit so the
    container restarts and main() picks up the configured-mode path). With
    token_rejected the bot is configured but Telegram refused its token: the
    panel stays up until the token is changed."""
    boot_token = settings_store.settings.telegram_bot_token
    if token_rejected:
        set_token_rejected(True)
        logger.error(
            "Telegram rejected the bot token. The bot is not running; the Admin page "
            "stays up at http://<host>:%d/admin. Save a new token there to restart.",
            http_port,
        )
    else:
        logger.warning(
            "Bot is NOT configured (telegram_bot_token / admin_telegram_id missing). "
            "Running in SETUP-ONLY mode. Open http://<host>:%d/admin to finish setup.",
            http_port,
        )
    web_app = web.Application(client_max_size=ADMIN_UPLOAD_MAX_BYTES)

    async def _on_settings_changed() -> None:
        register_log_secret(settings_store.settings.telegram_bot_token)
        if token_rejected:
            if settings_store.settings.telegram_bot_token != boot_token:
                logger.info("Bot token changed; exiting in 2s to restart into full mode")
                schedule_clean_exit(2.0)
        elif settings_store.settings.is_bot_configured():
            logger.info("Setup complete; exiting in 2s so container restarts into full mode")
            schedule_clean_exit(2.0)

    attach_webui(
        web_app,
        settings_store=settings_store,
        session_secret=session_secret,
        data_dir=data_dir,
        settings_path=settings_path,
        db_path=Path(db_path),
        on_settings_changed=_on_settings_changed,
        trusted_proxies=parse_trusted_proxies(os.environ.get("TRUSTED_PROXIES", "")),
        http_port=http_port,
        enc_key_path=_encryption_key_path(data_dir),
    )
    runner = await start_http_server(web_app, host=http_bind, port=http_port)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows event loops: keep the default signal behaviour
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await runner.cleanup()


def _run_polling(app: Application) -> None:
    try:
        app.run_polling(drop_pending_updates=True)
    except InvalidToken:
        logger.error("Telegram rejected the bot token. Check the token in the Admin "
                     "page (Settings).")
        bot_data = app.bot_data
        asyncio.run(_run_setup_only(
            bot_data["settings_store"], bot_data["session_secret"], bot_data["data_dir"],
            bot_data["settings_path"], bot_data["db_path"], bot_data["http_port"], bot_data["http_bind"],
            token_rejected=True,
        ))


def main() -> None:
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    db_path = os.environ.get("STORE_PATH", str(data_dir / "mappings.sqlite"))
    settings_path = data_dir / "settings.json"
    http_port = int(os.environ.get("WEBHOOK_PORT", "8765"))
    http_bind = os.environ.get("WEBHOOK_BIND", "0.0.0.0").strip() or "0.0.0.0"

    settings_store = SettingsStore(settings_path)
    _migrate_legacy_env_into_settings(settings_store)
    register_log_secret(settings_store.settings.telegram_bot_token)
    session_secret = load_or_create_session_secret(data_dir / "session_secret")
    crypto = TokenCrypto(key_path=_encryption_key_path(data_dir))
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
    _run_polling(app)


if __name__ == "__main__":
    main()
