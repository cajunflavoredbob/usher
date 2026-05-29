"""Slim aiohttp admin webui.

Routes:
  GET  /admin/setup    -- first-run admin account creation
  POST /admin/setup
  GET  /admin/login
  POST /admin/login
  GET  /admin/logout
  GET  /admin          -- settings page (auth required)
  POST /admin          -- save settings
  POST /admin/password -- change admin password
  GET  /admin/backup   -- download backup ZIP
  POST /admin/restore  -- upload backup ZIP and exit (container restarts)

Inline HTML, no template engine. Session cookie is signed with HMAC-SHA256
using a per-install secret persisted under /data.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import io
import json
import logging
import os
import shutil
import signal
import sqlite3
import tempfile
import time
import zipfile
from hashlib import sha256
from pathlib import Path
from typing import Awaitable, Callable, Optional

from aiohttp import web

from auth_util import (
    CSRF_COOKIE,
    CSRF_FORM_FIELD,
    LoginThrottle,
    attach_csrf_cookie,
    audit,
    client_ip,
    clear_setup_token,
    csrf_for_request,
    generate_csrf_token,
    load_or_create_setup_token,
    request_is_secure,
    validate_csrf,
)
from backup_crypto import is_wrapped, unwrap, wrap
from settings import (
    DEFAULT_DAILY_AUTOFIX_LIMIT,
    Settings,
    SettingsStore,
    hash_password,
    validate_public_url,
    verify_password,
)
from _version import __version__ as HERMES_VERSION

logger = logging.getLogger("hermes.webui")

SESSION_COOKIE = "hermes_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600

ReloadCallback = Callable[[], Awaitable[None]]

# Single in-memory throttle shared by all login_post invocations in this process.
_throttle = LoginThrottle()


def _set_session_cookie(resp, cookie_value: str, *, secure: bool) -> None:
    resp.set_cookie(
        SESSION_COOKIE, cookie_value,
        max_age=SESSION_TTL_SECONDS,
        httponly=True, samesite="Lax", secure=secure,
    )


def _schedule_clean_exit(delay_s: float = 2.0) -> None:
    """Send SIGTERM to self after `delay_s` so PTB's run_polling and
    aiohttp's runner unwind cleanly (closing httpx clients, DB
    connections, the HTTP server). Falls back to os._exit only if
    the SIGTERM dispatch itself fails."""
    loop = asyncio.get_running_loop()
    def _kill():
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception:
            logger.exception("SIGTERM dispatch failed; falling back to os._exit")
            os._exit(0)
    loop.call_later(delay_s, _kill)


def _csrf_input(token: str) -> str:
    """HTML hidden field for double-submit CSRF validation."""
    return f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{_esc(token)}">'


# --- Session helpers --------------------------------------------------------

def _sign(secret: bytes, data: bytes) -> bytes:
    return hmac.new(secret, data, sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _make_session_cookie(secret: bytes, username: str) -> str:
    payload = json.dumps({"u": username, "exp": int(time.time()) + SESSION_TTL_SECONDS}).encode()
    body = _b64(payload)
    sig = _b64(_sign(secret, body.encode()))
    return f"{body}.{sig}"


def _verify_session_cookie(secret: bytes, cookie: str) -> Optional[str]:
    try:
        body, sig = cookie.split(".")
    except ValueError:
        return None
    expected_sig = _b64(_sign(secret, body.encode()))
    if not hmac.compare_digest(sig, expected_sig):
        return None
    try:
        payload = json.loads(_b64d(body))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload.get("u")


def _current_user(request: web.Request) -> Optional[str]:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    return _verify_session_cookie(request.app["session_secret"], cookie)


# --- HTML rendering ---------------------------------------------------------

def _esc(s) -> str:
    if not s:
        return ""
    return (str(s).replace("&", "&amp;")
                  .replace("<", "&lt;")
                  .replace(">", "&gt;")
                  .replace('"', "&quot;"))


CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #1e1e2e; color: #cdd6f4; margin: 0; padding: 20px; line-height: 1.5; }
.container { max-width: 760px; margin: 0 auto; }
h1, h2 { color: #f5e0dc; }
h2 { margin-top: 0; border-bottom: 1px solid #45475a; padding-bottom: 6px; }
form { background: #313244; padding: 20px; border-radius: 8px; margin-bottom: 20px; }
label { display: block; margin: 12px 0 4px; font-weight: 600; color: #cba6f7; }
input[type="text"], input[type="password"], input[type="file"], textarea {
  width: 100%; box-sizing: border-box; padding: 8px;
  border: 1px solid #45475a; border-radius: 4px;
  background: #1e1e2e; color: #cdd6f4; font-size: 14px; }
button {
  background: #89b4fa; color: #1e1e2e; border: none;
  padding: 10px 20px; border-radius: 4px; cursor: pointer;
  font-weight: 600; margin-top: 16px; font-size: 14px; }
button:hover { background: #74c7ec; }
button.danger { background: #f38ba8; }
button.danger:hover { background: #eba0ac; }
.error { background: #f38ba8; color: #1e1e2e; padding: 10px 14px; border-radius: 4px; margin-bottom: 14px; }
.success { background: #a6e3a1; color: #1e1e2e; padding: 10px 14px; border-radius: 4px; margin-bottom: 14px; }
.note { color: #a6adc8; font-size: 13px; margin-top: 4px; }
a { color: #89b4fa; }
code { background: #45475a; padding: 2px 6px; border-radius: 3px; font-size: 13px; }

.topbar { display: flex; justify-content: flex-end; align-items: center;
          gap: 14px; margin-bottom: 14px; }
.topbar .version { color: #a6adc8; font-size: 13px;
                   font-family: ui-monospace, Menlo, monospace; }
.topbar .logout {
  background: #45475a; color: #cdd6f4; text-decoration: none;
  padding: 6px 14px; border-radius: 4px; font-size: 13px; font-weight: 600;
}
.topbar .logout:hover { background: #585b70; color: #f5e0dc; }
.intro { color: #a6adc8; margin: 0 0 14px 0; font-size: 14px; }
.saved-marker {
  display: inline-block; margin-left: 12px; font-weight: 600; font-size: 13px;
  vertical-align: middle;
}
.saved-marker.ok {
  color: #a6e3a1;
  animation: fade-out 1s ease-in-out 3s forwards;
}
.saved-marker.err { color: #f38ba8; }
@keyframes fade-out {
  to { opacity: 0; visibility: hidden; }
}

/* Tabs (CSS-only via radio inputs) */
.tabs > input[type="radio"] { position: absolute; left: -9999px; }
.tab-labels { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 0;
              border-bottom: 2px solid #45475a; }
.tab-labels label {
  display: inline-block; padding: 10px 18px; cursor: pointer;
  background: #313244; color: #a6adc8; font-weight: 600;
  border-radius: 6px 6px 0 0; margin: 0; user-select: none;
  border: 1px solid transparent; border-bottom: none;
}
.tab-labels label:hover { color: #cdd6f4; }
#tab-telegram:checked ~ .tab-labels label[for="tab-telegram"],
#tab-seerr:checked    ~ .tab-labels label[for="tab-seerr"],
#tab-autofix:checked  ~ .tab-labels label[for="tab-autofix"],
#tab-webhook:checked  ~ .tab-labels label[for="tab-webhook"],
#tab-account:checked  ~ .tab-labels label[for="tab-account"] {
  background: #313244; color: #f5e0dc; border-color: #45475a;
  border-bottom: 2px solid #313244; margin-bottom: -2px;
}
.tab-content { display: none; }
#tab-telegram:checked ~ .tab-contents .tab-c-telegram,
#tab-seerr:checked    ~ .tab-contents .tab-c-seerr,
#tab-autofix:checked  ~ .tab-contents .tab-c-autofix,
#tab-webhook:checked  ~ .tab-contents .tab-c-webhook,
#tab-account:checked  ~ .tab-contents .tab-c-account { display: block; }
.url-box {
  background: #1e1e2e; border: 1px solid #45475a; border-radius: 4px;
  padding: 10px 12px; font-family: ui-monospace, Menlo, monospace; font-size: 13px;
  color: #a6e3a1; word-break: break-all;
}
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html>\n"
        f"<html><head><meta charset=\"utf-8\"><title>{_esc(title)} - Hermes</title>"
        f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<style>{CSS}</style></head><body><div class=\"container\">{body}"
        "</div></body></html>"
    )


def _flash(message: str = "", error: str = "") -> str:
    out = ""
    if message:
        out += f'<div class="success">{_esc(message)}</div>'
    if error:
        out += f'<div class="error">{_esc(error)}</div>'
    return out


TAB_KEYS = ("telegram", "seerr", "autofix", "webhook", "account")


def _settings_page(
    s: Settings,
    *,
    message: str = "",
    error: str = "",
    active_tab: str = "telegram",
    webhook_url: str = "",
    marker_target: str = "",
    csrf_token: str = "",
) -> str:
    if active_tab not in TAB_KEYS:
        active_tab = "telegram"
    ids_str = ",".join(str(i) for i in s.allowed_autofix_telegram_ids)
    admin_tg_val = str(s.admin_telegram_id) if s.admin_telegram_id else ""
    csrf = _csrf_input(csrf_token)

    # Inline marker next to the relevant form's Save button. marker_target
    # defaults to the active tab so the obvious case Just Works.
    target = marker_target or active_tab

    def chk(key: str) -> str:
        return ' checked' if active_tab == key else ''

    def marker(target_name: str) -> str:
        if target != target_name:
            return ""
        if error:
            return f'<span class="saved-marker err">✗ {_esc(error)}</span>'
        if message:
            return f'<span class="saved-marker ok">✓ {_esc(message)}</span>'
        return ""

    telegram_form = f"""
