"""Slim aiohttp admin webui.

Routes (auth middleware covers everything except setup/login):
  GET/POST /admin/setup      -- first-run admin account creation
  GET/POST /admin/login      -- session login
  POST     /admin/logout
  GET      /admin            -- settings page
  POST     /admin/telegram | /admin/seerr | /admin/autofix | /admin/webhook
                             -- save one settings tab each
  POST     /admin/test/{telegram|seerr|autofix|webhook}
                             -- connection tests (JSON)
  GET      /admin/seerr/newplex-warning          -- banner state (JSON)
  POST     /admin/seerr/newplex-warning/dismiss
  POST     /admin/password   -- change admin password
  POST     /admin/backup     -- download backup ZIP
  POST     /admin/restore    -- upload backup ZIP and exit (container restarts)

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
import sqlite3
import tempfile
import time
import uuid
import zipfile
from hashlib import sha256
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx
from aiohttp import web

from auth_util import (
    CSRF_FORM_FIELD,
    THROTTLE_IP_MAX_FAILURES,
    _SETUP_TOKEN_UNREADABLE,
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
from const import (ADMIN_PASSWORD_MIN_CHARS, ADMIN_UPLOAD_MAX_BYTES,
                   MIN_BACKUP_PASSPHRASE_CHARS, RESTORE_MEMBER_MAX_BYTES)
from fsutil import atomic_write_bytes
from procutil import schedule_clean_exit
from backup_crypto import is_wrapped, unwrap, wrap
from settings import (
    DEFAULT_DAILY_AUTOFIX_LIMIT,
    PBKDF2_ITERATIONS,
    Settings,
    SettingsStore,
    hash_password,
    iterations_of,
    validate_public_url,
    verify_password,
)
from http_util import user_friendly_message
from radarr import RadarrClient
from seerr import SeerrClient
from sonarr import SonarrClient
from _version import __version__ as USHER_VERSION

logger = logging.getLogger("usher.webui")

SESSION_COOKIE = "usher_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600

ReloadCallback = Callable[[], Awaitable[None]]

# Single in-memory throttle shared by all login_post invocations in this process.
_throttle = LoginThrottle()
# Coarser per-IP throttle checked before any PBKDF2, so a username-rotating
# flood can't drive unbounded password hashing on the event loop.
_ip_throttle = LoginThrottle(max_failures=THROTTLE_IP_MAX_FAILURES)

# Verified against instead of the real hash when the username doesn't match,
# so wrong-username and wrong-password attempts cost the same PBKDF2 work
# (no timing oracle on the admin username). Computed once at import.
_TIMING_EQUALIZER_HASH = hash_password("timing-equalizer-not-a-real-password")


def _set_session_cookie(resp, cookie_value: str, *, secure: bool) -> None:
    resp.set_cookie(
        SESSION_COOKIE, cookie_value,
        max_age=SESSION_TTL_SECONDS,
        httponly=True, samesite="Lax", secure=secure,
    )


# _schedule_clean_exit moved to procutil.schedule_clean_exit (it was a
# verbatim copy between here and bot/shared.py).


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


def _make_session_cookie(secret: bytes, username: str, pwd_ver: int = 0) -> str:
    payload = json.dumps({"u": username, "v": pwd_ver,
                          "exp": int(time.time()) + SESSION_TTL_SECONDS}).encode()
    body = _b64(payload)
    sig = _b64(_sign(secret, body.encode()))
    return f"{body}.{sig}"


def _verify_session_cookie(secret: bytes, cookie: str,
                           expected_pwd_ver: int = 0) -> Optional[str]:
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
    # Sessions minted before a password change carry an older version and
    # die here. Cookies from pre-0.12.0 have no "v" and
    # default to 0, matching installs that never changed the password.
    if payload.get("v", 0) != expected_pwd_ver:
        return None
    return payload.get("u")


# Sessions are stateless HMAC cookies, so logout needs a server-side
# denylist or the cookie stays valid for its full TTL after "logging out".
# Keyed by cookie signature -> expiry epoch; entries drop once the cookie
# would have expired anyway. In-memory: a restart forgets revocations, but
# also invalidates nothing else -- acceptable for a single-admin app, and
# strictly better than no revocation at all.
_revoked_sessions: dict[str, float] = {}


def _revoke_session(cookie: str) -> None:
    try:
        body, sig = cookie.split(".")
        exp = json.loads(_b64d(body)).get("exp", 0)
    except Exception:
        return
    now = time.time()
    for k, e in list(_revoked_sessions.items()):
        if e < now:
            _revoked_sessions.pop(k, None)
    _revoked_sessions[sig] = exp


def _session_revoked(cookie: str) -> bool:
    try:
        sig = cookie.split(".")[1]
    except IndexError:
        return False
    exp = _revoked_sessions.get(sig)
    return exp is not None and exp >= time.time()


def _current_user(request: web.Request) -> Optional[str]:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    if _session_revoked(cookie):
        return None
    store: SettingsStore = request.app["settings_store"]
    return _verify_session_cookie(request.app["session_secret"], cookie,
                                  store.settings.admin.password_version)


# --- HTML rendering ---------------------------------------------------------

def _esc(s) -> str:
    if not s:
        return ""
    return (str(s).replace("&", "&amp;")
                  .replace("<", "&lt;")
                  .replace(">", "&gt;")
                  .replace('"', "&quot;"))


CSS = """
/* Dracula Pro variants (facelessuser/sublime-dracula-scheme):
   Van Helsing (near-black, blue accent) is the default; Alucard (light,
   purple accent) is applied via data-theme on <html>, persisted by JS. */
:root {
  --bg: #0b0d0f;
  --fg: #f8f8f2;
  --surface: #16191d;
  --border: #304050;
  --muted: #708ca9;
  --label: #9580ff;
  --accent: #80bfff;
  --accent-hover: #80ffea;
  --on-accent: #0b0d0f;
  --btn-2: #2b333b;
  --btn-2-hover: #414c58;
  --btn-2-fg: #f8f8f2;
  --input-bg: #0b0d0f;
  --input-locked-bg: #08090b;
  --code-bg: #23292f;
  --green: #8aff80;
  --red: #ff9580;
  --yellow: #ffff80;
  --on-status: #0b0d0f;
  --warn-bg: #26261b;
  --link: #80bfff;
}
:root[data-theme="alucard"] {
  --bg: #f5f5f5;
  --fg: #1f1f1f;
  --surface: #ffffff;
  --border: #cfcfde;
  --muted: #635d97;
  --label: #644ac9;
  --accent: #644ac9;
  --accent-hover: #4f3a9f;
  --on-accent: #ffffff;
  --btn-2: #dcdeef;
  --btn-2-hover: #cfcfde;
  --btn-2-fg: #1f1f1f;
  --input-bg: #ffffff;
  --input-locked-bg: #e9e9ef;
  --code-bg: #e9e9ef;
  --green: #14720a;
  --red: #cb3a2a;
  --yellow: #836e15;
  --on-status: #ffffff;
  --warn-bg: #f4eed3;
  --link: #3654c2;
}
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: var(--bg); color: var(--fg); margin: 0; padding: 20px; line-height: 1.5; }
.container { max-width: 760px; margin: 0 auto; }
h1, h2 { color: var(--fg); }
h2 { margin-top: 0; border-bottom: 1px solid var(--border); padding-bottom: 6px; }
form { background: var(--surface); padding: 20px; border-radius: 8px; margin-bottom: 20px; }
label { display: block; margin: 12px 0 4px; font-weight: 600; color: var(--label); }
input[type="text"], input[type="password"], input[type="file"], textarea, select {
  width: 100%; box-sizing: border-box; padding: 8px;
  border: 1px solid var(--border); border-radius: 4px;
  background: var(--input-bg); color: var(--fg); font-size: 14px; }
button {
  background: var(--accent); color: var(--on-accent); border: none;
  padding: 10px 20px; border-radius: 4px; cursor: pointer;
  font-weight: 600; margin-top: 16px; font-size: 14px; }
