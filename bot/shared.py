"""Cross-module helpers, constants, and conversation-state values.

Lives at the bottom of the package's import graph so any other bot.*
module can pull from it without circular imports.
"""
from __future__ import annotations

import contextlib
import functools
import html
import logging
import re
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Callable, Final, Optional

import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes, ConversationHandler

from seerr import SeerrClient
from store import UserStore

from procutil import schedule_clean_exit  # noqa: F401  (re-export for bot.*)

from bot.callback_prefixes import (
    ACCESS_APPROVE,
    ASK_ADMIN,
    ACCESS_IGNORE,
    DIGEST_INFO,
    RELINK,
    RQ_ADMIN_APPROVE,
    RQ_ADMIN_DECLINE,
    RQ_ADMIN_RETRY,
    RQ_INFO_DISMISS,
    DV_SIMILAR,
    SUBSCRIPTION_ADD,
    FOLLOW_ADD,
    RQ_REMAINING,
    FOLLOW_DEL,
    TK_CLOSE,
    TK_FIX,
    TK_OPEN,
    TK_REPLY,
)
from const import RELINK_RESUME_TTL_S

logger = logging.getLogger("usher")

# Conversation states

# Ticket creation
TITLE, PICK_MEDIA, PICK_SEASON, PICK_EPISODE, PICK_TYPE, DESCRIPTION, OFFER_AUTOFIX, CONFIRM_AUTOFIX = range(8)
# Post-completion follow-up
AWAIT_COMMENT = 100
# Plex link flow
AWAIT_LINK_CONSENT = 200
AWAIT_PLATFORM_CHOICE = 201
# Media requesting (/request)
RQ_TITLE = 300
RQ_PICK_MEDIA = 301
RQ_PICK_SEASONS = 302
RQ_CONFIRM = 303
RQ_PICK_USER = 304
# Ticket management (reply to existing ticket)
AWAIT_TICKET_REPLY = 400
# Admin provisioning (/invite)
INV_EMAIL = 500
INV_LIBS = 501
INV_CONFIRM = 502
# Admin Broadcast
BC_TEXT = 510
BC_CONFIRM = 511

# Issue type maps

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
    "SUBTITLES": ("📝", "Subtitles"),
    "SUBTITLE": ("📝", "Subtitles"),
    "OTHER": ("❓", "Other"),
}

# Button-staleness gate

BTN_TTL_SECONDS = 6 * 3600  # 6h before a button-bearing message's buttons expire


# schedule_clean_exit moved to the root procutil module (it was
# a verbatim copy between here and webui.py). Re-exported for bot.* callers.


# Formatting

# Module-level set of timestamps that have already produced a parse-fail
# warning. Prevents log spam if Seerr starts emitting a non-ISO format
# while still surfacing the change on the first occurrence so we notice.
_FORMAT_AGE_WARNED: set[str] = set()


def format_age(created_at_iso: str) -> str:
    try:
        created = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
    # TypeError/AttributeError: a JSON-null timestamp arriving as None must
    # degrade to "?" like any other unparseable value, not abort the render.
    except (ValueError, TypeError, AttributeError):
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
    """HTML-formatted status block. Emits <b> tags and escapes the values
    (which carry upstream error text); callers send it with parse_mode=HTML
    and must NOT re-escape it (that would show the tags literally)."""
    return "\n".join(
        f"  • <b>{html.escape(k)}</b>: {html.escape(v)}"
        for k, v in summary.items()
    )