<form method="POST" action="/admin/telegram">
  {csrf}
  <h2>Telegram</h2>
  <div class="note">Changes to the bot token or admin user ID restart the container so the new identity takes effect.</div>
  <label>Bot Token <span class="note">(from @BotFather)</span></label>
  <input type="password" name="telegram_bot_token" value="{_esc(s.telegram_bot_token)}" required>
  <label>Admin Telegram User ID <span class="note">(DM @userinfobot)</span></label>
  <input type="text" name="admin_telegram_id" value="{_esc(admin_tg_val)}" inputmode="numeric" pattern="[0-9]+" required>
  <label>Hermes Admin UI URL <span class="note">(optional)</span></label>
  <input type="text" name="hermes_public_url" value="{_esc(s.hermes_public_url)}" placeholder="http://192.168.1.15:8765 or https://hermes.example.com">
  <div class="note">Used in the bot's startup DM to point you back here. Leave blank to fall back to a generic placeholder.</div>
  <button type="submit">Save</button>{marker("telegram")}
</form>
"""

    seerr_form = f"""
<form method="POST" action="/admin/seerr">
  {csrf}
  <h2>Seerr</h2>
  <label>Seerr URL</label>
  <input type="text" name="seerr_url" value="{_esc(s.seerr_url)}" placeholder="http://192.168.1.10:5056" required>
  <label>Seerr API Key</label>
  <input type="password" name="seerr_api_key" value="{_esc(s.seerr_api_key)}" required>
  <label>Seerr Public URL <span class="note">(optional, for reverse-proxy links sent to users)</span></label>
  <input type="text" name="seerr_public_url" value="{_esc(s.seerr_public_url)}" placeholder="https://seerr.example.com">
  <button type="submit">Save</button>{marker("seerr")}