button:hover { background: var(--accent-hover); }
button.danger { background: var(--red); color: var(--on-status); }
button.danger:hover { background: var(--red); opacity: 0.85; }
.error { background: var(--red); color: var(--on-status); padding: 10px 14px; border-radius: 4px; margin-bottom: 14px; }
.success { background: var(--green); color: var(--on-status); padding: 10px 14px; border-radius: 4px; margin-bottom: 14px; }
.note { color: var(--muted); font-size: 13px; margin-top: 4px; }
.banner-warn { background: var(--warn-bg); border: 1px solid var(--yellow); color: var(--yellow);
               padding: 12px 14px; border-radius: 6px; margin-bottom: 14px;
               font-size: 14px; line-height: 1.5; }
.banner-warn button { background: var(--yellow); color: var(--on-status); border: none;
                      padding: 4px 12px; border-radius: 4px; font-weight: 600;
                      cursor: pointer; margin-left: 8px; }
a { color: var(--link); }
code { background: var(--code-bg); padding: 2px 6px; border-radius: 3px; font-size: 13px; }

.topbar { display: flex; justify-content: flex-end; align-items: center;
          gap: 14px; margin-bottom: 14px; }
.topbar .version { color: var(--muted); font-size: 13px;
                   font-family: ui-monospace, Menlo, monospace; }
.topbar .logout-form { background: none; padding: 0; margin: 0; display: inline; }
.topbar .logout {
  background: var(--btn-2); color: var(--btn-2-fg); text-decoration: none; border: none;
  cursor: pointer; font-family: inherit; width: auto; margin-top: 0;
  padding: 6px 14px; border-radius: 4px; font-size: 13px; font-weight: 600;
}
.topbar .logout:hover { background: var(--btn-2-hover); }
.topbar .theme-toggle {
  background: var(--btn-2); color: var(--btn-2-fg); border: none; cursor: pointer;
  width: auto; min-width: 34px; padding: 6px 10px; border-radius: 4px;
  font-size: 13px; margin-top: 0; line-height: 1.5;
}
.topbar .theme-toggle:hover { background: var(--btn-2-hover); }
.intro { color: var(--muted); margin: 0 0 14px 0; font-size: 14px; }
.saved-marker {
  display: inline-block; margin-left: 12px; font-weight: 600; font-size: 13px;
  vertical-align: middle;
}
.saved-marker.ok {
  color: var(--green);
  animation: fade-out 1s ease-in-out 3s forwards;
}
.saved-marker.err { color: var(--red); }
@keyframes fade-out {
  to { opacity: 0; visibility: hidden; }
}

/* Tabs (CSS-only via radio inputs) */
.tabs > input[type="radio"] { position: absolute; left: -9999px; }
.tab-labels { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 0;
              border-bottom: 2px solid var(--border); }
.tab-labels label {
  display: inline-block; padding: 10px 18px; cursor: pointer;
  background: var(--surface); color: var(--muted); font-weight: 600;
  border-radius: 6px 6px 0 0; margin: 0; user-select: none;
  border: 1px solid transparent; border-bottom: none;
}
.tab-labels label:hover { color: var(--fg); }
#tab-telegram:checked ~ .tab-labels label[for="tab-telegram"],
#tab-seerr:checked    ~ .tab-labels label[for="tab-seerr"],
#tab-autofix:checked  ~ .tab-labels label[for="tab-autofix"],
#tab-sabnzbd:checked  ~ .tab-labels label[for="tab-sabnzbd"],
#tab-webhook:checked  ~ .tab-labels label[for="tab-webhook"],
#tab-account:checked  ~ .tab-labels label[for="tab-account"] {
  background: var(--surface); color: var(--fg); border-color: var(--border);
  border-bottom: 2px solid var(--surface); margin-bottom: -2px;
}
.tab-content { display: none; }
#tab-telegram:checked ~ .tab-contents .tab-c-telegram,
#tab-seerr:checked    ~ .tab-contents .tab-c-seerr,
#tab-autofix:checked  ~ .tab-contents .tab-c-autofix,
#tab-sabnzbd:checked  ~ .tab-contents .tab-c-sabnzbd,
#tab-webhook:checked  ~ .tab-contents .tab-c-webhook,
#tab-account:checked  ~ .tab-contents .tab-c-account { display: block; }
/* The first card in a pane attaches to the tab strip: squared top corners
   so the active tab meets the panel edge instead of a rounded notch. */
.tab-content > form:first-of-type {
  border-top-left-radius: 0; border-top-right-radius: 0;
}
.url-box {
  background: var(--input-bg); border: 1px solid var(--border); border-radius: 4px;
  padding: 10px 12px; font-family: ui-monospace, Menlo, monospace; font-size: 13px;
  color: var(--green); word-break: break-all;
}

/* Test buttons + generate/copy row */
.btn-row { display: flex; align-items: center; gap: 10px;
           margin-top: 16px; flex-wrap: wrap; }
.btn-row button { margin-top: 0; }
.btn-row.divided { border-top: 1px solid var(--border);
                   margin-top: 20px; padding-top: 20px; }
button.secondary { background: var(--btn-2); color: var(--btn-2-fg); }
button.secondary:hover { background: var(--btn-2-hover); }
.test-btn { position: relative; overflow: hidden;
            background: var(--btn-2); color: var(--btn-2-fg); }
.test-btn:hover { background: var(--btn-2-hover); }
.test-btn:disabled { cursor: default; opacity: 0.9; }
.test-overlay {
  position: absolute; inset: 0; display: flex; align-items: center;
  justify-content: center; gap: 6px; font-weight: 700; pointer-events: none;
  background: var(--btn-2); color: var(--btn-2-fg); border-radius: 4px;
}
.test-overlay.pass { background: var(--green); color: var(--on-status); }
.test-overlay.fail { background: var(--red); color: var(--on-status); }
.test-overlay.show { animation: test-fade 5s ease-out forwards; }
@keyframes test-fade { 0%, 80% { opacity: 1; } 100% { opacity: 0; visibility: hidden; } }
.test-detail { font-size: 13px; color: var(--muted); }
.copied-note { font-size: 13px; color: var(--green); opacity: 0; transition: opacity .2s; }
.copied-note.show { opacity: 1; }

/* Inline checkbox toggles (allow-all / unlimited) */
.inline-check { display: flex; align-items: center; gap: 8px;
                margin: 12px 0 4px; font-weight: 400; color: var(--fg);
                cursor: pointer; }
.inline-check input[type="checkbox"] { width: auto; margin: 0; cursor: pointer; }
input.locked { opacity: 0.5; background: var(--input-locked-bg); cursor: not-allowed; }

/* Narrow screens: tighten chrome, spread the tabs across the full row, and
   bump inputs to 16px (below that iOS Safari zooms the page on focus). */
