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
import time
import zipfile
from hashlib import sha256
from pathlib import Path
from typing import Awaitable, Callable, Optional

from aiohttp import web

from settings import (
    Settings,
    SettingsStore,
    hash_password,
    verify_password,
)

logger = logging.getLogger("hermes.webui")

SESSION_COOKIE = "hermes_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600

ReloadCallback = Callable[[], Awaitable[None]]


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
nav { margin-bottom: 20px; padding: 12px; background: #313244; border-radius: 8px; }
nav a { color: #89b4fa; text-decoration: none; margin-right: 18px; font-weight: 600; }
nav a:hover { text-decoration: underline; }
a { color: #89b4fa; }
code { background: #45475a; padding: 2px 6px; border-radius: 3px; font-size: 13px; }

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
) -> str:
    if active_tab not in TAB_KEYS:
        active_tab = "telegram"
    ids_str = ",".join(str(i) for i in s.allowed_autofix_telegram_ids)
    admin_tg_val = str(s.admin_telegram_id) if s.admin_telegram_id else ""

    def chk(key: str) -> str:
        return ' checked' if active_tab == key else ''

    telegram_form = f"""
<form method="POST" action="/admin/telegram">
  <h2>Telegram</h2>
  <div class="note">Changes to these fields restart the container so the new identity takes effect.</div>
  <label>Bot Token <span class="note">(from @BotFather)</span></label>
  <input type="password" name="telegram_bot_token" value="{_esc(s.telegram_bot_token)}" required>
  <label>Admin Telegram User ID <span class="note">(DM @userinfobot)</span></label>
  <input type="text" name="admin_telegram_id" value="{_esc(admin_tg_val)}" inputmode="numeric" pattern="[0-9]+" required>
  <button type="submit">Save &amp; Restart</button>
</form>
"""

    seerr_form = f"""
<form method="POST" action="/admin/seerr">
  <h2>Seerr</h2>
  <label>Seerr URL</label>
  <input type="text" name="seerr_url" value="{_esc(s.seerr_url)}" placeholder="http://192.168.1.10:5056" required>
  <label>Seerr API Key</label>
  <input type="password" name="seerr_api_key" value="{_esc(s.seerr_api_key)}" required>
  <label>Seerr Public URL <span class="note">(optional, for reverse-proxy links sent to users)</span></label>
  <input type="text" name="seerr_public_url" value="{_esc(s.seerr_public_url)}" placeholder="https://seerr.example.com">
  <button type="submit">Save</button>
</form>
"""

    autofix_form = f"""
<form method="POST" action="/admin/autofix">
  <h2>Auto-fix (Radarr / Sonarr)</h2>
  <div class="note">Optional. Both Radarr and Sonarr are independent -- configure whichever you use.</div>

  <label>Radarr URL</label>
  <input type="text" name="radarr_url" value="{_esc(s.radarr_url)}" placeholder="http://192.168.1.10:7878">
  <label>Radarr API Key</label>
  <input type="password" name="radarr_api_key" value="{_esc(s.radarr_api_key)}">

  <label>Sonarr URL</label>
  <input type="text" name="sonarr_url" value="{_esc(s.sonarr_url)}" placeholder="http://192.168.1.10:8989">
  <label>Sonarr API Key</label>
  <input type="password" name="sonarr_api_key" value="{_esc(s.sonarr_api_key)}">

  <label>Allowed Telegram User IDs</label>
  <input type="text" name="allowed_autofix_telegram_ids" value="{_esc(ids_str)}" placeholder="123456,789012">
  <div class="note">Comma-separated. Leave empty for admin-only.</div>

  <button type="submit">Save</button>
</form>
"""

    webhook_form = f"""
<form method="POST" action="/admin/webhook">
  <h2>Webhook</h2>
  <p>Hermes receives webhook events from Seerr on this URL:</p>
  <div class="url-box">{_esc(webhook_url)}</div>
  <div class="note">Configure in Seerr: Settings → Notifications → Webhook. Set the URL above and enable the <strong>Issue Comment</strong> event.</div>

  <label>Webhook Secret <span class="note">(optional)</span></label>
  <input type="password" name="webhook_secret" value="{_esc(s.webhook_secret)}">
  <div class="note">If set, paste the same value into Seerr's Webhook <code>Authorization Header</code> field. Hermes rejects requests without a matching header.</div>

  <button type="submit">Save</button>
</form>
"""

    account_section = """
<form method="POST" action="/admin/password">
  <h2>Change Password</h2>
  <label>Current password</label>
  <input type="password" name="current" required>
  <label>New password</label>
  <input type="password" name="new" required minlength="8">
  <label>Confirm new password</label>
  <input type="password" name="confirm" required minlength="8">
  <button type="submit">Change Password</button>
</form>

<form method="GET" action="/admin/backup">
  <h2>Download Backup</h2>
  <div class="note">Downloads a ZIP containing settings.json, the mappings database, and the encryption key. Treat this file as secret.</div>
  <button type="submit">Download Backup</button>
</form>

<form method="POST" action="/admin/restore" enctype="multipart/form-data">
  <h2>Restore from Backup</h2>
  <input type="file" name="backup" accept=".zip" required>
  <div class="note">Overwrites settings, mappings DB, and encryption key. The container will restart.</div>
  <button type="submit" class="danger">Restore</button>
</form>
"""

    return _page("Admin", f"""
<nav>
  <a href="/admin">Settings</a>
  <a href="/admin/logout">Log out</a>
</nav>
<h1>Hermes Settings</h1>
{_flash(message, error)}
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
    s = store.settings
    admin_tg_val = str(s.admin_telegram_id) if s.admin_telegram_id else ""
    body = _page("Setup", f"""
<h1>Hermes First-Time Setup</h1>
<p>Configure the minimum settings needed to bring the bot online. You can change everything later from the admin UI.</p>
<form method="POST" action="/admin/setup">
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
    return web.Response(text=body, content_type="text/html")


async def setup_post(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    if store.settings.admin.is_set():
        return web.HTTPFound("/admin/login")
    form = await request.post()
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
    body = _page("Login", """
<h1>Hermes Admin</h1>
<form method="POST" action="/admin/login">
  <label>Username</label>
  <input type="text" name="username" required autofocus>
  <label>Password</label>
  <input type="password" name="password" required>
  <button type="submit">Log in</button>
</form>
""")
    return web.Response(text=body, content_type="text/html")


async def login_post(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    form = await request.post()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    admin = store.settings.admin
    if not admin.is_set() or username != admin.username or not verify_password(password, admin.password_hash):
        body = _page("Login", _flash(error="Invalid credentials.") +
                     '<p><a href="/admin/login">Try again</a></p>')
        return web.Response(text=body, content_type="text/html", status=401)
    cookie = _make_session_cookie(request.app["session_secret"], username)
    resp = web.HTTPFound("/admin")
    resp.set_cookie(SESSION_COOKIE, cookie, max_age=SESSION_TTL_SECONDS, httponly=True, samesite="Lax")
    return resp


async def logout(request: web.Request) -> web.Response:
    resp = web.HTTPFound("/admin/login")
    resp.del_cookie(SESSION_COOKIE)
    return resp


async def admin_get(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    active_tab = request.query.get("tab", "telegram")
    return web.Response(
        text=_settings_page(
            store.settings,
            active_tab=active_tab,
            webhook_url=_webhook_url_from_request(request),
        ),
        content_type="text/html",
    )


async def _save_and_render(
    request: web.Request,
    *,
    active_tab: str,
    success_msg: str = "Settings saved.",
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
    return web.Response(
        text=_settings_page(
            store.settings,
            message=msg, error=err,
            active_tab=active_tab,
            webhook_url=_webhook_url_from_request(request),
        ),
        content_type="text/html",
    )


async def telegram_post(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    form = await request.post()
    s = store.settings
    token = (form.get("telegram_bot_token") or "").strip()
    admin_tg_raw = (form.get("admin_telegram_id") or "").strip()
    if not token:
        return web.Response(
            text=_settings_page(s, error="Bot token is required.",
                                active_tab="telegram",
                                webhook_url=_webhook_url_from_request(request)),
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
                                webhook_url=_webhook_url_from_request(request)),
            content_type="text/html", status=400,
        )
    s.telegram_bot_token = token
    s.admin_telegram_id = admin_tg
    return await _save_and_render(
        request, active_tab="telegram",
        success_msg="Saved. Container restarting in ~2s to apply the new Telegram identity.",
    )


async def seerr_post(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    form = await request.post()
    s = store.settings
    s.seerr_url = (form.get("seerr_url") or "").strip()
    s.seerr_api_key = (form.get("seerr_api_key") or "").strip()
    s.seerr_public_url = (form.get("seerr_public_url") or "").strip()
    return await _save_and_render(request, active_tab="seerr")


async def autofix_post(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    form = await request.post()
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
    return await _save_and_render(request, active_tab="autofix")


async def webhook_post(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    form = await request.post()
    s = store.settings
    s.webhook_secret = (form.get("webhook_secret") or "").strip()
    return await _save_and_render(request, active_tab="webhook")


async def change_password(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    form = await request.post()
    current = form.get("current") or ""
    new = form.get("new") or ""
    confirm = form.get("confirm") or ""
    admin = store.settings.admin
    if not verify_password(current, admin.password_hash):
        return web.Response(
            text=_settings_page(store.settings, error="Current password is incorrect.",
                                active_tab="account",
                                webhook_url=_webhook_url_from_request(request)),
            content_type="text/html", status=400,
        )
    if len(new) < 8 or new != confirm:
        return web.Response(
            text=_settings_page(store.settings, error="New password must be >= 8 chars and match confirm.",
                                active_tab="account",
                                webhook_url=_webhook_url_from_request(request)),
            content_type="text/html", status=400,
        )
    admin.password_hash = hash_password(new)
    store.save()
    return web.Response(
        text=_settings_page(store.settings, message="Password changed.",
                            active_tab="account",
                            webhook_url=_webhook_url_from_request(request)),
        content_type="text/html",
    )


async def backup_download(request: web.Request) -> web.Response:
    data_dir: Path = request.app["data_dir"]
    settings_path: Path = request.app["settings_path"]
    db_path: Path = Path(request.app["db_path"])
    enc_key_path = data_dir / "encryption.key"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if settings_path.exists():
            zf.write(settings_path, "settings.json")
        if db_path.exists():
            zf.write(db_path, "mappings.sqlite")
        if enc_key_path.exists():
            zf.write(enc_key_path, "encryption.key")

    ts = time.strftime("%Y%m%d-%H%M%S")
    return web.Response(
        body=buf.getvalue(),
        headers={
            "Content-Type": "application/zip",
            "Content-Disposition": f'attachment; filename="hermes-backup-{ts}.zip"',
        },
    )


async def restore_upload(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    data_dir: Path = request.app["data_dir"]
    settings_path: Path = request.app["settings_path"]
    db_path: Path = Path(request.app["db_path"])
    enc_key_path = data_dir / "encryption.key"

    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != "backup":
        return web.Response(text=_settings_page(store.settings, error="No backup file in upload.",
                                                        active_tab="account",
                                                        webhook_url=_webhook_url_from_request(request)),
                            content_type="text/html", status=400)
    data = await field.read()

    # Validate the ZIP before touching anything on disk
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            if "settings.json" not in names and "mappings.sqlite" not in names:
                raise ValueError("Backup must contain settings.json and/or mappings.sqlite")
            if "settings.json" in names:
                json.loads(zf.read("settings.json").decode())  # parse-check
    except Exception as exc:
        return web.Response(
            text=_settings_page(store.settings, error=f"Invalid backup: {exc}",
                                active_tab="account",
                                webhook_url=_webhook_url_from_request(request)),
            content_type="text/html", status=400,
        )

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if "settings.json" in names:
                settings_path.write_bytes(zf.read("settings.json"))
            if "mappings.sqlite" in names:
                db_path.write_bytes(zf.read("mappings.sqlite"))
            if "encryption.key" in names:
                enc_key_path.write_bytes(zf.read("encryption.key"))
    except Exception as exc:
        return web.Response(
            text=_settings_page(store.settings, error=f"Restore failed: {exc}",
                                active_tab="account",
                                webhook_url=_webhook_url_from_request(request)),
            content_type="text/html", status=500,
        )

    logger.info("Restore complete; exiting in 2s so the container restarts and picks up new state")
    loop = asyncio.get_event_loop()
    loop.call_later(2.0, lambda: os._exit(0))

    body = _page("Restore", """
<h1>Restore Complete</h1>
<p>Container is restarting. Refresh in a few seconds.</p>
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
    app.router.add_get("/admin/backup", backup_download)
    app.router.add_post("/admin/restore", restore_upload)