</form>
"""

    autofix_form = f"""
<form method="POST" action="/admin/autofix">
  {csrf}
  <h2>Auto-fix (Radarr / Sonarr)</h2>
  <p class="intro">When a user reports a Video, Audio, or Subtitle issue, Hermes can ask Radarr or Sonarr to delete the current file and trigger a new search. Configure the URLs and API keys below, then list the Telegram users allowed to use it. The admin always bypasses the per-day limit.</p>

  <label>Radarr URL <span class="note">(optional)</span></label>
  <input type="text" name="radarr_url" value="{_esc(s.radarr_url)}" placeholder="http://192.168.1.10:7878">
  <label>Radarr API Key</label>
  <input type="password" name="radarr_api_key" value="{_esc(s.radarr_api_key)}">

  <label>Sonarr URL <span class="note">(optional)</span></label>
  <input type="text" name="sonarr_url" value="{_esc(s.sonarr_url)}" placeholder="http://192.168.1.10:8989">
  <label>Sonarr API Key</label>
  <input type="password" name="sonarr_api_key" value="{_esc(s.sonarr_api_key)}">

  <label>Allowed Telegram User IDs</label>
  <input type="text" name="allowed_autofix_telegram_ids" value="{_esc(ids_str)}" placeholder="123456,789012">
  <div class="note">Comma-separated. Leave empty for admin-only.</div>

  <label>Per-user daily limit</label>
  <input type="text" name="daily_autofix_limit" value="{_esc(s.daily_autofix_limit)}" inputmode="numeric" pattern="[0-9]+" required>
  <div class="note">Number of auto-fix runs each non-admin user gets per 24 hours. Default {DEFAULT_DAILY_AUTOFIX_LIMIT}.</div>

  <button type="submit">Save</button>{marker("autofix")}
</form>
"""

    webhook_form = f"""
<form method="POST" action="/admin/webhook">
  {csrf}
  <h2>Webhook</h2>
  <p>Hermes receives webhook events from Seerr on this URL:</p>
  <div class="url-box">{_esc(webhook_url)}</div>
  <div class="note">Configure in Seerr: Settings → Notifications → Webhook. Set the URL above and enable the <strong>Issue Comment</strong> event.</div>

  <label>Webhook Secret <span class="note">(optional)</span></label>
  <input type="password" name="webhook_secret" value="{_esc(s.webhook_secret)}">
  <div class="note">If set, paste the same value into Seerr's Webhook <code>Authorization Header</code> field. Hermes rejects requests without a matching header.</div>

  <button type="submit">Save</button>{marker("webhook")}
</form>
"""

    account_section = f"""
<form method="POST" action="/admin/password">
  {csrf}
  <h2>Change Password</h2>
  <label>Current password</label>
  <input type="password" name="current" required>
  <label>New password</label>
  <input type="password" name="new" required minlength="8">
  <label>Confirm new password</label>
  <input type="password" name="confirm" required minlength="8">
  <button type="submit">Change Password</button>{marker("account")}
</form>

<form method="POST" action="/admin/backup">
  {csrf}
  <h2>Download Backup</h2>
  <div class="note">Downloads a ZIP containing settings.json, the mappings database, and the encryption key. Treat this file as secret. Optionally wrap it with a passphrase (PBKDF2 + AES-GCM).</div>
  <label>Passphrase <span class="note">(optional)</span></label>
  <input type="password" name="passphrase" placeholder="Leave blank for plain ZIP">
  <button type="submit">Download Backup</button>
</form>