@media (max-width: 640px) {
  body { padding: 10px; }
  form { padding: 16px; }
  .topbar { gap: 10px; margin-bottom: 10px; }
  .tab-labels { gap: 3px; }
  .tab-labels label { flex: 1 1 auto; text-align: center;
                      padding: 8px 6px; font-size: 13px; }
  input[type="text"], input[type="password"], input[type="file"], textarea {
    font-size: 16px; }
  .btn-row { gap: 8px; }
}
"""


# Vanilla JS for the settings page: connection-test buttons (POST each form's
# values to /admin/test/<which>, render a PASS/FAIL overlay that fades after
# 5s) and the webhook Generate/Copy helpers. Guards on element existence so it
# is harmless on the login/setup/restore pages that reuse _page().
SCRIPT = """
<script>
(function () {
  function showResult(btn, ok, fade) {
    var old = btn.querySelector('.test-overlay');
    if (old) old.remove();
    var ov = document.createElement('span');
    ov.className = 'test-overlay' + (ok === null ? '' : (ok ? ' pass' : ' fail'))
                 + (fade ? ' show' : '');
    ov.textContent = ok === null ? 'Testing\\u2026' : (ok ? 'PASS \\u2713' : 'FAIL \\u2717');
    btn.appendChild(ov);
    if (fade) setTimeout(function () { if (ov.parentNode) ov.remove(); }, 5000);
  }
  document.querySelectorAll('.test-btn').forEach(function (btn) {
    btn.addEventListener('click', async function () {
      var which = btn.dataset.test;
      var form = document.getElementById(btn.dataset.form);
      var detail = document.querySelector('[data-detail="' + which + '"]');
      if (detail) detail.textContent = '';
      btn.disabled = true;
      showResult(btn, null, false);
      try {
        var resp = await fetch('/admin/test/' + which,
                               { method: 'POST', body: new FormData(form) });
        var data = await resp.json();
        showResult(btn, !!data.ok, true);
        if (detail) detail.textContent = data.detail || '';
      } catch (e) {
        showResult(btn, false, true);
        if (detail) detail.textContent = 'Request failed.';
      } finally {
        btn.disabled = false;
      }
    });
  });
  var show = document.getElementById('wh-show');
  if (show) show.addEventListener('click', function () {
    var inp = document.getElementById('webhook_secret');
    var hidden = inp.type === 'password';
    inp.type = hidden ? 'text' : 'password';
    show.textContent = hidden ? 'Hide' : 'Show';
  });
  var gen = document.getElementById('wh-generate');
  if (gen) gen.addEventListener('click', function () {
    var inp = document.getElementById('webhook_secret');
    var bytes = new Uint8Array(32);
    crypto.getRandomValues(bytes);
    var b64 = btoa(String.fromCharCode.apply(null, bytes))
                .replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
    inp.value = b64;
    inp.type = 'text';
    if (show) show.textContent = 'Hide';
  });
  // Allow-all / Unlimited toggles: a checked box makes its paired input
  // readonly + dimmed (value retained, still submitted) and back on un-check.
  function bindLock(cbId, inputId) {
    var cb = document.getElementById(cbId);
    var inp = document.getElementById(inputId);
    if (!cb || !inp) return;
    function sync() {
      inp.readOnly = cb.checked;
      inp.classList.toggle('locked', cb.checked);
    }
    cb.addEventListener('change', sync);
    sync();
  }
  bindLock('autofix-allow-all', 'allowed-ids');
  bindLock('daily-unlimited', 'daily-limit');
  // Theme toggle: swaps Van Helsing (dark) <-> Alucard (light) on <html> and
  // persists the choice; THEME_BOOT reads it back before paint on every page.
  var themeBtn = document.getElementById('theme-toggle');
  function themeSync() {
    var dark = document.documentElement.getAttribute('data-theme') !== 'alucard';
    themeBtn.textContent = dark ? '\\u2600' : '\\u263D';
    themeBtn.title = dark ? 'Switch to Alucard (light)' : 'Switch to Van Helsing (dark)';
  }
  if (themeBtn) {
    themeBtn.addEventListener('click', function () {
      var next = document.documentElement.getAttribute('data-theme') === 'alucard'
        ? 'van-helsing' : 'alucard';
      document.documentElement.setAttribute('data-theme', next);
      try { localStorage.setItem('usher-theme', next); } catch (e) {}
      themeSync();
    });
    themeSync();
  }
  var copy = document.getElementById('wh-copy');
  if (copy) copy.addEventListener('click', async function () {
    var inp = document.getElementById('webhook_secret');
    if (!inp.value) return;
    try {
      await navigator.clipboard.writeText(inp.value);
    } catch (e) {
      inp.type = 'text'; inp.select();
      try { document.execCommand('copy'); } catch (e2) {}
    }
    var note = document.getElementById('wh-copied');
    if (note) {
      note.classList.add('show');
      setTimeout(function () { note.classList.remove('show'); }, 2000);
    }
  });

  // New-Plex-Sign-In warning banner (Seerr tab). Fetched async after page
  // load so a slow or down Seerr never delays panel render; errors keep the
  // banner hidden.
  (async function () {
    var banner = document.getElementById('newplex-banner');
    if (!banner) return;
    try {
      var resp = await fetch('/admin/seerr/newplex-warning');
      var data = await resp.json();
      if (data.show) banner.hidden = false;
    } catch (e) {}
    var dismiss = document.getElementById('newplex-dismiss');
    if (dismiss) dismiss.addEventListener('click', async function () {
      banner.hidden = true;
      var fd = new FormData();
      var tok = document.querySelector('#seerr-form input[name="csrf_token"]');
      if (tok) fd.append('csrf_token', tok.value);
      try {
        await fetch('/admin/seerr/newplex-warning/dismiss',
                    { method: 'POST', body: fd });
      } catch (e) {}
    });
  })();
})();
</script>
"""


# Runs before the stylesheet so the saved theme applies pre-paint (no white
# flash when Alucard users load a page).
THEME_BOOT = """
<script>(function () {
  var t; try { t = localStorage.getItem('usher-theme'); } catch (e) {}
  document.documentElement.setAttribute(
    'data-theme', t === 'alucard' ? 'alucard' : 'van-helsing');
})();</script>
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html>\n"
        f"<html><head><meta charset=\"utf-8\"><title>{_esc(title)} - Usher</title>"
        f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"{THEME_BOOT}<style>{CSS}</style></head><body><div class=\"container\">{body}"
        f"</div>{SCRIPT}</body></html>"
    )


def _flash(message: str = "", error: str = "") -> str:
    out = ""
    if message:
        out += f'<div class="success">{_esc(message)}</div>'
    if error:
        out += f'<div class="error">{_esc(error)}</div>'
    return out