def format_media_label(
    title: str, year: str, *,
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> str:
    """Single canonical media label across the bot.

    Examples:
      Inception                            -> "Inception"
      Inception (2010)                     -> "Inception (2010)"
      Example Show (2026) - S01           -> "Example Show (2026) - S01"
      Example Show (2026) - S01E08        -> "Example Show (2026) - S01E08"

    Used by /tickets list, ticket-detail header, /issue summary, and the
    auto-fix completion / abandoned DMs so the format never drifts.
    """
    base = title or "(unknown)"
    if year:
        base = f"{base} ({year})"
    # Season 0 is ambiguous: with an episode it's a specials episode
    # (S00Exx, render it); WITHOUT one it's Seerr's "all seasons" scope and
    # must render nothing extra. A truthiness test alone dropped picked
    # specials episodes from confirmations and ticket lists.
    if season is not None and episode:
        base += f" - S{int(season):02d}E{int(episode):02d}"
    elif season:
        base += f" - S{int(season):02d}"
    return base


def day_count(n: int) -> str:
    """"1 day", "7 days"."""
    return f"{n} day{'' if n == 1 else 's'}"


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


def _se_to_int(v):
    """Coerce a season/episode value (int / str / None / "") to int, or None."""
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# One-time guard so we log the raw webhook `extra` shape exactly once per
# process -- enough to verify the affected-season/episode field names against a
# live Seerr payload without spamming the log on every webhook.
_EXTRA_SHAPE_LOGGED = False


def extract_affected_se(payload: dict):
    """Pull the affected (season, episode) as ints from a Seerr webhook payload.

    Seerr's default webhook delivers these in the top-level `extra` array as
    `{"name": "Affected Season", "value": "1"}` / `"Affected Episode"`; a custom
    webhook template may instead set problemSeason/problemEpisode on the ticket
    object. Tolerant of both; returns (None, None) when neither is present.
    """
    global _EXTRA_SHAPE_LOGGED
    ticket = payload.get("issue") or {}
    season = ticket.get("problemSeason")
    episode = ticket.get("problemEpisode")
    extra = payload.get("extra") or []
    for item in extra:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").lower()
        if "season" in name and season in (None, ""):
            season = item.get("value")
        elif "episode" in name and episode in (None, ""):
            episode = item.get("value")
    if not _EXTRA_SHAPE_LOGGED and (ticket.get("media") or payload.get("media")):
        _EXTRA_SHAPE_LOGGED = True
        logger.info(
            "Seerr webhook `extra` shape (first seen): %r; parsed season=%r episode=%r",
            [i.get("name") for i in extra if isinstance(i, dict)], season, episode,
        )
    return _se_to_int(season), _se_to_int(episode)


def format_scope_label(media_type, season, episode) -> str:
    """Human-readable affected scope for a TV ticket:
    'Season 1, Episode 5' / 'Season 1' / 'All seasons'. Empty for movies.

    Seerr uses season 0 (and absence) to mean a whole-series / all-seasons
    ticket, so a falsy season renders as 'All seasons'.

    Only safe when the caller KNOWS the scope was carried in the payload (the
    ISSUE_CREATED webhook). For follow-up events, where absence means "not
    sent" rather than "whole series", use followup_scope_label instead."""
    if (media_type or "").lower() != "tv":
        return ""
    if not season:
        return "All seasons"
    if episode:
        return f"Season {season}, Episode {episode}"
    return f"Season {season}"


def coerce_tmdb_id(raw) -> Optional[int]:
    """A TMDb id from a Seerr webhook value, or None. Seerr's default
    template sends every value as a string ("550"), so accept ints and
    digit strings; "", junk, bools and non-positive numbers give None."""
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value > 0 else None


async def followup_scope_label(seerr, payload: dict, ticket_id, media_type) -> str:
    """Scope line for follow-up events (ISSUE_COMMENT / ISSUE_RESOLVED).

    Seerr only puts `Affected Season`/`Affected Episode` in the `extra` array
    of the ISSUE_CREATED webhook. Follow-up payloads carry neither those nor
    problemSeason/problemEpisode, so absence there means "unknown", NOT the
    whole series -- feeding it to format_scope_label told the reporter their
    single-episode ticket was "All seasons" (the create DM said
    "Season 6, Episode 9", the comment DM contradicted it).

    So: use the payload when it does carry a scope, otherwise ask the REST API
    for the ticket's real scope, and render nothing at all when it still can't
    be determined. Never guess -- a missing line beats a wrong one.
    """
    if (media_type or "").lower() != "tv":
        return ""
    season, episode = extract_affected_se(payload)
    if season is None and episode is None and seerr is not None:
        try:
            ticket = await seerr.get_issue(ticket_id)
        except Exception:
            logger.info(
                "Couldn't fetch ticket #%s for its affected scope; omitting the "
                "scope line rather than guessing", ticket_id, exc_info=True,
            )
        else:
            season = _se_to_int(getattr(ticket, "problem_season", None))
            episode = _se_to_int(getattr(ticket, "problem_episode", None))
    if season is None and episode is None:
        # Genuinely unknown (no payload fields, no/failed lookup). A
        # whole-series ticket comes back as season 0, which is not None and
        # still renders "All seasons" below.
        return ""
    return format_scope_label(media_type, season, episode)


async def watch_now_button(seerr, media_type: str,
                           tmdb_id: int) -> Optional[InlineKeyboardButton]:
    """The "Watch now" URL button for a title in Plex, or None when Seerr
    has no link yet or the lookup fails. Shared by every availability
    notice."""
    if seerr is None or media_type not in ("movie", "tv") or not tmdb_id:
        return None
    try:
        url = await seerr.get_media_url(media_type, tmdb_id)
    except Exception:
        logger.warning("Watch now lookup failed (type=%s tmdb=%s)",
                       media_type, tmdb_id, exc_info=True)
        return None
    return InlineKeyboardButton("▶️ Watch now", url=url) if url else None


async def format_media_title_line(
    seerr: Optional[SeerrClient],
    media: dict,
    *,
    problem_season=None,
    problem_episode=None,
) -> str:
    """Build "🎬 Movie Title (Year)" or "📺 Show Title (Year) - S01E02" from a
    Seerr webhook payload's media block. Returns "" if seerr is unavailable
    or the lookup fails. Caller is responsible for HTML-escaping when emitting.
    """
    if seerr is None:
        return ""
    media_type = media.get("media_type") or ""
    tmdb_id = coerce_tmdb_id(media.get("tmdbId")) or 0
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
        line += f" - {se}"
    return line


# Seerr-required gate

async def require_seerr(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                        *, reply_markup=None) -> Optional[SeerrClient]:
    """Bail out gracefully if Seerr isn't configured yet. The admin gets the
    settings URL hint; regular users get a next step they can actually take
    (a placeholder URL and port number are useless to them). A flow started
    from a Hub passes `reply_markup` (its back button); the sent message is
    then recorded with the button gate."""
    seerr: Optional[SeerrClient] = ctx.bot_data.get("seerr")
    if seerr is None:
        if update.effective_user.id == ctx.bot_data.get("admin_id"):
            port = ctx.bot_data.get("http_port", 8765)
            text = (f"Usher isn't configured yet. Fill in Seerr settings at "
                    f"http://<host>:{port}/admin")
        else:
            text = "Usher isn't set up yet: ask the admin to finish configuration."
        if reply_markup is None:
            await update.effective_message.reply_text(text)
        else:
            sent = await update.effective_message.reply_text(
                text, reply_markup=reply_markup)
            record_btn(ctx.application, update.effective_user.id, sent)
    return seerr


# Telegram edit/send helper

async def edit_or_send(q, text: str, **kwargs):
    """Edit the callback's message; if Telegram rejects (e.g., the user edited
    or deleted the source message), send a new message in the same chat so the
    response isn't silently dropped. Returns the resulting Message (edited or
    newly sent) so callers can track it (e.g. as a Progress card),
    or None when both paths failed."""
    try:
        return await q.edit_message_text(text, **kwargs)
    except telegram.error.BadRequest as exc:
        # A double tap re-renders identical text; Telegram refuses that
        # edit, and the message already shows what we wanted.
        if "not modified" in str(exc).lower():
            return q.message
    except Exception as exc:
        logger.exception("edit_message_text failed unexpectedly; falling back to send", exc_info=exc)
    try:
        return await q.message.reply_text(text, **kwargs)
    except Exception:
        logger.exception("reply_text fallback also failed")
        return None


# Per-user identity / Plex token resolution

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
    link = await store.get(tg_id)
    if link is None:
        return False, None, False
    if link.plex_token_decrypt_failed:
        return False, None, True
    if not link.plex_token:
        return False, None, False
    return False, link.plex_token, False


# Button bookkeeping
# How many recent button-bearing messages per user the gate will admit. Three
# is enough to cover a rapid-fire webhook burst (new-ticket + comment + resolve)
# without letting truly-old messages stay live.
BTN_HISTORY_MAX = 3


# How often the btn_msgs map is swept for users whose tracked buttons have
# all expired. Without the sweep the map gains a permanent per-user entry for
# every account the bot ever sent a keyboard to (any stranger can trigger
# that via /link), growing unbounded on attacker-controlled keys.
_BTN_PRUNE_INTERVAL_S = 3600


def _prune_btn_history(app, history: dict) -> None:
    now = datetime.now(timezone.utc)
    last = app.bot_data.get("_btn_msgs_pruned_at")
    if last is not None and (now - last).total_seconds() < _BTN_PRUNE_INTERVAL_S:
        return
    app.bot_data["_btn_msgs_pruned_at"] = now
    for uid in list(history):
        fresh = []
        for e in history[uid]:
            try:
                sent = datetime.fromisoformat(e["sent_at"])
            except (KeyError, TypeError, ValueError):
                continue  # unparseable entry: drop it
            if (now - sent).total_seconds() <= BTN_TTL_SECONDS:
                fresh.append(e)
        if fresh:
            history[uid] = fresh
        else:
            history.pop(uid, None)


def record_btn(app, user_id: int, message) -> None:
    """Record `message` as a button-bearing bot message for `user_id`. The
    global button gate admits callbacks whose source message matches any of
    the last BTN_HISTORY_MAX entries (FIFO eviction).

    `message` may be whatever the send/edit call returned: anything without
    a message_id (None, or the bare `True` an inline-message edit returns)
    is ignored. Re-recording an already-tracked message (a flow step that
    edits its menu in place) refreshes its timestamp and moves it to the
    newest slot instead of duplicating it, so a long multi-step flow can't
    evict itself out of its own history."""
    if message is None or getattr(message, "message_id", None) is None:
        return
    history: dict = app.bot_data.setdefault("btn_msgs", {})
    _prune_btn_history(app, history)
    entry = {
        "chat_id": message.chat_id,
        "message_id": message.message_id,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    user_entries: list = history.setdefault(user_id, [])
    user_entries[:] = [
        e for e in user_entries
        if (e.get("chat_id"), e.get("message_id"))
        != (entry["chat_id"], entry["message_id"])
    ]
    user_entries.append(entry)
    while len(user_entries) > BTN_HISTORY_MAX:
        user_entries.pop(0)


def record_card_owner(app, owner_id, message) -> None:
    """Remember who a detail card in a group chat was sent for. Cards stay out
    of the button history (recording them would evict the live pick list), so
    the group tap check reads this map instead. Only group cards are kept, and
    entries older than BTN_TTL_SECONDS are dropped as new ones arrive."""
    chat_id = getattr(message, "chat_id", None)
    msg_id = getattr(message, "message_id", None)
    if (app is None or owner_id is None or msg_id is None
            or not isinstance(chat_id, int) or chat_id >= 0):
        return
    owners: dict = app.bot_data.setdefault("card_owners", {})
    now = datetime.now(timezone.utc)
    for key in [k for k, (_, at) in owners.items()
                if (now - at).total_seconds() > BTN_TTL_SECONDS]:
        del owners[key]
    owners[(chat_id, msg_id)] = (owner_id, now)


# Relink-resume executors, keyed by the resume kind stashed at each gated
# surface. Populated at import time by ticket_flow / resolve_flow / tickets so
# link_flow can dispatch without importing them (avoids import cycles).
# Signature: async (update, ctx, payload: dict) -> None.
RELINK_RESUME_EXECUTORS: dict[str, Callable] = {}


# One canonical decrypt-failed message (it was pasted six times
# with wording drift across tickets.py / resolve_flow.py / ticket_flow.py).
DECRYPT_FAILED_MSG = (
    "Your Plex link can't be decrypted (the encryption key may have "
    "rotated). Run /unlink then /link to reconnect."
)


async def send_typing(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Best-effort typing indicator before a slow network read (ticket
    list / title search / submit otherwise look frozen). Never fatal."""
    try:
        await ctx.bot.send_chat_action(chat_id=update.effective_chat.id,
                                       action="typing")
    except Exception as exc:
        logger.debug("send_chat_action failed", exc_info=exc)


# Per-media action serialization

def media_action_key(media: dict) -> str:
    """Serialization key for destructive arr workflows on one title."""
    return f"{media.get('type')}:{media.get('tmdb_id')}"


def try_begin_action(ctx: ContextTypes.DEFAULT_TYPE, key: str) -> bool:
    """Claim the in-flight slot for key; False if already claimed. Safe under
    concurrent_updates: one event loop, and no await between check and add."""
    inflight: set = ctx.bot_data.setdefault("inflight_actions", set())
    if key in inflight:
        return False
    inflight.add(key)
    return True


def end_action(ctx: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    ctx.bot_data.get("inflight_actions", set()).discard(key)


# Cross-conversation text-capture guard

def user_in_conversation(ctx: ContextTypes.DEFAULT_TYPE, update: Update,
                         *names: str) -> bool:
    """True if any of the named conversations (bot_data["conversations"],
    registered in app.py) is active for this chat+user. Reads PTB's private
    _conversations dict, keyed (chat_id, user_id) for per_chat+per_user
    handlers -- tests pin that shape so a PTB upgrade that changes it fails
    loudly instead of silently letting text capture cross flows again."""
    convs = ctx.bot_data.get("conversations") or {}
    key = (update.effective_chat.id, update.effective_user.id)
    for name in names:
        conv = convs.get(name)
        if conv is None:
            continue
        if getattr(conv, "_conversations", {}).get(key) is not None:
            return True
    return False


# Expired-keyboard catch-all

async def unmatched_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Terminal catch-all for callback taps nothing else claimed: a keyboard
    outlives its 10-minute conversation by hours in chat history, and a tap
    on it previously hung the Telegram spinner with no feedback at all.
    Registered LAST in the default group, so it only fires when every real
    handler (including active conversations) has passed on the update."""
    q = update.callback_query
    if q is None:
        return
    try:
        await q.answer("That menu has expired. Run the command again.")
    except Exception as exc:
        logger.debug("expired-callback answer failed", exc_info=exc)
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        # Message may be too old to edit or already edited; the toast above
        # is the load-bearing part.
        logger.debug("expired-callback keyboard strip failed", exc_info=True)


async def prompt_plex_relink(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *,
                             resume_kind: Optional[str] = None,
                             resume_payload: Optional[dict] = None,
                             back: Optional[InlineKeyboardButton] = None) -> None:
    """Recovery prompt for a revoked Plex token (PlexTokenInvalidError): one
    tap drops the user into the sign-in flow
    (link_flow.cmd_relink). Works from both callback and message contexts.
    `back` (a flow started from a Hub) goes on its own row below the relink
    button.

    resume_kind/resume_payload (optional) stash what the user was doing so
    _finalize_link can pick it back up after the re-link succeeds. NEVER put
    a token in the payload; executors re-fetch it at execution time."""
    if resume_kind is not None:
        ctx.user_data["relink_resume"] = {
            "kind": resume_kind,
            "payload": resume_payload or {},
            "saved_at": time.time(),
        }
    rows = [[InlineKeyboardButton("🔗 Sign in to Plex again",
                                  callback_data=RELINK)]]
    if back is not None:
        rows.append([back])
    kb = InlineKeyboardMarkup(rows)
    text = (
        "⚠️ Plex says your sign-in is no longer valid. This usually happens "
        "after a password change or signing out of all devices.\n\n"
        "Tap below to sign back in - takes about a minute. Your Link stays "
        "as it is until the new sign-in succeeds."
    )
    if resume_kind is not None:
        text += "\n\nI'll pick up where you left off once you're signed back in."
    if update.callback_query:
        sent = await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        sent = await update.effective_message.reply_text(text, reply_markup=kb)
    record_btn(ctx.application, update.effective_user.id, sent)


# Command that restarts the action behind each relink-resume marker kind.
_RELINK_RESUME_COMMANDS: Final = {
    "submit_request": "/request",
    "submit_ticket": "/issue",
    "ticket_reply": "/tickets",
    "resolve_close": "/tickets",
    "resolve_comment": "/tickets",
}


async def run_relink_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Execute a stashed relink-resume marker if one is fresh. Called by
    _finalize_link after a successful (re-)link. The marker is popped BEFORE
    running so a failing executor can't fire twice. Returns True if an
    action was resumed."""
    marker = ctx.user_data.pop("relink_resume", None)
    if not marker:
        return False
    if time.time() - marker.get("saved_at", 0) > RELINK_RESUME_TTL_S:
        # The prompt promised "I'll pick up where you left off"; when the
        # marker aged out, honesty beats silence.
        await update.effective_message.reply_text(
            "That was a while ago, so I didn't repeat your earlier action, "
            f"start it again with {_RELINK_RESUME_COMMANDS.get(marker.get('kind'), '/help')} "
            "if you still need it."
        )
        return False
    executor = RELINK_RESUME_EXECUTORS.get(marker.get("kind"))
    if executor is None:
        return False
    await update.effective_message.reply_text("▶️ Picking up where you left off...")
    await executor(update, ctx, marker.get("payload") or {})
    return True


_GATE_EXEMPT_PREFIXES: Final = frozenset({
    RQ_INFO_DISMISS, DV_SIMILAR, SUBSCRIPTION_ADD, FOLLOW_ADD, FOLLOW_DEL,
    DIGEST_INFO,
    RQ_REMAINING, RQ_ADMIN_APPROVE, RQ_ADMIN_DECLINE, RQ_ADMIN_RETRY,
    ACCESS_APPROVE, ACCESS_IGNORE, ASK_ADMIN,
})


def _is_group_message(message) -> bool:
    chat = getattr(message, "chat", None)
    return getattr(chat, "type", None) in ("group", "supergroup")


def _group_tap_is_owner(ctx, message, user_id: int) -> bool:
    """Whether `user_id` is the person a group-chat bot message was sent to.
    A bot reply carries the message it answers, so a reply to a human's
    command or text belongs to that human. A message with no such reply
    (the command was deleted, or the bot sent it unprompted) belongs to
    whoever has it in their own recorded button history."""
    replied = getattr(message, "reply_to_message", None)
    author = getattr(replied, "from_user", None)
    if author is not None and not getattr(author, "is_bot", False):
        return author.id == user_id
    msg_id = getattr(message, "message_id", None)
    chat_id = getattr(message, "chat_id", None)
    now = datetime.now(timezone.utc)
    card = ctx.application.bot_data.get("card_owners", {}).get((chat_id, msg_id))
    if card is not None and card[0] == user_id:
        return (now - card[1]).total_seconds() <= BTN_TTL_SECONDS
    for e in ctx.application.bot_data.get("btn_msgs", {}).get(user_id, []):
        if e.get("message_id") != msg_id or e.get("chat_id") != chat_id:
            continue
        try:
            sent = datetime.fromisoformat(e["sent_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if (now - sent).total_seconds() <= BTN_TTL_SECONDS:
            return True
    return False


async def global_btn_gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """TypeHandler at group=-1. Runs before any callback handler. The gate
    snapshots the per-user button history once at entry, then decides, so a
    concurrent webhook that appends a fresh entry mid-await can't shift the
    decision out from under us. A callback is admitted iff its source message
    matches one of the recent BTN_HISTORY_MAX entries AND is younger than
    BTN_TTL_SECONDS.
    """
    q = update.callback_query
    if q is None or q.message is None or q.from_user is None:
        return
    # In a group the owner check below still applies to these. In a DM,
    # callbacks are exempt from the history check because their source
    # message is not in the history or may be old, and the action is safe
    # to run anyway:
    #  - Dismiss and Notify-me sit on detail cards, which are deliberately NOT
    #    recorded (recording them would evict the live pick list after three
    #    taps and brick the /request flow). Dismiss only deletes the tapped
    #    card; Notify-me only writes a row for the tapper.
    #  - Follow and Unfollow sit on detail cards, availability notices and
    #    Follow pings; they only write the tapper's own Follow row.
    #  - Request remaining seasons sits on detail cards and availability
    #    notices; it only opens a confirm message (recorded, with its own
    #    Send) and files nothing itself.
    #  - A Digest entry's detail-card button: read-only, and a Digest DM is
    #    not recorded (it can be a day old when tapped).
    #  - Approve/Decline/Retry sit on the Admin's Request DMs. They are
    #    Admin-only and re-read the Request's status from Seerr before acting,
    #    and there is no way to bring the DM back in Telegram once it expires.
    #  - Approve/Ignore on an Access ask DM: Admin-only, and each ask is acted
    #    on once (the ask's status is re-read before anything happens).
    #  - More like this sits on detail cards, which are not in the history. It
    #    is read-only: it only sends a new list.
    #  - Ask the Admin sits on a Reporter's repeat DM, which can be hours
    #    old when tapped and cannot be brought back. The handler checks the
    #    tapped message carries that button, and each Ticket is asked once.
    data = getattr(q, "data", None)
    user_id = q.from_user.id
    in_group = _is_group_message(q.message)
    if in_group and not _group_tap_is_owner(ctx, q.message, user_id):
        # Another member's tap on a menu sent to someone else: nothing runs
        # and the message stays as it is. This covers the exempt prefixes
        # too, since their handlers would otherwise act as the tapper on a
        # card in someone else's menu.
        try:
            await q.answer("This menu isn't yours.", show_alert=False)
        except telegram.error.TelegramError as exc:
            logger.debug("callback answer failed", exc_info=exc)
        raise ApplicationHandlerStop
    if isinstance(data, str) and data.split(":", 1)[0] in _GATE_EXEMPT_PREFIXES:
        return
    # Snapshot via list copy so concurrent record_btn calls don't mutate
    # the iterable we're inspecting.
    entries = list(ctx.application.bot_data.get("btn_msgs", {}).get(user_id, []))
    if not entries:
        return  # no record yet (new user, or since a restart) -- allow

    msg_id = q.message.message_id
    chat_id = getattr(q.message, "chat_id", None)
    now = datetime.now(timezone.utc)
    for e in entries:
        if e.get("message_id") != msg_id:
            continue
        # Message ids are only unique per chat; an entry that recorded its
        # chat must match it too. An entry with no chat_id matches on id alone.
        if e.get("chat_id") is not None and e.get("chat_id") != chat_id:
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
        reason = "Use the most recent message - this menu is from an older one."
    try:
        await q.answer(reason, show_alert=False)
    except telegram.error.TelegramError as exc:
        logger.debug("callback answer failed", exc_info=exc)
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except telegram.error.TelegramError as exc:
        logger.debug("edit_message_reply_markup failed", exc_info=exc)
    raise ApplicationHandlerStop


# "Most recent command wins" flow reset

# Commands that must NOT abandon an in-progress flow. /cancel is handled by each
# conversation's own fallback (which sends a "Cancelled." reply), so the gate
# has to leave the conversation alive for that fallback to run.
FLOW_RESET_EXEMPT_COMMANDS: Final = frozenset({"cancel"})

# Flow registry names (bot_data["conversations"]) that are started by a
# command of the same name; the others start from buttons.
_FLOW_COMMANDS: Final = frozenset({"link", "issue", "request", "invite"})


def command_name(msg) -> Optional[str]:
    """Return the bot-command at the start of `msg` (lowercased, without the
    leading slash or a trailing @botname), or None if it isn't a command
    or is addressed to a different bot (/weather@OtherBot in a group)."""
    if msg is None or not msg.text:
        return None
    for ent in (msg.entities or ()):
        if ent.type == MessageEntity.BOT_COMMAND and ent.offset == 0:
            name, _, target = msg.text[1:ent.length].partition("@")
            if target and not _addressed_to_this_bot(msg, target):
                return None
            return name.lower()
    return None


def _addressed_to_this_bot(msg, target: str) -> bool:
    """True unless the bot's own username is known and differs."""
    try:
        own = msg.get_bot().username
    except Exception:
        return True
    return not own or own.lower() == target.lower()


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
    ended = force_end_flow_conversations(ctx, update)
    if not ended:
        return
    # Tell the user their flow ended, so the next text they type isn't met
    # with silence. Name the flow only when exactly one flow ended and it maps
    # to a command; skip the notice when the new command is that flow's own
    # command (the user is restarting it).
    names = {id(conv): name
             for name, conv in ctx.application.bot_data.get("conversations", {}).items()
             if name in _FLOW_COMMANDS}
    flows = {names.get(id(conv)) for conv in ended}
    if flows == {cmd}:
        return
    flows.discard(None)
    if len(flows) == 1 and len(ended) == 1:
        text = f"Stopped your unfinished /{next(iter(flows))}."
    else:
        text = "Stopped what you were in the middle of."
    try:
        await update.effective_message.reply_text(text)
    except telegram.error.TelegramError as exc:
        logger.debug("flow-reset notice failed", exc_info=exc)


def force_end_flow_conversations(ctx: ContextTypes.DEFAULT_TYPE,
                                 update: Update) -> list:
    """End every registered flow conversation for this update's (chat, user)
    and drop the free-text flow markers those conversations own. Shared by
    reset_stale_flows and by cross-flow jump entry points (e.g. the /issue
    dead-end's "Request it instead" button, whose tap starts the request
    conversation while /issue is still mid-state -- without this the
    abandoned /issue would fire a confusing timeout notice minutes into the
    new flow).

    The markers travel WITH the force-end, never separately: ending the link
    conversation also cancels the timeout job that would normally clear
    link_active_loop, so leaving the marker set would let an in-flight Plex
    PIN poll outlive its conversation and fire _finalize_link mid-flow.

    Returns the conversations that were actually live and got ended."""
    before = _flow_gen(ctx)
    ctx.user_data[_FLOW_GEN_KEY] = before + 1
    cell = _STEP_GEN.get()
    if cell is not None and cell[0] == before:
        # The calling step ends flows on purpose, so its own bump is not an
        # overtake. A step already overtaken earlier stays overtaken.
        cell[0] = before + 1
    ended = []
    for conv in ctx.application.bot_data.get("flow_convs", ()):
        try:
            key = conv._get_key(update)
        except Exception:
            continue
        if key in conv._conversations:
            conv._update_state(ConversationHandler.END, key)
            ended.append(conv)
            job = conv.timeout_jobs.pop(key, None)
            if job is not None:
                job.schedule_removal()
    for marker in ("tk_reply_id", "tk_close_after", "link_active_loop",
                   "link_retry_ok"):
        ctx.user_data.pop(marker, None)
    return ended


# Per-user flow generation. Force-ending a user's flows bumps it; a flow step
# remembers the value it started under (in _STEP_GEN) and, if the value moved
# while it ran, the step's returned state is dropped (guard_flow_steps) and its
# own writes can be skipped (flow_step_is_stale). Lives in user_data under a key
# no flow reset clears, like rq_search_version and inv_version, but is separate
# from them: those answer "is this button from the latest menu".
_FLOW_GEN_KEY: Final = "flow_gen"
_STEP_GEN: ContextVar[Optional[list]] = ContextVar("flow_step_gen", default=None)


def _flow_gen(ctx) -> int:
    return ctx.user_data.get(_FLOW_GEN_KEY) or 0


def flow_step_is_stale(ctx) -> bool:
    """True when the flow step running in this task started before the user's
    flows were last force-ended. Always False outside a guarded step."""
    cell = _STEP_GEN.get()
    return cell is not None and cell[0] != _flow_gen(ctx)


def guard_flow_steps(conv: ConversationHandler) -> None:
    """Wrap every step callback of `conv` so a step that was overtaken by a
    force-end returns END instead of the state it computed (PTB would store
    that state and revive the ended conversation). When the conversation
    already holds a live state again (the user restarted the flow), the
    overtaken step returns None and leaves that newer state alone. A step that itself calls
    force_end_flow_conversations (cross-flow jump entry points) is not
    overtaken by its own call. Idempotent."""
    handlers = (list(conv.entry_points) + list(conv.fallbacks)
                + [h for hs in conv.states.values() for h in hs])
    for handler in handlers:
        cb = handler.callback
        if getattr(cb, "_flow_guarded", False):
            continue
        handler.callback = _guarded_step(conv, cb)


@contextlib.contextmanager
def _step_scope(ctx):
    """Run a step under the generation the user's flows have right now."""
    cell = [_flow_gen(ctx)]
    token = _STEP_GEN.set(cell)
    try:
        yield cell
    finally:
        _STEP_GEN.reset(token)


def track_flow_generation(cb):
    """For a handler that is not a conversation step but has the same hazard
    (a write after an await that a force-end would have cleared): lets
    flow_step_is_stale work inside it. Does not change what it returns."""
    @functools.wraps(cb)
    async def step(update, ctx):
        with _step_scope(ctx):
            return await cb(update, ctx)
    return step


def _guarded_step(conv: ConversationHandler, cb):
    @functools.wraps(cb)
    async def step(update, ctx):
        with _step_scope(ctx) as cell:
            result = await cb(update, ctx)
            if cell[0] == _flow_gen(ctx):
                return result
        # Overtaken. If the conversation has a live state again, a newer step
        # (the user restarted the flow) owns it: leave it alone.
        try:
            if conv._get_key(update) in conv._conversations:
                return None
        except Exception:
            logger.debug("Overtaken step could not read its conversation state",
                         exc_info=True)
        return ConversationHandler.END
    step._flow_guarded = True
    return step


# Keycap-emoji labels for numbered pick lists (/issue and /request share the
# same picker idiom; one definition so they can't drift). Indexes past the
# keycap set fall back to plain digits at the call sites.
KEYCAP_DIGITS: Final = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣",
                        "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


_HTML_TAG_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)[^>]*>")
_TRUNCATION_NOTE = "\n…(truncated)"


def _safe_cut(text: str, cut: int) -> int:
    """Move `cut` back so text[:cut] ends neither inside a tag nor inside an
    entity such as &amp;."""
    lt = text.rfind("<", 0, cut)
    if lt != -1 and text.find(">", lt, cut) == -1:
        cut = lt
    amp = text.rfind("&", max(0, cut - 10), cut)
    if amp != -1 and text.find(";", amp, cut) == -1:
        cut = amp
    return cut


def _closing_tags(fragment: str) -> str:
    """Close tags a cut left open, innermost first."""
    stack: list[str] = []
    for m in _HTML_TAG_RE.finditer(fragment):
        if m.group(1):
            if m.group(2) in stack:
                while stack and stack.pop() != m.group(2):
                    pass
        else:
            stack.append(m.group(2))
    return "".join(f"</{t}>" for t in reversed(stack))


def truncate_message(text: str, limit: int = 4000, html: bool = True) -> str:
    """Keep a message under Telegram's 4096-char cap (an oversized send or
    edit fails silently from the user's perspective). Cuts at the last
    newline when one is near, never inside a tag or entity, then closes any
    tag the cut left open (a multi-line <i>...</i> span would otherwise make
    Telegram reject the whole message). The result, closers and note
    included, is at most `limit` chars.
    Shared by /tickets and /requests list rendering and the Ticket DMs."""
    if len(text) <= limit:
        return text
    limit = max(limit, len(_TRUNCATION_NOTE) + 1)
    budget = limit - len(_TRUNCATION_NOTE)
    if not html:
        # Plain text (no parse_mode): any '<' is literal, nothing to close.
        cut = text.rfind("\n", 0, budget)
        if cut < budget // 2:
            cut = budget
        return text[:cut] + _TRUNCATION_NOTE
    while budget > 0:
        cut = text.rfind("\n", 0, budget)
        if cut < budget // 2:
            cut = budget
        cut = _safe_cut(text, cut)
        head = text[:cut]
        closers = _closing_tags(head)
        if len(head) + len(closers) + len(_TRUNCATION_NOTE) <= limit:
            return head + closers + _TRUNCATION_NOTE
        budget -= len(closers) + 1
    # Closers alone outgrow the limit: drop all markup rather than loop.
    return re.sub(r"<[^>]*>", "", text)[:limit - len(_TRUNCATION_NOTE)] + _TRUNCATION_NOTE


# Ticket detail keyboard

def ticket_detail_kb(ticket_id: int, is_admin: bool,
                     resolved: bool = False) -> InlineKeyboardMarkup:
    """Top-level row for a ticket's detail view. Reply goes straight to the
    reply input (no submenu); only Close and Fix have submenus. A resolved
    Ticket keeps Reply only: Fix would delete the current file again,
    and there is nothing left to Close."""
    row = [InlineKeyboardButton("💬 Reply", callback_data=f"{TK_REPLY}:{ticket_id}")]
    if is_admin and not resolved:
        row.append(InlineKeyboardButton("🔧 Fix", callback_data=f"{TK_FIX}:{ticket_id}"))
        row.append(InlineKeyboardButton("✅ Close", callback_data=f"{TK_CLOSE}:{ticket_id}"))
    return InlineKeyboardMarkup([row])


def new_ticket_dm_kb(ticket_id: int, resolved: bool = False) -> InlineKeyboardMarkup:
    """The new-Ticket DM's buttons: the Admin's Ticket row, then "📜 History".
    A Fix on that card puts this back (`tickets._restore_ticket_card`) once
    the Ticket loads."""
    return InlineKeyboardMarkup([
        list(ticket_detail_kb(ticket_id, True, resolved).inline_keyboard[0]),
        [InlineKeyboardButton("📜 History", callback_data=f"{TK_OPEN}:{ticket_id}")],
    ])


# The underscore-prefix alias block (`_foo = foo`) was removed in 0.12.0
# in favor of one naming convention: the unprefixed public names.