<form method="POST" action="/admin/restore" enctype="multipart/form-data">
  {csrf}
  <h2>Restore from Backup</h2>
  <input type="file" name="backup" accept=".zip,.hermes-backup" required>
  <label>Passphrase <span class="note">(required only if the backup was wrapped)</span></label>
  <input type="password" name="passphrase" placeholder="Leave blank for plain ZIP">
  <div class="note">Overwrites settings, mappings DB, and encryption key after validating them. Current files are copied to <code>/data/pre-restore-TIMESTAMP/</code> first. The container restarts.</div>
  <button type="submit" class="danger">Restore</button>{marker("restore")}
</form>
"""

    return _page("Admin", f"""
<div class="topbar">
  <span class="version">Hermes v{_esc(HERMES_VERSION)}</span>
  <a href="/admin/logout" class="logout">Log out</a>
</div>
<div class="tabs">
  <input type="radio" name="tab" id="tab-telegram"{chk('telegram')}>
  <input type="radio" name="tab" id="tab-seerr"{chk('seerr')}>
  <input type="radio" name="tab" id="tab-autofix"{chk('autofix')}>
  <input type="radio" name="tab" id="tab-webhook"{chk('webhook')}>
  <input type="radio" name="tab" id="tab-account"{chk('account')}>
  <div class="tab-labels">
    <label for="tab-telegram">Telegram</label>
    <label for="tab-seerr">Seerr</label>
    <label for="tab-autofix">Auto-fix</label>
    <label for="tab-webhook">Webhook</label>
    <label for="tab-account">Account</label>
  </div>
  <div class="tab-contents">
    <div class="tab-content tab-c-telegram">{telegram_form}</div>
    <div class="tab-content tab-c-seerr">{seerr_form}</div>
    <div class="tab-content tab-c-autofix">{autofix_form}</div>
    <div class="tab-content tab-c-webhook">{webhook_form}</div>
    <div class="tab-content tab-c-account">{account_section}</div>
  </div>
</div>
""")


def _webhook_url_from_request(request: web.Request) -> str:
    """Construct the webhook URL using the request's Host header so users see
    the actual host:port they hit (works behind reverse proxies that set Host)."""
    scheme = request.headers.get("X-Forwarded-Proto") or request.scheme or "http"
    host = request.host
    return f"{scheme}://{host}/webhook/seerr"


# --- Route handlers ---------------------------------------------------------

async def setup_get(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    if store.settings.admin.is_set():
        return web.HTTPFound("/admin/login")
    setup_token = load_or_create_setup_token(request.app["data_dir"])
    csrf = csrf_for_request(request)
    s = store.settings
    admin_tg_val = str(s.admin_telegram_id) if s.admin_telegram_id else ""

    token_field = ""
    if setup_token:
        token_field = """
  <h2>Setup Token</h2>
  <p class="note">A one-time setup token was printed to the container logs on first run.
  Paste it here to prove you have host access (run <code>docker logs hermes | grep "setup token"</code>).</p>
  <label>Setup token</label>
  <input type="text" name="setup_token" required autocomplete="off">
"""

    body = _page("Setup", f"""
<h1>Hermes First-Time Setup</h1>
<p>Configure the minimum settings needed to bring the bot online. You can change everything later from the admin UI.</p>
<form method="POST" action="/admin/setup">
  {_csrf_input(csrf)}
  {token_field}
  <h2>Admin Account</h2>
  <label>Username</label>
  <input type="text" name="username" required autofocus>
  <label>Password <span class="note">(min 8 characters)</span></label>
  <input type="password" name="password" required minlength="8">
  <label>Confirm password</label>
  <input type="password" name="confirm" required minlength="8">

  <h2>Telegram</h2>
  <label>Telegram Bot Token <span class="note">(from @BotFather)</span></label>
  <input type="password" name="telegram_bot_token" value="{_esc(s.telegram_bot_token)}" required>
  <label>Admin Telegram User ID <span class="note">(DM @userinfobot to get yours)</span></label>
  <input type="text" name="admin_telegram_id" value="{_esc(admin_tg_val)}" inputmode="numeric" pattern="[0-9]+" required>

  <h2>Seerr</h2>
  <label>Seerr URL</label>
  <input type="text" name="seerr_url" value="{_esc(s.seerr_url)}" placeholder="http://192.168.1.10:5056" required>
  <label>Seerr API Key</label>
  <input type="password" name="seerr_api_key" value="{_esc(s.seerr_api_key)}" required>

  <button type="submit">Save &amp; Start Hermes</button>
  <div class="note">After saving, the container will restart to bring the bot online.</div>
