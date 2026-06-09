"""Cross-module helpers, constants, and conversation-state values.

Lives at the bottom of the package's import graph so any other bot.*
module can pull from it without circular imports.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timezone
from typing import Final, Optional

import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes, ConversationHandler

from seerr import SeerrClient
from store import UserStore

logger = logging.getLogger("hermes")

# --- Conversation states ----------------------------------------------------

# Issue creation
TITLE, PICK_MEDIA, PICK_SEASON, PICK_EPISODE, PICK_TYPE, DESCRIPTION, OFFER_AUTOFIX, CONFIRM_AUTOFIX = range(8)
# Post-completion follow-up
AWAIT_COMMENT = 100
# Plex link flow
AWAIT_LINK_CONSENT = 200
AWAIT_PLATFORM_CHOICE = 201
# Ticket management (reply to existing issue)
AWAIT_TICKET_REPLY = 400

# --- Issue type maps --------------------------------------------------------

ISSUE_TYPES: Final = {
    1: ("🎥", "Video"),
    2: ("🔊", "Audio"),
    3: ("📝", "Subtitles"),
    4: ("❓", "Other"),
}

AUTOFIX_ELIGIBLE_TYPES = {1, 2, 3}

# Maps Seerr's issueType enum string (from webhook payloads) to a (emoji, label)
# pair. Subtitle/Subtitles spelling varies between Seerr forks.
ISSUE_TYPE_LABELS: Final = {
    "VIDEO": ("🎥", "Video"),
    "AUDIO": ("🔊", "Audio"),
    "SUBTITLES": ("📝", "Subtitle"),
    "SUBTITLE": ("📝", "Subtitle"),
    "OTHER": ("❓", "Other"),
}

# --- Button-staleness gate -------------------------------------------------

BTN_TTL_SECONDS = 6 * 3600  # 6h before a button-bearing message's buttons expire


# --- Clean exit -------------------------------------------------------------

def schedule_clean_exit(delay_s: float = 2.0) -> None:
    """Send SIGTERM to self after `delay_s` so PTB's run_polling and aiohttp's
    runner unwind cleanly (closing httpx clients, DB connections, the HTTP
    server). Falls back to os._exit only if the SIGTERM dispatch itself fails.
    """
    loop = asyncio.get_running_loop()
    def _kill():
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception:
            logger.exception("SIGTERM dispatch failed; falling back to os._exit")
            os._exit(0)
    loop.call_later(delay_s, _kill)


# --- Formatting -------------------------------------------------------------

# Module-level set of timestamps that have already produced a parse-fail
# warning. Prevents log spam if Seerr starts emitting a non-ISO format
# while still surfacing the change on the first occurrence so we notice.
_FORMAT_AGE_WARNED: set[str] = set()


def format_age(created_at_iso: str) -> str:
    try:
        created = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
    except ValueError:
        # Log once per unparseable prefix so a Seerr format change isn't
        # silently swallowed by `return "?"`.
        key = (created_at_iso or "")[:20]
        if key not in _FORMAT_AGE_WARNED:
            _FORMAT_AGE_WARNED.add(key)
            logger.warning(
                "format_age: couldn't parse timestamp %r as ISO 8601; "
                "returning '?'. Has Seerr's payload format changed?",
                created_at_iso,
            )
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


def format_status(summary: dict[str, str]) -> str:
    return "\n".join(f"  • *{k}*: {v}" for k, v in summary.items())


def format_media_label(
    title: str, year: str, *,
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> str:
    """Single canonical media label across the bot.

    Examples:
      Inception                            -> "Inception"
      Inception (2010)                     -> "Inception (2010)"
      Mating Season (2026) — S01           -> "Mating Season (2026) — S01"
      Mating Season (2026) — S01E08        -> "Mating Season (2026) — S01E08"

    Used by /tickets list, ticket-detail header, /issue summary, and the
    auto-fix completion / abandoned DMs so the format never drifts.
    """
    base = title or "(unknown)"
    if year:
        base = f"{base} ({year})"
    if season:
        s = int(season)
        if episode:
            base += f" — S{s:02d}E{int(episode):02d}"
        else:
            base += f" — S{s:02d}"
    return base


def format_se_suffix(problem_season, problem_episode) -> str:
    """Render `S01E02` / `S01` from the Seerr webhook problemSeason+problemEpisode
    fields, tolerating None / string / int variants. Returns empty string if
    no season is set."""
    if problem_season is None:
        return ""
    try:
        s = int(problem_season)
        e = int(problem_episode) if problem_episode not in (None, "") else None
    except (TypeError, ValueError):
        return ""
    return f"S{s:02d}E{e:02d}" if e else f"S{s:02d}"


async def format_media_title_line(
    seerr: Optional[SeerrClient],
    media: dict,
    *,
    problem_season=None,
    problem_episode=None,
) -> str:
    """Build "🎬 Movie Title (Year)" or "📺 Show Title (Year) — S01E02" from a
    Seerr webhook payload's media block. Returns "" if seerr is unavailable
    or the lookup fails. Caller is responsible for HTML-escaping when emitting.
    """
    if seerr is None:
        return ""
    media_type = media.get("media_type") or ""
    tmdb_raw = media.get("tmdbId")
    try:
        tmdb_id = int(tmdb_raw) if tmdb_raw not in (None, "") else 0
    except (TypeError, ValueError):
        tmdb_id = 0
    if media_type not in ("movie", "tv") or not tmdb_id:
        return ""
    try:
        title, year = await seerr.get_media_title(media_type, tmdb_id)
    except Exception:
        logger.exception("Failed to fetch media title (type=%s tmdb=%d)",
                         media_type, tmdb_id)
        return ""
    emoji = "🎬" if media_type == "movie" else "📺"
    line = f"{emoji} {title}"
    if year:
        line += f" ({year})"
    se = format_se_suffix(problem_season, problem_episode)
    if se:
        line += f" — {se}"
    return line


# --- Seerr-required gate ----------------------------------------------------

async def require_seerr(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Optional[SeerrClient]:
    """Bail out gracefully if Seerr isn't configured yet."""
    seerr: Optional[SeerrClient] = ctx.bot_data.get("seerr")
    if seerr is None:
        port = ctx.bot_data.get("http_port", 8765)
        await update.effective_message.reply_text(
            f"Hermes isn't configured yet. The admin needs to fill in Seerr settings at "
            f"http://<host>:{port}/admin",
        )
    return seerr