TAB_KEYS = ("telegram", "seerr", "autofix", "sabnzbd", "webhook", "account")


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

    # Auto-fix allow-all / unlimited toggles. The checkbox reflects the saved
    # flag; the paired input is rendered readonly+dimmed when the flag is set
    # (JS keeps it in sync on toggle). readonly -- not disabled -- so the
    # retained list/number still posts and survives a later un-check.
    allow_all_chk = " checked" if s.autofix_allow_all else ""
    unlimited_chk = " checked" if s.daily_autofix_unlimited else ""
    ids_lock = ' readonly class="locked"' if s.autofix_allow_all else ""
    limit_lock = ' readonly class="locked"' if s.daily_autofix_unlimited else ""

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

    req_chk = " checked" if s.tg_notify_requester else ""
    adm_chk = " checked" if s.tg_notify_admin_requests else ""
    fail_chk = " checked" if s.tg_notify_admin_failed else ""
    iss_chk = " checked" if s.tg_notify_issues else ""
    subs_chk = " checked" if s.tg_notify_subscriptions else ""
    cards_chk = " checked" if s.tg_progress_cards else ""
    telegram_form = f"""
<form id="telegram-form" method="POST" action="/admin/telegram">
  {csrf}
  <h2>Telegram</h2>
  <div class="note">Changes to the bot token or admin user ID restart the container so the new identity takes effect.</div>
  <label>Bot Token <span class="note">(from @BotFather)</span></label>
  <input type="password" name="telegram_bot_token" value="{_esc(s.telegram_bot_token)}" autocomplete="off" required>
  <label>Admin Telegram User ID <span class="note">(DM @userinfobot)</span></label>
  <input type="text" name="admin_telegram_id" value="{_esc(admin_tg_val)}" inputmode="numeric" pattern="[0-9]+" required>
  <label>Usher Admin UI URL <span class="note">(optional)</span></label>
  <input type="text" name="usher_public_url" value="{_esc(s.usher_public_url)}" placeholder="http://192.168.1.15:8765 or https://usher.example.com">
  <div class="note">Used in the bot's startup DM to point you back here. Leave blank to fall back to a generic placeholder.</div>

  <h2>Notifications</h2>
  <div class="note">Which DM classes the bot sends. Turning one off silences that class for everyone; events that fire while a class is off are not replayed later.</div>
  <label class="inline-check"><input type="checkbox" name="tg_notify_requester"{req_chk}> Request updates to requesters (approved / declined / available / failed)</label>
  <label class="inline-check"><input type="checkbox" name="tg_notify_admin_requests"{adm_chk}> New-request and auto-approved notices to the admin</label>
  <label class="inline-check"><input type="checkbox" name="tg_notify_admin_failed"{fail_chk}> Failed-download alarm to the admin</label>
  <label class="inline-check"><input type="checkbox" name="tg_notify_issues"{iss_chk}> Issue reported / comment / resolved DMs</label>
  <label class="inline-check"><input type="checkbox" name="tg_notify_subscriptions"{subs_chk}> Availability-watch notifications</label>
  <label class="inline-check"><input type="checkbox" name="tg_progress_cards"{cards_chk}> Morphing progress cards on requests and auto-fixes (paint only: queue boosting and tracking continue while off)</label>

  <div class="btn-row divided">
    <button type="button" class="test-btn" data-test="telegram" data-form="telegram-form">Test</button>
    <button type="submit">Save</button>{marker("telegram")}
    <span class="test-detail" data-detail="telegram"></span>
  </div>
</form>
"""

    seerr_form = f"""
<form id="seerr-form" method="POST" action="/admin/seerr">
  {csrf}
  <h2>Seerr</h2>
  <div id="newplex-banner" class="banner-warn" hidden>
    <b>⚠️ Seerr's "Enable New Plex Sign-In" is turned on.</b>
    Any Plex account with access to your Plex server becomes a Seerr user the
    first time they sign in - including through this bot's /link, which anyone
    who finds the bot can start. If that's not what you want, disable it in
    Seerr under Settings &rarr; Users. (Usher already turns away sign-ins
    Seerr rejects, so disabling it won't break anything here.)
    <button type="button" id="newplex-dismiss">Dismiss</button>
  </div>
  <label>Seerr URL</label>
  <input type="text" name="seerr_url" value="{_esc(s.seerr_url)}" placeholder="http://192.168.1.10:5056" required>
  <label>Seerr API Key</label>
  <input type="password" name="seerr_api_key" value="{_esc(s.seerr_api_key)}" autocomplete="off" required>
  <label>Seerr Public URL <span class="note">(optional, for reverse-proxy links sent to users)</span></label>
  <input type="text" name="seerr_public_url" value="{_esc(s.seerr_public_url)}" placeholder="https://seerr.example.com">
  <div class="btn-row divided">
    <button type="button" class="test-btn" data-test="seerr" data-form="seerr-form">Test</button>
    <button type="submit">Save</button>{marker("seerr")}
    <span class="test-detail" data-detail="seerr"></span>
  </div>
</form>
"""

    autofix_form = f"""
<form id="autofix-form" method="POST" action="/admin/autofix">
  {csrf}
  <h2>Auto-fix</h2>
  <p class="intro">When a user reports a Video, Audio, or Subtitles issue, Usher can ask Radarr or Sonarr to delete the current file and trigger a new search. Configure the URLs and API keys below, then list the Telegram users allowed to use it. The admin always bypasses the per-day limit.</p>

  <label>Radarr URL <span class="note">(optional)</span></label>
  <input type="text" name="radarr_url" value="{_esc(s.radarr_url)}" placeholder="http://192.168.1.10:7878">
  <label>Radarr API Key</label>
  <input type="password" name="radarr_api_key" value="{_esc(s.radarr_api_key)}" autocomplete="off">

  <label>Sonarr URL <span class="note">(optional)</span></label>
  <input type="text" name="sonarr_url" value="{_esc(s.sonarr_url)}" placeholder="http://192.168.1.10:8989">
  <label>Sonarr API Key</label>
  <input type="password" name="sonarr_api_key" value="{_esc(s.sonarr_api_key)}" autocomplete="off">

  <label>Allowed Telegram User IDs</label>
  <label class="inline-check" for="autofix-allow-all">
    <input type="checkbox" id="autofix-allow-all" name="autofix_allow_all"{allow_all_chk}>
    Allow all users
  </label>
  <input type="text" id="allowed-ids" name="allowed_autofix_telegram_ids" value="{_esc(ids_str)}" placeholder="123456,789012"{ids_lock}>
  <div class="note">Comma-separated. Leave empty for admin-only. "Allow all" lets every signed-in user auto-fix.</div>

  <label>Per-user daily limit</label>
  <label class="inline-check" for="daily-unlimited">
    <input type="checkbox" id="daily-unlimited" name="daily_autofix_unlimited"{unlimited_chk}>
    Unlimited
  </label>
  <input type="text" id="daily-limit" name="daily_autofix_limit" value="{_esc(s.daily_autofix_limit)}" inputmode="numeric" pattern="[0-9]+" required{limit_lock}>
  <div class="note">Auto-fix runs per non-admin user per 24 hours. Default {DEFAULT_DAILY_AUTOFIX_LIMIT}. "Unlimited" removes the cap for all users.</div>

  <div class="btn-row divided">
    <button type="button" class="test-btn" data-test="autofix" data-form="autofix-form">Test</button>
    <button type="submit">Save</button>{marker("autofix")}
    <span class="test-detail" data-detail="autofix"></span>
  </div>
</form>
"""

    boost_options = "".join(
        f'<option value="{val}"{" selected" if s.sabnzbd_boost == val else ""}>{label}</option>'
        for val, label in (("off", "Off"), ("high", "High priority"),
                           ("force", "Force (starts immediately)")))
    sabnzbd_form = f"""
<form id="sabnzbd-form" method="POST" action="/admin/sabnzbd">
  {csrf}
  <h2>SABnzbd</h2>
  <p class="intro">Optional. When configured, downloads that originate from this bot (requests and auto-fixes) can be bumped up SABnzbd's queue automatically.</p>
  <label>SABnzbd URL <span class="note">(optional)</span></label>
  <input type="text" name="sabnzbd_url" value="{_esc(s.sabnzbd_url)}" placeholder="http://192.168.1.10:8080">
  <label>SABnzbd API Key</label>
  <input type="password" name="sabnzbd_api_key" value="{_esc(s.sabnzbd_api_key)}" autocomplete="off">
  <label>Auto-bump priority</label>
  <select name="sabnzbd_boost">{boost_options}</select>
  <div class="note">Applied once per download, to anything this bot caused to be grabbed. "Force" starts the job even when the queue is paused or speed-limited.</div>
  <div class="btn-row divided">
    <button type="button" class="test-btn" data-test="sabnzbd" data-form="sabnzbd-form">Test</button>
    <button type="submit">Save</button>{marker("sabnzbd")}
    <span class="test-detail" data-detail="sabnzbd"></span>
  </div>
</form>
"""

    webhook_form = f"""
<form id="webhook-form" method="POST" action="/admin/webhook">
  {csrf}
  <h2>Webhook</h2>
  <p>Usher receives webhook events from Seerr on this URL:</p>
  <div class="url-box">{_esc(webhook_url)}</div>
  <div class="note">Configure in Seerr: Settings → Notifications → Webhook. Set the URL above and enable the <strong>Issue Reported</strong>, <strong>Issue Comment</strong>, and <strong>Issue Resolved</strong> events, plus the request events — <strong>Request Pending Approval</strong>, <strong>Request Approved</strong>, <strong>Request Automatically Approved</strong>, <strong>Request Declined</strong>, <strong>Request Available</strong>, and <strong>Request Processing Failed</strong> — so /request status updates reach users in Telegram.</div>

  <label>Webhook Secret</label>
  <input type="password" id="webhook_secret" name="webhook_secret" value="{_esc(s.webhook_secret)}" autocomplete="off">
  <div class="note">Paste the same value into Seerr's Webhook <code>Authorization Header</code> field. Usher rejects requests without a matching header. <strong>Test</strong> sends a synthetic event to the URL above using the currently <em>saved</em> secret, so Generate &rarr; Save &rarr; Test.</div>
  <div class="btn-row">
    <button type="button" class="secondary" id="wh-show">Show</button>
    <button type="button" class="secondary" id="wh-generate">Generate</button>
    <button type="button" class="secondary" id="wh-copy">Copy</button>
    <span class="copied-note" id="wh-copied">Copied &#10003;</span>
  </div>

  <div class="btn-row divided">
    <button type="button" class="test-btn" data-test="webhook" data-form="webhook-form">Test</button>
    <button type="submit">Save</button>{marker("webhook")}
    <span class="test-detail" data-detail="webhook"></span>
  </div>
</form>
"""

    account_section = f"""
<form method="POST" action="/admin/password">
  {csrf}
  <h2>Change Password</h2>
  <label>Current password</label>
  <input type="password" name="current" required>
  <label>New password</label>
  <input type="password" name="new" required minlength="{ADMIN_PASSWORD_MIN_CHARS}">
  <label>Confirm new password</label>
  <input type="password" name="confirm" required minlength="{ADMIN_PASSWORD_MIN_CHARS}">
  <button type="submit">Change Password</button>{marker("account")}
</form>

<form method="POST" action="/admin/backup">
  {csrf}
  <h2>Download Backup</h2>
  <div class="note">Downloads a ZIP containing settings.json, the mappings database, and the encryption key. Set a passphrase to encrypt it.</div>
  <div class="note">⚠️ Without a passphrase the file contains the encryption key, every API key, the bot token, and all users' Plex tokens in a plain ZIP; anyone who obtains it fully controls the bot and its accounts.</div>
  <label>Passphrase <span class="note">(strongly recommended)</span></label>
  <input type="password" name="passphrase" placeholder="Encrypts the backup">
  <label><input type="checkbox" name="unencrypted_ok" value="1"> I understand the risk; download WITHOUT a passphrase</label>
  <button type="submit">Download Backup</button>{marker("backup")}
</form>

<form method="POST" action="/admin/restore" enctype="multipart/form-data">
  {csrf}
  <h2>Restore from Backup</h2>
  <input type="file" name="backup" accept=".zip,.usher-backup,.hermes-backup" required>
  <label>Passphrase <span class="note">(required only if the backup was wrapped)</span></label>
  <input type="password" name="passphrase" placeholder="Leave blank for plain ZIP">
  <div class="note">Overwrites settings, mappings DB, and encryption key after validating. A backup is created before restoring.</div>
  <button type="submit" class="danger">Restore</button>{marker("restore")}
</form>
"""

    return _page("Admin", f"""
<div class="topbar">
  <span class="version">Usher v{_esc(USHER_VERSION)}</span>
  <button type="button" id="theme-toggle" class="theme-toggle" aria-label="Switch theme"></button>
  <form method="POST" action="/admin/logout" class="logout-form">{csrf}
    <button type="submit" class="logout">Log out</button>
  </form>
</div>
<div class="tabs">
  <input type="radio" name="tab" id="tab-telegram"{chk('telegram')}>
  <input type="radio" name="tab" id="tab-seerr"{chk('seerr')}>
  <input type="radio" name="tab" id="tab-autofix"{chk('autofix')}>
  <input type="radio" name="tab" id="tab-sabnzbd"{chk('sabnzbd')}>
  <input type="radio" name="tab" id="tab-webhook"{chk('webhook')}>
  <input type="radio" name="tab" id="tab-account"{chk('account')}>
  <div class="tab-labels">
    <label for="tab-telegram">Telegram</label>
    <label for="tab-seerr">Seerr</label>
    <label for="tab-autofix">Auto-fix</label>
    <label for="tab-sabnzbd">SABnzbd</label>
    <label for="tab-webhook">Webhook</label>
    <label for="tab-account">Account</label>
  </div>
  <div class="tab-contents">
    <div class="tab-content tab-c-telegram">{telegram_form}</div>
    <div class="tab-content tab-c-seerr">{seerr_form}</div>
    <div class="tab-content tab-c-autofix">{autofix_form}</div>
    <div class="tab-content tab-c-sabnzbd">{sabnzbd_form}</div>
    <div class="tab-content tab-c-webhook">{webhook_form}</div>
    <div class="tab-content tab-c-account">{account_section}</div>
  </div>
</div>
""")