</form>
""")
    resp = web.Response(text=body, content_type="text/html")
    attach_csrf_cookie(resp, csrf, secure=request_is_secure(request))
    return resp


async def setup_post(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    if store.settings.admin.is_set():
        return web.HTTPFound("/admin/login")
    form = await request.post()
    if not validate_csrf(request, form.get(CSRF_FORM_FIELD)):
        audit("setup_csrf_fail", ip=client_ip(request))
        return web.Response(text="CSRF token mismatch.", status=403)

    setup_token = load_or_create_setup_token(request.app["data_dir"])
    if setup_token:
        submitted = (form.get("setup_token") or "").strip()
        if not submitted or not hmac.compare_digest(submitted, setup_token):
            audit("setup_token_fail", ip=client_ip(request))
            body = _page("Setup", _flash(error="Invalid setup token.") +
                         '<p><a href="/admin/setup">Try again</a></p>')
            return web.Response(text=body, content_type="text/html", status=403)

    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    confirm = form.get("confirm") or ""
    bot_token = (form.get("telegram_bot_token") or "").strip()
    admin_tg_raw = (form.get("admin_telegram_id") or "").strip()
    seerr_url = (form.get("seerr_url") or "").strip()
    seerr_api_key = (form.get("seerr_api_key") or "").strip()

    errors: list[str] = []
    if not username:
        errors.append("Username required.")
    if len(password) < 8 or password != confirm:
        errors.append("Password must be at least 8 chars and match confirm.")
    if not bot_token:
        errors.append("Telegram bot token required.")
    try:
        admin_tg = int(admin_tg_raw)
        if admin_tg <= 0:
            raise ValueError
    except ValueError:
        errors.append("Admin Telegram User ID must be a positive integer.")
        admin_tg = 0
    if not seerr_url:
        errors.append("Seerr URL required.")
    if not seerr_api_key:
        errors.append("Seerr API Key required.")

    if errors:
        body = _page("Setup", _flash(error=" ".join(errors)) + '<p><a href="/admin/setup">Try again</a></p>')
        return web.Response(text=body, content_type="text/html", status=400)

    s = store.settings
    s.admin.username = username
    s.admin.password_hash = hash_password(password)
    s.telegram_bot_token = bot_token
    s.admin_telegram_id = admin_tg
    s.seerr_url = seerr_url
    s.seerr_api_key = seerr_api_key
    store.save()
    clear_setup_token(request.app["data_dir"])
    audit("setup_complete", user=username, ip=client_ip(request))
    logger.info("Setup complete; admin '%s' created, bot token + seerr configured", username)

    # Tell the surrounding app (setup-only mode) that we're done -- it will exit
    # so the container restarts into configured mode.
    reload_cb: Optional[ReloadCallback] = request.app.get("on_settings_changed")
    if reload_cb:
        try:
            await reload_cb()
        except Exception:
            logger.exception("on_settings_changed failed during setup")

    body = _page("Setup", """
<h1>Setup Complete</h1>
<p>Hermes is restarting to bring the bot online. Refresh in about 10 seconds and log in.</p>
""")
    return web.Response(text=body, content_type="text/html")


async def login_get(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    if not store.settings.admin.is_set():
        return web.HTTPFound("/admin/setup")
    if _current_user(request):
        return web.HTTPFound("/admin")
    csrf = csrf_for_request(request)
    body = _page("Login", f"""
<h1>Hermes Admin</h1>
<form method="POST" action="/admin/login">
  {_csrf_input(csrf)}
  <label>Username</label>
  <input type="text" name="username" required autofocus>
  <label>Password</label>
  <input type="password" name="password" required>
  <button type="submit">Log in</button>