# --- Telegram edit/send helper ---------------------------------------------

async def edit_or_send(q, text: str, **kwargs) -> None:
    """Edit the callback's message; if Telegram rejects (e.g., the user edited
    or deleted the source message), send a new message in the same chat so the
    response isn't silently dropped."""
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


# --- Per-user identity / Plex token resolution -----------------------------

async def token_for(
    ctx: ContextTypes.DEFAULT_TYPE, tg_id: int
) -> tuple[bool, Optional[str], bool]:
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


# --- Button bookkeeping -----------------------------------------------------
# How many recent button-bearing messages per user the gate will admit. Three
# is enough to cover a rapid-fire webhook burst (new-issue + comment + resolve)
# without letting truly-old messages stay live.
BTN_HISTORY_MAX = 3


def record_btn(app, user_id: int, message) -> None:
    """Record `message` as a button-bearing bot message for `user_id`. The
    global button gate admits callbacks whose source message matches any of
    the last BTN_HISTORY_MAX entries (FIFO eviction)."""
    if message is None:
        return
    history: dict = app.bot_data.setdefault("btn_msgs", {})
    entry = {
        "chat_id": message.chat_id,
        "message_id": message.message_id,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    user_entries: list = history.setdefault(user_id, [])
    user_entries.append(entry)
    while len(user_entries) > BTN_HISTORY_MAX:
        user_entries.pop(0)


async def global_btn_gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """TypeHandler at group=-1. Runs before any callback handler. The gate
    snapshots the per-user button history once at entry, then decides — so a
    concurrent webhook that appends a fresh entry mid-await can't shift the
    decision out from under us. A callback is admitted iff its source message
    matches one of the recent BTN_HISTORY_MAX entries AND is younger than
    BTN_TTL_SECONDS.
    """
    q = update.callback_query
    if q is None or q.message is None or q.from_user is None:
        return
    user_id = q.from_user.id
    # Snapshot via list copy so concurrent record_btn calls don't mutate
    # the iterable we're inspecting.
    entries = list(ctx.application.bot_data.get("btn_msgs", {}).get(user_id, []))
    if not entries:
        return  # no record yet -- allow (gradual rollout)

    msg_id = q.message.message_id
    now = datetime.now(timezone.utc)
    for e in entries:
        if e.get("message_id") != msg_id:
            continue
        try:
            sent = datetime.fromisoformat(e["sent_at"])
        except (KeyError, ValueError):
            continue
        if (now - sent).total_seconds() <= BTN_TTL_SECONDS:
            return  # this callback's source message is still live

    # No matching live entry. Determine the most likely reason for the
    # toast: stale (message wasn't the most recent) vs. expired (it was,
    # but past the TTL).
    latest = entries[-1]
    if latest.get("message_id") == msg_id:
        reason = "This menu has expired. Run the command again."
    else:
        reason = "Use the most recent message — this menu is from an older one."
    try:
        await q.answer(reason, show_alert=False)
    except Exception:
        pass
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    raise ApplicationHandlerStop


# --- "Most recent command wins" flow reset ---------------------------------

# Commands that must NOT abandon an in-progress flow. /cancel is handled by each
# conversation's own fallback (which sends a "Cancelled." reply), so the gate
# has to leave the conversation alive for that fallback to run.
FLOW_RESET_EXEMPT_COMMANDS: Final = frozenset({"cancel"})


def command_name(msg) -> Optional[str]:
    """Return the bot-command at the start of `msg` (lowercased, without the
    leading slash or a trailing @botname), or None if it isn't a command."""
    if msg is None or not msg.text:
        return None
    for ent in (msg.entities or ()):
        if ent.type == MessageEntity.BOT_COMMAND and ent.offset == 0:
            return msg.text[1:ent.length].split("@", 1)[0].lower()
    return None


async def reset_stale_flows(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """TypeHandler at group=-2: "most recent command wins".

    When the user sends a top-level command, abandon any in-progress
    conversation flow so a half-finished /issue (etc.) can't later intercept
    free text meant for the new flow. The conversation is ended here -- at
    command time, before any colliding message arrives -- so nothing the user
    types next gets swallowed. Mirrors the button gate's "newest wins".

    Uses ConversationHandler internals (_get_key / _update_state / timeout_jobs)
    because PTB exposes no public API to end another handler's conversation.
    """
    cmd = command_name(update.effective_message)
    if cmd is None or cmd in FLOW_RESET_EXEMPT_COMMANDS:
        return
    for conv in ctx.application.bot_data.get("flow_convs", ()):
        try:
            key = conv._get_key(update)
        except Exception:
            continue
        if key in conv._conversations:
            conv._update_state(ConversationHandler.END, key)
            job = conv.timeout_jobs.pop(key, None)
            if job is not None:
                job.schedule_removal()
    # Drop free-text flow markers so a pending ticket reply / close-comment is
    # abandoned too (its conversation was just ended above).
    for marker in ("tk_reply_id", "tk_close_after"):
        ctx.user_data.pop(marker, None)


# --- Ticket detail keyboard ------------------------------------------------

def ticket_detail_kb(issue_id: int, is_admin: bool) -> InlineKeyboardMarkup:
    """Top-level row for a ticket's detail view. Reply goes straight to the
    reply input (no submenu); only Close and Fix have submenus."""
    # Local import avoids a circular dep at module load (callback_prefixes is
    # a leaf module so this is cheap).
    from bot.callback_prefixes import TK_CLOSE, TK_FIX, TK_REPLY
    row = [InlineKeyboardButton("💬 Reply", callback_data=f"{TK_REPLY}:{issue_id}")]
    if is_admin:
        row.append(InlineKeyboardButton("🔧 Fix", callback_data=f"{TK_FIX}:{issue_id}"))
        row.append(InlineKeyboardButton("✅ Close", callback_data=f"{TK_CLOSE}:{issue_id}"))
    return InlineKeyboardMarkup([row])


# --- Underscore-prefix aliases for backwards-compatible internal callers ---
# The extracted handler modules use the original `_foo` names. New external
# call sites should prefer the unprefixed names.
_schedule_clean_exit = schedule_clean_exit
_format_age = format_age
_format_status = format_status
_require_seerr = require_seerr
_edit_or_send = edit_or_send
_token_for = token_for
_record_btn = record_btn
_global_btn_gate = global_btn_gate
_reset_stale_flows = reset_stale_flows
_ticket_detail_kb = ticket_detail_kb
_format_media_title_line = format_media_title_line
_format_se_suffix = format_se_suffix
_format_media_label = format_media_label