def _webhook_url_from_request(request: web.Request) -> str:
    """Construct the webhook URL the admin is told to paste into Seerr.
    Prefers the configured public URL; otherwise falls back to the request's
    Host header so users see the host:port they hit. X-Forwarded-Proto is
    honored only via request_is_secure (trusted proxies), never raw -- this
    URL receives secret-bearing webhooks, so a spoofable header must not
    shape it."""
    s = request.app["settings_store"].settings
    if s.usher_public_url:
        return f"{s.usher_public_url.rstrip('/')}/webhook/seerr"
    scheme = "https" if request_is_secure(request) else (request.scheme or "http")
    return f"{scheme}://{request.host}/webhook/seerr"


# --- Route handlers ---------------------------------------------------------

async def setup_get(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    if store.settings.admin.is_set():
        return web.HTTPFound("/admin/login")
    setup_token = load_or_create_setup_token(request.app["data_dir"])
    if setup_token == _SETUP_TOKEN_UNREADABLE:
        return web.Response(
            text="Setup token file is unreadable; fix its permissions and retry.",
            status=503)
    csrf = csrf_for_request(request)
    s = store.settings
    admin_tg_val = str(s.admin_telegram_id) if s.admin_telegram_id else ""

    token_field = ""
    if setup_token:
        token_field = """
  <h2>Setup Token</h2>
  <p class="note">A one-time setup token was printed to the container logs on first run.
  Paste it here to prove you have host access (run <code>docker logs usher | grep "setup token"</code>).</p>
  <label>Setup token</label>
  <input type="text" name="setup_token" required autocomplete="off">
"""

    body = _page("Setup", f"""
<h1>Usher First-Time Setup</h1>
<p>Configure the minimum settings needed to bring the bot online. You can change everything later from the admin UI.</p>
<form method="POST" action="/admin/setup">
  {_csrf_input(csrf)}
  {token_field}
  <h2>Admin Account</h2>
  <label>Username</label>
  <input type="text" name="username" required autofocus>
  <label>Password <span class="note">(min 8 characters)</span></label>
  <input type="password" name="password" required minlength="{ADMIN_PASSWORD_MIN_CHARS}">
  <label>Confirm password</label>
  <input type="password" name="confirm" required minlength="{ADMIN_PASSWORD_MIN_CHARS}">

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

  <button type="submit">Save &amp; Start Usher</button>
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
    if setup_token == _SETUP_TOKEN_UNREADABLE:
        return web.Response(
            text="Setup token file is unreadable; fix its permissions and retry.",
            status=503)
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
    if len(password) < ADMIN_PASSWORD_MIN_CHARS or password != confirm:
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
    await store.save_async()
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
<p>Usher is restarting to bring the bot online. Refresh in about 10 seconds and log in.</p>
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
<h1>Usher Admin</h1>
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
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""

    def _locked_response(locked: float) -> web.Response:
        body = _page("Login", _flash(
            error=f"Too many failed attempts. Try again in {int(locked)}s."
        ) + '<p><a href="/admin/login">Back</a></p>')
        return web.Response(text=body, content_type="text/html", status=429,
                            headers={"Retry-After": str(int(locked))})

    # Per-IP aggregate gate FIRST, before any password hashing: a
    # username-rotating flood would otherwise dodge the per-(ip,username)
    # lockout and drive unbounded PBKDF2 work on the event loop.
    ip_locked = _ip_throttle.is_locked(ip)
    if ip_locked is not None:
        audit("login_throttled_ip", ip=ip, seconds_left=int(ip_locked))
        return _locked_response(ip_locked)

    # Then the tighter per-(ip, username) lockout: with a misconfigured
    # reverse proxy every client shares the proxy's IP, so an ip-only key
    # would let anyone lock the admin out of the login page entirely.
    throttle_key = f"{ip}|{username.lower()}"
    locked = _throttle.is_locked(throttle_key)
    if locked is not None:
        audit("login_throttled", user=username or "-", ip=ip,
              seconds_left=int(locked))
        return _locked_response(locked)

    admin = store.settings.admin
    username_ok = admin.is_set() and username == admin.username
    # Constant-work verification: run PBKDF2 even on a wrong username so
    # response timing can't enumerate the admin username. Off the event loop
    # (600k iterations) so it can't stall the webhook receiver / bot HTTP.
    stored = admin.password_hash if username_ok else _TIMING_EQUALIZER_HASH
    password_ok = await asyncio.to_thread(verify_password, password, stored)
    if not (username_ok and password_ok):
        _throttle.record_failure(throttle_key)
        _ip_throttle.record_failure(ip)
        audit("login_fail", user=username or "-", ip=ip)
        body = _page("Login", _flash(error="Invalid credentials.") +
                     '<p><a href="/admin/login">Try again</a></p>')
        return web.Response(text=body, content_type="text/html", status=401)

    _throttle.record_success(throttle_key)
    _ip_throttle.record_success(ip)
    audit("login_success", user=username, ip=ip)

    # PBKDF2 auto-upgrade: if the stored hash uses a stale
    # iteration count, rehash with the current count and persist.
    stored_iters = iterations_of(admin.password_hash)
    if stored_iters and stored_iters < PBKDF2_ITERATIONS:
        try:
            admin.password_hash = hash_password(password)
            await store.save_async()
            audit("password_rehashed",
                  user=username, ip=ip,
                  from_iters=stored_iters, to_iters=PBKDF2_ITERATIONS)
        except Exception:
            # Login itself already succeeded; rehash failure is non-fatal.
            logger.exception("PBKDF2 auto-upgrade rehash failed for %s", username)

    secure = request_is_secure(request)
    cookie = _make_session_cookie(request.app["session_secret"], username,
                                  store.settings.admin.password_version)
    resp = web.HTTPFound("/admin")
    _set_session_cookie(resp, cookie, secure=secure)
    # Rotate CSRF cookie after privilege change, bound to the NEW session
    # cookie's signature so it validates on the first authenticated POST.
    new_binding = cookie.rsplit(".", 1)[-1]
    attach_csrf_cookie(
        resp, generate_csrf_token(request.app["session_secret"], new_binding),
        secure=secure)
    return resp


async def logout(request: web.Request) -> web.Response:
    user = _current_user(request)
    form = await request.post()
    if not validate_csrf(request, form.get(CSRF_FORM_FIELD)):
        audit("logout_csrf_fail", ip=client_ip(request))
        return web.Response(text="CSRF token mismatch.", status=403)
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie and user:
        # Only revoke cookies that verified: recording arbitrary submitted
        # strings would let an unauthenticated client grow the denylist.
        _revoke_session(cookie)
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
    await store.save_async()
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
    public_url = (form.get("usher_public_url") or "").strip()
    url_err = validate_public_url(public_url)
    if url_err:
        return web.Response(
            text=_settings_page(s, error=f"Usher Public URL: {url_err}",
                                active_tab="telegram",
                                webhook_url=_webhook_url_from_request(request),
                                csrf_token=csrf_for_request(request)),
            content_type="text/html", status=400,
        )
    s.telegram_bot_token = token
    s.admin_telegram_id = admin_tg
    s.usher_public_url = public_url
    for flag in ("tg_notify_requester", "tg_notify_admin_requests",
                 "tg_notify_admin_failed", "tg_notify_issues",
                 "tg_notify_subscriptions", "tg_progress_cards"):
        setattr(s, flag, form.get(flag) is not None)
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
    # Same validation as usher_public_url (one field class had two
    # behaviors); non-empty URLs must parse, empty stays allowed where the
    # field is optional.
    for field_name, label in (("seerr_url", "Seerr URL"),
                              ("seerr_public_url", "Seerr Public URL")):
        value = (form.get(field_name) or "").strip()
        url_err = validate_public_url(value) if value else None
        if url_err:
            return web.Response(
                text=_settings_page(s, error=f"{label}: {url_err}",
                                    active_tab="seerr",
                                    webhook_url=_webhook_url_from_request(request),
                                    csrf_token=csrf_for_request(request)),
                content_type="text/html", status=400,
            )
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
    for field_name, label in (("radarr_url", "Radarr URL"),
                              ("sonarr_url", "Sonarr URL")):
        value = (form.get(field_name) or "").strip()
        url_err = validate_public_url(value) if value else None
        if url_err:
            return web.Response(
                text=_settings_page(s, error=f"{label}: {url_err}",
                                    active_tab="autofix",
                                    webhook_url=_webhook_url_from_request(request),
                                    csrf_token=csrf_for_request(request)),
                content_type="text/html", status=400,
            )
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
    # The allowlist is always retained even when "Allow all" is on, so an
    # un-check later restores exactly the IDs that were entered.
    s.allowed_autofix_telegram_ids = parsed_ids
    s.autofix_allow_all = form.get("autofix_allow_all") is not None
    unlimited = form.get("daily_autofix_unlimited") is not None
    s.daily_autofix_unlimited = unlimited
    limit_raw = (form.get("daily_autofix_limit") or "").strip()
    try:
        limit = int(limit_raw)
        if limit < 1:
            raise ValueError
    except ValueError:
        # When unlimited, the numeric box is readonly and just being retained;
        # a missing/odd value shouldn't block the save -- keep the prior limit
        # (or the default) so it's there if unlimited is turned back off.
        if unlimited:
            limit = s.daily_autofix_limit or DEFAULT_DAILY_AUTOFIX_LIMIT
        else:
            return web.Response(
                text=_settings_page(s, error="Per-user daily limit must be a positive integer.",
                                    active_tab="autofix",
                                    webhook_url=_webhook_url_from_request(request),
                                    csrf_token=csrf_for_request(request)),
                content_type="text/html", status=400,
            )
    s.daily_autofix_limit = limit
    return await _save_and_render(request, active_tab="autofix")


async def sabnzbd_post(request: web.Request) -> web.Response:
    store: SettingsStore = request.app["settings_store"]
    form = await request.post()
    csrf_resp = _csrf_check_or_403(request, form)
    if csrf_resp is not None:
        return csrf_resp
    s = store.settings
    value = (form.get("sabnzbd_url") or "").strip()
    url_err = validate_public_url(value) if value else None
    if url_err:
        return web.Response(
            text=_settings_page(s, error=f"SABnzbd URL: {url_err}",
                                active_tab="sabnzbd",
                                webhook_url=_webhook_url_from_request(request),
                                csrf_token=csrf_for_request(request)),
            content_type="text/html", status=400,
        )
    s.sabnzbd_url = value
    s.sabnzbd_api_key = (form.get("sabnzbd_api_key") or "").strip()
    boost = (form.get("sabnzbd_boost") or "off").strip()
    s.sabnzbd_boost = boost if boost in ("off", "high", "force") else "off"
    return await _save_and_render(request, active_tab="sabnzbd")


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
    if len(new) < ADMIN_PASSWORD_MIN_CHARS or new != confirm:
        return web.Response(
            text=_settings_page(store.settings, error="New password must be >= 8 chars and match confirm.",
                                active_tab="account",
                                webhook_url=_webhook_url_from_request(request),
                                csrf_token=csrf_for_request(request)),
            content_type="text/html", status=400,
        )
    user = _current_user(request) or "-"
    admin.password_hash = hash_password(new)
    # Invalidate every outstanding session: a stolen cookie
    # must die when the admin rotates the password in response to it.
    admin.password_version += 1
    await store.save_async()
    audit("password_changed", user=user, ip=client_ip(request))
    # Re-issue THIS session at the new version so the admin who changed the
    # password isn't logged out mid-page. The session signature changes, so
    # the browser's CSRF cookie is no longer bound to it: mint ONE new
    # bound token and use it for both the cookie and the rendered form, or
    # the next POST on this page would 403.
    secure = request_is_secure(request)
    cookie = _make_session_cookie(request.app["session_secret"],
                                  admin.username, admin.password_version)
    new_binding = cookie.rsplit(".", 1)[-1]
    new_csrf = generate_csrf_token(request.app["session_secret"], new_binding)
    resp = web.Response(
        text=_settings_page(store.settings,
                            message="Password changed. All other sessions are signed out.",
                            active_tab="account",
                            webhook_url=_webhook_url_from_request(request),
                            csrf_token=new_csrf),
        content_type="text/html",
    )
    _set_session_cookie(resp, cookie, secure=secure)
    attach_csrf_cookie(resp, new_csrf, secure=secure)
    return resp


async def backup_download(request: web.Request) -> web.Response:
    data_dir: Path = request.app["data_dir"]
    settings_path: Path = request.app["settings_path"]
    db_path: Path = Path(request.app["db_path"])
    enc_key_path = data_dir / "encryption.key"
    store: SettingsStore = request.app["settings_store"]

    form = await request.post()
    csrf_resp = _csrf_check_or_403(request, form)
    if csrf_resp is not None:
        return csrf_resp
    def _backup_error(message: str, status: int = 400) -> web.Response:
        return web.Response(
            text=_settings_page(store.settings, error=message,
                                active_tab="account",
                                marker_target="backup",
                                webhook_url=_webhook_url_from_request(request),
                                csrf_token=csrf_for_request(request)),
            content_type="text/html", status=status,
        )

    passphrase = form.get("passphrase") or ""
    # Server-side gate for the unencrypted download: with an
    # empty passphrase the ZIP is a total-compromise artifact (encryption
    # key + the DB it decrypts + every API key/token), so it requires the
    # explicit acknowledgement checkbox.
    if not passphrase and not form.get("unencrypted_ok"):
        return _backup_error("Set a passphrase, or tick the acknowledgement "
                             "to download an unencrypted backup.")
    # A 1-character passphrase would pass the gate above while wrapping a
    # total-compromise artifact PBKDF2 can't save from a tiny keyspace.
    if passphrase and len(passphrase) < MIN_BACKUP_PASSPHRASE_CHARS:
        return _backup_error(
            f"Backup passphrase must be at least "
            f"{MIN_BACKUP_PASSPHRASE_CHARS} characters.")

    def _build_zip() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            if settings_path.exists():
                zf.write(settings_path, "settings.json")
            if db_path.exists():
                # The store runs in WAL mode: copying the bare .sqlite file
                # misses uncheckpointed commits (recent links, pending fixes)
                # and a mid-checkpoint copy can be torn.
                # VACUUM INTO produces a complete, consistent snapshot.
                # Unique name per request: a PID-keyed name collides when two
                # backups run concurrently (first one's cleanup deletes the
                # file out from under the second's vacuum).
                snap = db_path.with_name(
                    f".backup-snapshot-{os.getpid()}-{uuid.uuid4().hex}.sqlite")
                try:
                    conn = sqlite3.connect(db_path)
                    try:
                        conn.execute("VACUUM INTO ?", (str(snap),))
                    finally:
                        conn.close()
                    zf.write(snap, "mappings.sqlite")
                finally:
                    snap.unlink(missing_ok=True)
            if enc_key_path.exists():
                zf.write(enc_key_path, "encryption.key")
        return buf.getvalue()

    try:
        # VACUUM INTO can take a while on a big DB and sqlite3 is
        # synchronous: run off the event loop so the webhook receiver and
        # bot keep servicing requests during the backup.
        raw_zip = await asyncio.to_thread(_build_zip)
    except Exception as exc:
        logger.exception("Backup build failed")
        return _backup_error(f"Backup failed: {exc}", status=500)

    if passphrase:
        # 600k-iteration PBKDF2: off the event loop like the ZIP build above.
        blob = await asyncio.to_thread(wrap, raw_zip, passphrase)
        ext = "usher-backup"
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
            "Content-Disposition": f'attachment; filename="usher-backup-{ts}.{ext}"',
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
            # multipart()/BodyPartReader don't honor client_max_size, so cap
            # the streamed read ourselves; otherwise a multi-GB body OOMs the
            # container before validation runs.
            chunks: list[bytes] = []
            read_total = 0
            too_big = False
            while True:
                chunk = await field.read_chunk(size=64 * 1024)
                if not chunk:
                    break
                read_total += len(chunk)
                if read_total > ADMIN_UPLOAD_MAX_BYTES:
                    too_big = True
                    break
                chunks.append(chunk)
            if too_big:
                return _restore_error(
                    request,
                    f"Backup upload exceeds the "
                    f"{ADMIN_UPLOAD_MAX_BYTES // (1024 * 1024)}MB limit.",
                    status=413)
            file_bytes = b"".join(chunks)

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
            # 600k-iteration PBKDF2: off the event loop.
            file_bytes = await asyncio.to_thread(unwrap, file_bytes, passphrase)
        except ValueError as exc:
            return _restore_error(request, f"Couldn't decrypt backup: {exc}")

    # Validate ZIP structure + member integrity before touching disk.
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            names = set(zf.namelist())
            if "settings.json" not in names and "mappings.sqlite" not in names:
                raise ValueError("Backup must contain settings.json and/or mappings.sqlite")
            # The upload cap bounds compressed size only; a zip bomb can
            # deflate ~1000:1. Check declared decompressed sizes before any
            # zf.read pulls a member fully into memory.
            for info in zf.infolist():
                if info.file_size > RESTORE_MEMBER_MAX_BYTES:
                    raise ValueError(
                        f"Backup member {info.filename} decompresses to "
                        f"{info.file_size} bytes (limit "
                        f"{RESTORE_MEMBER_MAX_BYTES}).")
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
    snapshot_ok = True
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        for src in (settings_path, db_path, enc_key_path):
            if Path(src).exists():
                shutil.copy2(src, backup_dir / Path(src).name)
    except Exception:
        snapshot_ok = False
        logger.exception("pre-restore snapshot failed; proceeding anyway")

    # Hold the store's maintenance lock (when the bot wired one in) across
    # the DB swap: store writes run in worker threads, and one landing
    # between the rename and the WAL unlink would get its fresh WAL deleted.
    user_store = request.app.get("user_store")
    maintenance_lock = getattr(user_store, "maintenance_lock", None)
    lock_held = False
    try:
        if maintenance_lock is not None:
            await maintenance_lock.acquire()
            lock_held = True
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            # Atomic per-file (temp + rename via fsutil): the bot keeps
            # running until the scheduled exit below, and a bare write_bytes
            # over the live files could be caught mid-write by power loss or
            # a concurrent reader. The rename swaps whole files,
            # so any in-flight reader sees old-or-new, never a torn file.
            # Default 0600 perms throughout: settings.json carries every
            # secret, and chmod=None would land them world-readable on the
            # host-mounted volume.
            if "settings.json" in names:
                atomic_write_bytes(settings_path, zf.read("settings.json"))
            if "mappings.sqlite" in names:
                atomic_write_bytes(db_path, zf.read("mappings.sqlite"))
                # Drop stale WAL/SHM sidecars: SQLite would otherwise pair
                # the restored DB with the OLD database's write-ahead log
                # and replay unrelated frames into it.
                for suffix in ("-wal", "-shm"):
                    Path(str(db_path) + suffix).unlink(missing_ok=True)
            if "encryption.key" in names:
                atomic_write_bytes(enc_key_path, zf.read("encryption.key"))
    except Exception as exc:
        return _restore_error(request, f"Restore failed: {exc}", status=500)
    finally:
        # Release only if THIS handler acquired it: lock.locked() is true when
        # anyone holds it, so releasing on that condition could drop a lock a
        # concurrent store write owns (e.g. if acquire() was cancelled).
        if lock_held:
            maintenance_lock.release()

    audit("restore_complete", user=_current_user(request) or "-",
          ip=client_ip(request), backup_dir=str(backup_dir))
    logger.info("Restore complete; restarting in 2s (snapshot at %s)", backup_dir)
    schedule_clean_exit(2.0)

    snapshot_note = (
        f'<p class="note">Previous files snapshot to <code>{_esc(backup_dir)}</code>.</p>'
        if snapshot_ok else
        '<p class="note">⚠️ Pre-restore snapshot FAILED (see logs); no rollback copy was saved.</p>'
    )
    body = _page("Restore", f"""
<h1>Restore Complete</h1>
<p>Container is restarting. Refresh in a few seconds.</p>
{snapshot_note}
""")
    return web.Response(text=body, content_type="text/html")


# --- Connection tests -------------------------------------------------------

def _test_json(ok: bool, detail: str, status: int = 200) -> web.Response:
    return web.json_response({"ok": ok, "detail": detail}, status=status)


async def _test_csrf_guard(request: web.Request, form) -> Optional[web.Response]:
    """CSRF gate for the JSON test endpoints. Returns a JSON 403 or None."""
    if not validate_csrf(request, form.get(CSRF_FORM_FIELD)):
        audit("admin_csrf_fail", user=_current_user(request) or "-",
              ip=client_ip(request), path=request.path)
        return _test_json(False, "CSRF token mismatch.", status=403)
    return None


async def test_telegram(request: web.Request) -> web.Response:
    """Validate the posted bot token against Telegram's getMe. Tests the
    typed (unsaved) value so you can verify before saving."""
    form = await request.post()
    guard = await _test_csrf_guard(request, form)
    if guard is not None:
        return guard
    token = (form.get("telegram_bot_token") or "").strip()
    if not token:
        return _test_json(False, "No bot token provided.")
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"https://api.telegram.org/bot{token}/getMe")
        data = r.json()
        if r.status_code == 200 and data.get("ok"):
            return _test_json(True, f"Connected as @{data['result'].get('username', '?')}")
        return _test_json(False, data.get("description") or f"HTTP {r.status_code}")
    except Exception as exc:
        return _test_json(False, user_friendly_message(exc))


async def test_seerr(request: web.Request) -> web.Response:
    """Ping Seerr with the typed (unsaved) URL + API key."""
    form = await request.post()
    guard = await _test_csrf_guard(request, form)
    if guard is not None:
        return guard
    url = (form.get("seerr_url") or "").strip()
    key = (form.get("seerr_api_key") or "").strip()
    if not url or not key:
        return _test_json(False, "Seerr URL and API key are required.")
    client = SeerrClient(url, key)
    try:
        version = await client.ping()
        return _test_json(True, f"Connected (Seerr v{version})")
    except Exception as exc:
        return _test_json(False, user_friendly_message(exc))
    finally:
        await client.close()


async def test_autofix(request: web.Request) -> web.Response:
    """Ping whichever of Radarr/Sonarr have a URL filled in. PASS only if
    every configured client succeeds."""
    form = await request.post()
    guard = await _test_csrf_guard(request, form)
    if guard is not None:
        return guard
    targets = (
        ("Radarr", RadarrClient, form.get("radarr_url"), form.get("radarr_api_key")),
        ("Sonarr", SonarrClient, form.get("sonarr_url"), form.get("sonarr_api_key")),
    )
    results: list[str] = []
    ok_all = True
    any_configured = False
    for name, cls, url, key in targets:
        url = (url or "").strip()
        key = (key or "").strip()
        if not url:
            continue
        any_configured = True
        if not key:
            ok_all = False
            results.append(f"{name}: API key missing")
            continue
        client = cls(url, key)
        try:
            version = await client.ping()
            results.append(f"{name}: v{version}")
        except Exception as exc:
            ok_all = False
            results.append(f"{name}: {user_friendly_message(exc)}")
        finally:
            await client.close()
    if not any_configured:
        return _test_json(False, "Neither Radarr nor Sonarr is configured.")
    return _test_json(ok_all, " · ".join(results))


async def test_sabnzbd(request: web.Request) -> web.Response:
    """Ping SABnzbd with the typed (unsaved) URL + API key."""
    form = await request.post()
    guard = await _test_csrf_guard(request, form)
    if guard is not None:
        return guard
    url = (form.get("sabnzbd_url") or "").strip()
    key = (form.get("sabnzbd_api_key") or "").strip()
    if not url or not key:
        return _test_json(False, "SABnzbd URL and API key are required.")
    from sabnzbd import SabnzbdClient
    client = SabnzbdClient(url, key)
    try:
        version = await client.ping()
        return _test_json(True, f"Connected (SABnzbd {version})")
    except Exception as exc:
        return _test_json(False, user_friendly_message(exc))
    finally:
        await client.close()


async def test_webhook(request: web.Request) -> web.Response:
    """Self-POST a synthetic TEST_NOTIFICATION to the live webhook URL using
    the SAVED secret (the receiver only knows the saved value), confirming the
    receiver is reachable and the secret round-trips."""
    form = await request.post()
    guard = await _test_csrf_guard(request, form)
    if guard is not None:
        return guard
    store: SettingsStore = request.app["settings_store"]
    secret = (store.settings.webhook_secret or "").strip()
    if not secret:
        return _test_json(False, "No saved secret. Generate, Save, then Test.")
    # Self-POST to loopback, never a Host-derived URL: the Host
    # header is attacker-influenceable, and this request carries the real
    # webhook secret in Authorization -- a spoofed Host exfiltrated it to an
    # arbitrary target. The receiver runs in this same process/port.
    url = f"http://127.0.0.1:{request.app['http_port']}/webhook/seerr"
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(url, headers={"Authorization": secret},
                             json={"notification_type": "TEST_NOTIFICATION"})
        if r.status_code == 200:
            return _test_json(True, "Receiver accepted the test event.")
        if r.status_code == 401:
            return _test_json(False, "Receiver rejected the secret (401). Save first?")
        return _test_json(False, f"Receiver returned HTTP {r.status_code}.")
    except Exception as exc:
        return _test_json(False, user_friendly_message(exc))


# --- Seerr New-Plex-Sign-In warning ------------------------------------------

async def newplex_warning_check(request: web.Request) -> web.Response:
    """Report whether the dismissible New-Plex-Sign-In banner should show.
    Session-gated by the auth middleware. Silent on every failure path: the
    panel must render normally with Seerr down, unconfigured, or too old to
    have the setting."""
    store: SettingsStore = request.app["settings_store"]
    s = store.settings
    if not (s.seerr_url and s.seerr_api_key):
        return web.json_response({"show": False})
    client = SeerrClient(s.seerr_url, s.seerr_api_key)
    try:
        main = await client.get_main_settings()
    except Exception:
        logger.debug("newplex check: couldn't read Seerr main settings", exc_info=True)
        return web.json_response({"show": False})
    finally:
        await client.close()
    enabled = main.get("newPlexLogin")
    if enabled is None:
        return web.json_response({"show": False})  # setting absent: unsupported build
    if not enabled:
        # Observed OFF: clear any dismissal so the warning re-arms if the
        # setting is ever flipped back on (off -> on transition semantics).
        # Deliberate state change on a GET: this endpoint is a same-origin
        # JS poll, and the only mutation RE-ARMS a warning (fail-safe
        # direction), so a forged GET can't suppress anything.
        if s.seerr_new_plex_login_ack:
            s.seerr_new_plex_login_ack = False
            await store.save_async()
        return web.json_response({"show": False})
    return web.json_response({"show": not s.seerr_new_plex_login_ack})


async def newplex_warning_dismiss(request: web.Request) -> web.Response:
    form = await request.post()
    guard = await _test_csrf_guard(request, form)
    if guard is not None:
        return guard
    store: SettingsStore = request.app["settings_store"]
    store.settings.seerr_new_plex_login_ack = True
    await store.save_async()
    audit("newplex_warning_dismissed", user=_current_user(request) or "-",
          ip=client_ip(request))
    return web.json_response({"ok": True})


# --- Auth middleware --------------------------------------------------------

PUBLIC_ADMIN_PATHS = {"/admin/setup", "/admin/login"}


@web.middleware
async def auth_middleware(request: web.Request, handler) -> web.Response:
    path = request.path
    if not path.startswith("/admin"):
        return await handler(request)

    def _no_store(resp: web.StreamResponse) -> web.StreamResponse:
        # /admin responses embed the bot token and every API key as form
        # values; an intermediary or disk cache must never persist them.
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    store: SettingsStore = request.app["settings_store"]
    # First-run: force /admin/setup until admin exists
    if not store.settings.admin.is_set() and path != "/admin/setup":
        return _no_store(web.HTTPFound("/admin/setup"))
    if path in PUBLIC_ADMIN_PATHS:
        return _no_store(await handler(request))
    if _current_user(request):
        return _no_store(await handler(request))
    return _no_store(web.HTTPFound("/admin/login"))


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
    trusted_proxies: tuple = (),
    http_port: int = 8765,
    user_store=None,
) -> None:
    app["settings_store"] = settings_store
    app["session_secret"] = session_secret
    app["data_dir"] = data_dir
    app["settings_path"] = settings_path
    app["db_path"] = db_path
    # UserStore (optional): restore uses its maintenance lock to serialize
    # the DB-file swap against in-flight store writes.
    app["user_store"] = user_store
    app["on_settings_changed"] = on_settings_changed
    # Used by the webhook self-test to build its loopback URL.
    app["http_port"] = http_port
    # Trusted-proxy CIDRs (TRUSTED_PROXIES env): gates X-Forwarded-For /
    # X-Forwarded-Proto trust in auth_util.client_ip / request_is_secure.
    app["trusted_proxies"] = trusted_proxies
    app.middlewares.append(auth_middleware)
    app.router.add_get("/admin/setup", setup_get)
    app.router.add_post("/admin/setup", setup_post)
    app.router.add_get("/admin/login", login_get)
    app.router.add_post("/admin/login", login_post)
    app.router.add_post("/admin/logout", logout)
    app.router.add_get("/admin", admin_get)
    app.router.add_post("/admin/telegram", telegram_post)
    app.router.add_post("/admin/seerr", seerr_post)
    app.router.add_post("/admin/autofix", autofix_post)
    app.router.add_post("/admin/sabnzbd", sabnzbd_post)
    app.router.add_post("/admin/webhook", webhook_post)
    app.router.add_post("/admin/test/telegram", test_telegram)
    app.router.add_post("/admin/test/seerr", test_seerr)
    app.router.add_post("/admin/test/autofix", test_autofix)
    app.router.add_post("/admin/test/sabnzbd", test_sabnzbd)
    app.router.add_post("/admin/test/webhook", test_webhook)
    app.router.add_get("/admin/seerr/newplex-warning", newplex_warning_check)
    app.router.add_post("/admin/seerr/newplex-warning/dismiss", newplex_warning_dismiss)
    app.router.add_post("/admin/password", change_password)
    app.router.add_post("/admin/backup", backup_download)
    app.router.add_post("/admin/restore", restore_upload)