</form>
""")
    resp = web.Response(text=body, content_type="text/html")
    attach_csrf_cookie(resp, csrf, secure=request_is_secure(request))
    return resp


async def login_post(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    form = await request.post()
    if not validate_csrf(request, form.get(CSRF_FORM_FIELD)):
        audit("login_csrf_fail", ip=client_ip(request))
        return web.Response(text="CSRF token mismatch.", status=403)

    ip = client_ip(request)
    locked = _throttle.is_locked(ip)
    if locked is not None:
        audit("login_throttled", ip=ip, seconds_left=int(locked))
        body = _page("Login", _flash(
            error=f"Too many failed attempts. Try again in {int(locked)}s."
        ) + '<p><a href="/admin/login">Back</a></p>')
        return web.Response(text=body, content_type="text/html", status=429,
                            headers={"Retry-After": str(int(locked))})

    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    admin = store.settings.admin
    if (not admin.is_set() or username != admin.username
            or not verify_password(password, admin.password_hash)):
        _throttle.record_failure(ip)
        audit("login_fail", user=username or "-", ip=ip)
        body = _page("Login", _flash(error="Invalid credentials.") +
                     '<p><a href="/admin/login">Try again</a></p>')
        return web.Response(text=body, content_type="text/html", status=401)

    _throttle.record_success(ip)
    audit("login_success", user=username, ip=ip)
    secure = request_is_secure(request)
    cookie = _make_session_cookie(request.app["session_secret"], username)
    resp = web.HTTPFound("/admin")
    _set_session_cookie(resp, cookie, secure=secure)
    # Rotate CSRF cookie after privilege change.
    attach_csrf_cookie(resp, generate_csrf_token(), secure=secure)
    return resp


async def logout(request: web.Request) -> web.Response:
    user = _current_user(request)
    audit("logout", user=user or "-", ip=client_ip(request))
    resp = web.HTTPFound("/admin/login")
    resp.del_cookie(SESSION_COOKIE)
    return resp


async def admin_get(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    active_tab = request.query.get("tab", "telegram")
    csrf = csrf_for_request(request)
    resp = web.Response(
        text=_settings_page(
            store.settings,
            active_tab=active_tab,
            webhook_url=_webhook_url_from_request(request),
            csrf_token=csrf,
        ),
        content_type="text/html",
    )
    attach_csrf_cookie(resp, csrf, secure=request_is_secure(request))
    return resp


async def _save_and_render(
    request: web.Request,
    *,
    active_tab: str,
    success_msg: str = "Saved.",
    error: str = "",
    skip_hot_reload: bool = False,
) -> web.Response:
    """Common epilogue: persist, trigger hot reload, render the current tab."""
    store: SettingsStore = request.app["settings_store"]
    store.save()
    msg = success_msg
    err = error
    if not skip_hot_reload:
        reload_cb: Optional[ReloadCallback] = request.app.get("on_settings_changed")
        if reload_cb:
            try:
                await reload_cb()
            except Exception as exc:
                logger.exception("Hot reload failed")
                err = f"Saved, but hot reload failed: {exc}. Restart the container."
    csrf = csrf_for_request(request)
    resp = web.Response(
        text=_settings_page(
            store.settings,
            message=msg, error=err,
            active_tab=active_tab,
            webhook_url=_webhook_url_from_request(request),
            csrf_token=csrf,
        ),
        content_type="text/html",
    )
    attach_csrf_cookie(resp, csrf, secure=request_is_secure(request))
    return resp


def _csrf_check_or_403(request: web.Request, form) -> Optional[web.Response]:
    """Reusable CSRF gate for admin POST handlers. Returns the rejection
    response (caller should `return` it) or None to proceed."""
    if not validate_csrf(request, form.get(CSRF_FORM_FIELD)):
        audit("admin_csrf_fail", user=_current_user(request) or "-",
              ip=client_ip(request), path=request.path)
        return web.Response(text="CSRF token mismatch.", status=403)
    return None


async def telegram_post(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    form = await request.post()
    csrf_resp = _csrf_check_or_403(request, form)
    if csrf_resp is not None:
        return csrf_resp
    s = store.settings
    _orig_token = s.telegram_bot_token
    _orig_admin = s.admin_telegram_id
    token = (form.get("telegram_bot_token") or "").strip()
    admin_tg_raw = (form.get("admin_telegram_id") or "").strip()
    if not token:
        return web.Response(
            text=_settings_page(s, error="Bot token is required.",
                                active_tab="telegram",
                                webhook_url=_webhook_url_from_request(request),
                                csrf_token=csrf_for_request(request)),
            content_type="text/html", status=400,
        )
    try:
        admin_tg = int(admin_tg_raw)
        if admin_tg <= 0:
            raise ValueError
    except ValueError:
        return web.Response(
            text=_settings_page(s, error="Admin Telegram User ID must be a positive integer.",
                                active_tab="telegram",
                                webhook_url=_webhook_url_from_request(request),
                                csrf_token=csrf_for_request(request)),
            content_type="text/html", status=400,
        )
    public_url = (form.get("hermes_public_url") or "").strip()
    url_err = validate_public_url(public_url)
    if url_err:
        return web.Response(
            text=_settings_page(s, error=f"Hermes Public URL: {url_err}",
                                active_tab="telegram",
                                webhook_url=_webhook_url_from_request(request),
                                csrf_token=csrf_for_request(request)),
            content_type="text/html", status=400,
        )
    s.telegram_bot_token = token
    s.admin_telegram_id = admin_tg
    s.hermes_public_url = public_url
    restart_needed = (token != _orig_token) or (admin_tg != _orig_admin)
    return await _save_and_render(
        request, active_tab="telegram",
        success_msg=("Saved. Container restarting in ~2s to apply the new Telegram identity."
                     if restart_needed else "Saved."),
    )


async def seerr_post(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    form = await request.post()
    csrf_resp = _csrf_check_or_403(request, form)
    if csrf_resp is not None:
        return csrf_resp
    s = store.settings
    s.seerr_url = (form.get("seerr_url") or "").strip()
    s.seerr_api_key = (form.get("seerr_api_key") or "").strip()
    s.seerr_public_url = (form.get("seerr_public_url") or "").strip()
    return await _save_and_render(request, active_tab="seerr")


async def autofix_post(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    form = await request.post()
    csrf_resp = _csrf_check_or_403(request, form)
    if csrf_resp is not None:
        return csrf_resp
    s = store.settings
    s.radarr_url = (form.get("radarr_url") or "").strip()
    s.radarr_api_key = (form.get("radarr_api_key") or "").strip()
    s.sonarr_url = (form.get("sonarr_url") or "").strip()
    s.sonarr_api_key = (form.get("sonarr_api_key") or "").strip()
    ids_raw = form.get("allowed_autofix_telegram_ids") or ""
    parsed_ids: list[int] = []
    for chunk in ids_raw.split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            parsed_ids.append(int(chunk))
    s.allowed_autofix_telegram_ids = parsed_ids
    limit_raw = (form.get("daily_autofix_limit") or "").strip()
    try:
        limit = int(limit_raw)
        if limit < 1:
            raise ValueError
    except ValueError:
        return web.Response(
            text=_settings_page(s, error="Per-user daily limit must be a positive integer.",
                                active_tab="autofix",
                                webhook_url=_webhook_url_from_request(request),
                                csrf_token=csrf_for_request(request)),
            content_type="text/html", status=400,
        )
    s.daily_autofix_limit = limit
    return await _save_and_render(request, active_tab="autofix")


async def webhook_post(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    form = await request.post()
    csrf_resp = _csrf_check_or_403(request, form)
    if csrf_resp is not None:
        return csrf_resp
    s = store.settings
    secret = (form.get("webhook_secret") or "").strip()
    if not secret:
        return web.Response(
            text=_settings_page(s, error="Webhook secret cannot be empty.",
                                active_tab="webhook",
                                webhook_url=_webhook_url_from_request(request),
                                csrf_token=csrf_for_request(request)),
            content_type="text/html", status=400,
        )
    s.webhook_secret = secret
    return await _save_and_render(request, active_tab="webhook")


async def change_password(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    form = await request.post()
    csrf_resp = _csrf_check_or_403(request, form)
    if csrf_resp is not None:
        return csrf_resp
    current = form.get("current") or ""
    new = form.get("new") or ""
    confirm = form.get("confirm") or ""
    admin = store.settings.admin
    if not verify_password(current, admin.password_hash):
        return web.Response(
            text=_settings_page(store.settings, error="Current password is incorrect.",
                                active_tab="account",
                                webhook_url=_webhook_url_from_request(request),
                                csrf_token=csrf_for_request(request)),
            content_type="text/html", status=400,
        )
    if len(new) < 8 or new != confirm:
        return web.Response(
            text=_settings_page(store.settings, error="New password must be >= 8 chars and match confirm.",
                                active_tab="account",
                                webhook_url=_webhook_url_from_request(request),
                                csrf_token=csrf_for_request(request)),
            content_type="text/html", status=400,
        )
    admin.password_hash = hash_password(new)
    store.save()
    audit("password_changed", user=_current_user(request) or "-", ip=client_ip(request))
    return web.Response(
        text=_settings_page(store.settings, message="Password changed.",
                            active_tab="account",
                            webhook_url=_webhook_url_from_request(request),
                            csrf_token=csrf_for_request(request)),
        content_type="text/html",
    )


async def backup_download(request: web.Request) -> web.Response:
    data_dir: Path = request.app["data_dir"]
    settings_path: Path = request.app["settings_path"]
    db_path: Path = Path(request.app["db_path"])
    enc_key_path = data_dir / "encryption.key"

    form = await request.post()
    csrf_resp = _csrf_check_or_403(request, form)
    if csrf_resp is not None:
        return csrf_resp
    passphrase = form.get("passphrase") or ""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if settings_path.exists():
            zf.write(settings_path, "settings.json")
        if db_path.exists():
            zf.write(db_path, "mappings.sqlite")
        if enc_key_path.exists():
            zf.write(enc_key_path, "encryption.key")
    raw_zip = buf.getvalue()

    if passphrase:
        blob = wrap(raw_zip, passphrase)
        ext = "hermes-backup"
        ctype = "application/octet-stream"
    else:
        blob = raw_zip
        ext = "zip"
        ctype = "application/zip"

    ts = time.strftime("%Y%m%d-%H%M%S")
    audit("backup_download", user=_current_user(request) or "-",
          ip=client_ip(request), wrapped=bool(passphrase))
    return web.Response(
        body=blob,
        headers={
            "Content-Type": ctype,
            "Content-Disposition": f'attachment; filename="hermes-backup-{ts}.{ext}"',
        },
    )


def _restore_error(request: web.Request, message: str, status: int = 400) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    return web.Response(
        text=_settings_page(store.settings, error=message,
                            active_tab="account",
                            marker_target="restore",
                            webhook_url=_webhook_url_from_request(request),
                            csrf_token=csrf_for_request(request)),
        content_type="text/html", status=status,
    )


async def restore_upload(request: web.Request) -> web.Response:
    data_dir: Path = request.app["data_dir"]
    settings_path: Path = request.app["settings_path"]
    db_path: Path = Path(request.app["db_path"])
    enc_key_path = data_dir / "encryption.key"

    # Multipart parsing: pull CSRF token, optional passphrase, and the file.
    reader = await request.multipart()
    csrf_form_value: Optional[str] = None
    passphrase = ""
    file_bytes: Optional[bytes] = None
    while True:
        field = await reader.next()
        if field is None:
            break
        if field.name == CSRF_FORM_FIELD:
            csrf_form_value = (await field.text()).strip()
        elif field.name == "passphrase":
            passphrase = await field.text()
        elif field.name == "backup":
            file_bytes = await field.read()

    if not validate_csrf(request, csrf_form_value):
        audit("admin_csrf_fail", user=_current_user(request) or "-",
              ip=client_ip(request), path=request.path)
        return web.Response(text="CSRF token mismatch.", status=403)

    if file_bytes is None:
        return _restore_error(request, "No backup file in upload.")

    # Unwrap passphrase-protected backup if needed.
    if is_wrapped(file_bytes):
        if not passphrase:
            return _restore_error(request, "This backup is passphrase-protected. Provide the passphrase.")
        try:
            file_bytes = unwrap(file_bytes, passphrase)
        except ValueError as exc:
            return _restore_error(request, f"Couldn't decrypt backup: {exc}")

    # Validate ZIP structure + member integrity before touching disk.
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            names = set(zf.namelist())
            if "settings.json" not in names and "mappings.sqlite" not in names:
                raise ValueError("Backup must contain settings.json and/or mappings.sqlite")
            if "settings.json" in names:
                # parse-check: must be valid JSON the Settings dataclass accepts.
                data = json.loads(zf.read("settings.json").decode())
                Settings.from_dict(data)
            if "encryption.key" in names:
                key_bytes = zf.read("encryption.key").strip()
                # Fernet() raises on invalid key shape (length, base64, etc).
                from cryptography.fernet import Fernet
                Fernet(key_bytes)
            if "mappings.sqlite" in names:
                sqlite_bytes = zf.read("mappings.sqlite")
                with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
                    tmp.write(sqlite_bytes)
                    tmp_path = tmp.name
                try:
                    with sqlite3.connect(tmp_path) as c:
                        row = c.execute("PRAGMA integrity_check").fetchone()
                    if not row or row[0] != "ok":
                        raise ValueError(
                            f"SQLite integrity check failed: {row[0] if row else 'unknown'}"
                        )
                finally:
                    try:
                        Path(tmp_path).unlink()
                    except OSError:
                        pass
    except Exception as exc:
        return _restore_error(request, f"Invalid backup: {exc}")

    # Snapshot current files before overwriting so a failed restore is recoverable.
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = data_dir / f"pre-restore-{ts}"
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        for src in (settings_path, db_path, enc_key_path):
            if Path(src).exists():
                shutil.copy2(src, backup_dir / Path(src).name)
    except Exception:
        logger.exception("pre-restore snapshot failed; proceeding anyway")

    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            if "settings.json" in names:
                settings_path.write_bytes(zf.read("settings.json"))
            if "mappings.sqlite" in names:
                db_path.write_bytes(zf.read("mappings.sqlite"))
            if "encryption.key" in names:
                enc_key_path.write_bytes(zf.read("encryption.key"))
    except Exception as exc:
        return _restore_error(request, f"Restore failed: {exc}", status=500)

    audit("restore_complete", user=_current_user(request) or "-",
          ip=client_ip(request), backup_dir=str(backup_dir))
    logger.info("Restore complete; restarting in 2s (snapshot at %s)", backup_dir)
    _schedule_clean_exit(2.0)

    body = _page("Restore", f"""
