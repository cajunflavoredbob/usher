"""Users never see the names of the backend services Usher talks to.

Text a regular User can see (bot messages, buttons, command help, error
replies) says what happened in plain words ("the request system", "the
download system", "watch history") instead of naming Seerr, Sonarr, Radarr,
Tautulli, SABnzbd or "the arrs". Plex stays: Users link their Plex account
and watch there.

Two guards:
- A source scan of every string literal in the bot package and the modules
  whose text reaches Users directly (the Fix results and previews). A string
  that names a service fails unless ADMIN_ONLY lists it with the reason only
  the Admin (or only a log line) sees it. Docstrings and logger arguments
  are skipped. The web panel (webui.py) is all /admin and is not scanned.
- user_friendly_message, the one formatter for upstream errors, renders a
  neutral name unless the caller passes admin=True.
"""
import ast
import pathlib
import re

import pytest

from http_util import (AmbiguousWriteError, NotFoundAPIError,
                       PermanentAPIError, TransientAPIError,
                       neutral_service_names, user_friendly_message)

ROOT = pathlib.Path(__file__).resolve().parent.parent

# A name as a word: "SonarrEpisode" (a type) and "seerr_url" (a key) pass.
SERVICE_NAME = re.compile(
    r"(?<![A-Za-z_])(Seerr|Overseerr|Jellyseerr|Tautulli|Sonarr|Radarr|Lidarr|"
    r"Readarr|Bazarr|Prowlarr|SABnzbd|NZBGet|[Aa]rrs?)(?![A-Za-z_])")

SCANNED = sorted(
    [p for p in (ROOT / "bot").glob("*.py")]
    + [ROOT / "radarr.py", ROOT / "sonarr.py", ROOT / "fix_result.py"])

_LOG_CALLS = {"debug", "info", "warning", "error", "exception", "critical"}

# (file, exact string literal) -> who sees it. Every entry is shown only to
# the Admin, used only as a lookup key or log label, or is code.
ADMIN_ONLY = {
    # Admin connection status and its labels (cmd_start's admin branch,
    # /status, the startup DM).
    ("bot/app.py", "Seerr"): "client label in the Admin's connection status",
    ("bot/app.py", "Radarr"): "client label in the Admin's connection status",
    ("bot/app.py", "Sonarr"): "client label in the Admin's connection status",
    ("bot/app.py", "SABnzbd"): "client label in the Admin's connection status",
    ("bot/app.py", "Tautulli"): "client label in the Admin's connection status",
    ("bot/app.py", "Seerr webhook"): "the Admin's connection status",
    ("bot/app.py", "❌ webhook agent disabled in Seerr, "
                   "no ticket/request DMs will arrive"): "the Admin's connection status",
    ("bot/app.py", "Hi! I take movie/TV requests and file tickets in Seerr."):
        "the Admin's /start; Users get their own greeting",
    ("bot/app.py", "(/mystats shows your watch stats when Tautulli is linked.)"):
        "the Admin's /start command list",
    # Health alarms DM the Admin only.
    ("bot/alarms.py", "Radarr"): "Admin health alarm label",
    ("bot/alarms.py", "Sonarr"): "Admin health alarm label",
    # Downloads is an Admin Hub screen (refuse_outside_admin_dm).
    ("bot/downloads.py", "SABnzbd has no maximum speed set, so it ignores a "
                         "percent limit. Set the maximum bandwidth in SABnzbd first."):
        "Admin Hub Downloads screen",
    ("bot/downloads.py", "SABnzbd isn't linked."): "Admin Hub Downloads screen",
    ("bot/downloads.py", "Couldn't reach SABnzbd. "): "Admin Hub Downloads screen",
    # Invite, Uninvite and Shares are Admin flows.
    ("bot/invite_flow.py", "They lose the server immediately; Seerr and their "
                           "/link die with their Plex access."): "Admin Uninvite confirm",
    ("bot/invite_flow.py", ").\nThey'll get an email from Plex; once they accept "
                           "and sign in, Seerr picks them up and /link works for them."):
        "Admin Invite result",
    ("bot/invite_flow.py", "Tautulli isn't linked."): "Admin Shares activity screen",
    ("bot/invite_flow.py", "Couldn't ask Tautulli. "): "Admin Shares activity screen",
    # Now playing sits under the Admin's Status.
    ("bot/nowplaying.py", "Tautulli isn't linked."): "Admin Now playing",
    ("bot/nowplaying.py", "Couldn't ask Tautulli. "): "Admin Now playing",
    ("bot/nowplaying.py", "\n\n<b>Now playing:</b>\n  Couldn't ask Tautulli. "):
        "Admin Now playing",
    # Pending/handled request DMs go to the Admin only.
    ("bot/request_admin.py", "ℹ️ Pending approval in Seerr"): "Admin request DM",
    ("bot/request_admin.py", "ℹ️ Already approved in Seerr"): "Admin request DM",
    ("bot/request_admin.py", "ℹ️ Already declined in Seerr"): "Admin request DM",
    ("bot/request_admin.py", "ℹ️ Already failed in Seerr"): "Admin request DM",
    ("bot/request_admin.py", "ℹ️ Already completed in Seerr"): "Admin request DM",
    ("bot/request_admin.py", "Seerr isn't set up."): "Admin request DM buttons",
    ("bot/request_admin.py", "\n\nℹ️ This request no longer exists in Seerr"):
        "Admin request DM",
    ("bot/webhook_handlers.py", "📥 New request, approve it in Seerr"):
        "admin_line of MEDIA_PENDING",
    ("bot/webhook_handlers.py", "⚠️ Request FAILED: check Radarr/Sonarr"):
        "admin_line of MEDIA_FAILED",
    # The not-configured hint's Admin branch (Users get a different line).
    ("bot/shared.py", "Usher isn't configured yet. Fill in Seerr settings at "
                      "http://<host>:"): "require_seerr's Admin branch",
    # Admin-only Fix menu and Auto-fix notes.
    ("bot/autofix_poll.py", "If it was a good release, remove it from the "
                            "blocklist in Sonarr or Radarr."):
        "_notify_admin_blocklisted DMs the Admin",
    ("bot/autofix_poll.py", " Check Sonarr/Radarr to see if a release was grabbed."):
        "appended only when the Fix's owner is the Admin",
    ("bot/request_admin.py", "ℹ️ This request changed in Seerr"): "Admin request DM",
    # Long-list footers: only the Admin's list adds where the rest live.
    ("bot/requests_cmd.py", "; the rest are in Seerr"): "set only when is_admin",
    ("bot/tickets.py", "; manage the rest in Seerr"): "set only when is_admin",
    # The Admin-only Fix menu (tk_fix, require_admin).
    ("bot/tickets.py", "Radarr: not configured."): "tk_fix preview, Admin only",
    ("bot/tickets.py", "Sonarr: not configured."): "tk_fix preview, Admin only",
    # Client modules: the service name each client hands its errors, which
    # user_friendly_message turns neutral for Users.
    ("radarr.py", "Radarr"): "_SERVICE error label",
    ("sonarr.py", "Sonarr"): "_SERVICE error label",
}