<h1>Restore Complete</h1>
<p>Container is restarting. Refresh in a few seconds.</p>
<p class="note">Previous files snapshot to <code>{_esc(backup_dir)}</code>.</p>
""")
    return web.Response(text=body, content_type="text/html")


# --- Auth middleware --------------------------------------------------------

PUBLIC_ADMIN_PATHS = {"/admin/setup", "/admin/login"}


@web.middleware
async def auth_middleware(request: web.Request, handler) -> web.Response:
    path = request.path
    if not path.startswith("/admin"):
        return await handler(request)
    store: SettingsStore = request.app["settings_store"]
    # First-run: force /admin/setup until admin exists
    if not store.settings.admin.is_set() and path != "/admin/setup":
        return web.HTTPFound("/admin/setup")
    if path in PUBLIC_ADMIN_PATHS:
        return await handler(request)
    if _current_user(request):
        return await handler(request)
    return web.HTTPFound("/admin/login")


# --- Attach -----------------------------------------------------------------

def attach_webui(
    app: web.Application,
    *,
    settings_store: SettingsStore,
    session_secret: bytes,
    data_dir: Path,
    settings_path: Path,
    db_path: Path,
    on_settings_changed: Optional[ReloadCallback] = None,
) -> None:
    app["settings_store"] = settings_store
    app["session_secret"] = session_secret
    app["data_dir"] = data_dir
    app["settings_path"] = settings_path
    app["db_path"] = db_path
    app["on_settings_changed"] = on_settings_changed
    app.middlewares.append(auth_middleware)
    app.router.add_get("/admin/setup", setup_get)
    app.router.add_post("/admin/setup", setup_post)
    app.router.add_get("/admin/login", login_get)
    app.router.add_post("/admin/login", login_post)
    app.router.add_get("/admin/logout", logout)
    app.router.add_get("/admin", admin_get)
    app.router.add_post("/admin/telegram", telegram_post)
    app.router.add_post("/admin/seerr", seerr_post)
    app.router.add_post("/admin/autofix", autofix_post)
    app.router.add_post("/admin/webhook", webhook_post)
    app.router.add_post("/admin/password", change_password)
    app.router.add_post("/admin/backup", backup_download)
    app.router.add_post("/admin/restore", restore_upload)