def _strings(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)):
                skip.add(id(body[0].value))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _LOG_CALLS:
                skip.update(id(n) for n in ast.walk(node))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in skip):
            yield node.lineno, node.value


def _offenders():
    found = []
    for path in SCANNED:
        rel = path.relative_to(ROOT).as_posix()
        for line, text in _strings(path):
            if SERVICE_NAME.search(text) and (rel, text) not in ADMIN_ONLY:
                found.append(f"{rel}:{line}: {text!r}")
    return found


def test_no_user_facing_string_names_a_backend_service():
    offenders = _offenders()
    assert not offenders, (
        "These strings name a backend service. Reword them for Users, or add "
        "them to ADMIN_ONLY with why only the Admin sees them:\n"
        + "\n".join(offenders))


def test_every_admin_only_entry_still_exists():
    present = set()
    for path in SCANNED:
        rel = path.relative_to(ROOT).as_posix()
        present.update((rel, text) for _, text in _strings(path))
    stale = sorted(key for key in ADMIN_ONLY if key not in present)
    assert not stale, f"ADMIN_ONLY lists strings no longer in the code: {stale}"


@pytest.mark.parametrize("service", ["Seerr", "Sonarr", "Radarr", "Tautulli",
                                     "SABnzbd"])
@pytest.mark.parametrize("exc_type", [TransientAPIError, NotFoundAPIError,
                                      PermanentAPIError])
def test_user_error_text_hides_the_service(service, exc_type):
    exc = exc_type(f"{service} said no", service=service)
    text = user_friendly_message(exc)
    assert not SERVICE_NAME.search(text), text


def test_ambiguous_write_hides_the_service():
    text = user_friendly_message(AmbiguousWriteError(service="Seerr"))
    assert "Seerr" not in text
    assert "check before trying again" in text


def test_admin_error_text_keeps_the_service():
    exc = TransientAPIError("busy", service="Sonarr")
    assert "Sonarr" in user_friendly_message(exc, admin=True)


def test_plex_keeps_its_name_for_users():
    exc = TransientAPIError("busy", service="Plex")
    assert "Plex" in user_friendly_message(exc)


@pytest.mark.parametrize("raw, neutral", [
    ("Seerr accepted the request but didn't return an id; check Seerr "
     "before retrying",
     "The request system accepted the request but didn't return an id; "
     "check the request system before retrying"),
    ("Series isn't in Sonarr.", "Series isn't in the download system."),
    ("check Sonarr/Radarr", "check the download system"),
    ("Seerr's sign-in response", "The request system's sign-in response"),
    ("Tautulli rejected it", "Watch history rejected it"),
    ("SABnzbd rejected it", "The downloader rejected it"),
    ("Plex is down", "Plex is down"),
    ("seerr rejected it", "The request system rejected it"),
    ("SONARR timed out", "The download system timed out"),
    ("check the Sonarr queue", "check the download system queue"),
    ("the arrs are down", "The download system are down"),
    ("x\nSeerr said no", "x\nThe request system said no"),
    ("check Sonarr, Radarr or Lidarr", "check the download system"),
    ("Radarr/Sonarr/Lidarr failed", "The download system failed"),
])
def test_neutral_service_names(raw, neutral):
    assert neutral_service_names(raw) == neutral
