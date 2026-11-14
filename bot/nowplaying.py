"""Now playing on the Admin's Status: one line per Tautulli session and a Stop
button per session (at most 2 per row). Stop opens a confirm with "Stop" and
"Stop with message"; both call Tautulli's terminate_session (POST) with the
session's id, the second with DEFAULT_STOP_MESSAGE. Everything here is
Admin-only and DM-only, and absent without Tautulli linked.

A Stop button carries a short hash of the session id (Telegram caps
callback_data at 64 bytes); each tap looks the session up again in
get_activity, so a stream that ended meanwhile is never "stopped" blindly."""
from __future__ import annotations

import hashlib
import html
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from http_util import user_friendly_message

from bot.admin_hub import refuse_outside_admin_dm
from bot.callback_prefixes import ADMIN_STATUS, STOP_ASK, STOP_GO, STOP_MSG
from bot.shared import edit_or_send, record_btn

logger = logging.getLogger("usher")

DEFAULT_STOP_MESSAGE = "The server admin stopped this stream."
MAX_SESSIONS = 20
_DECISIONS = {"direct play": "direct play", "copy": "direct stream",
              "transcode": "transcode"}


def _token(session_id) -> str:
    return hashlib.sha1(str(session_id).encode("utf-8")).hexdigest()[:12]


def _who(s: dict) -> str:
    return str(s.get("friendly_name") or s.get("user") or "Someone")


def _title(s: dict) -> str:
    return str(s.get("full_title") or s.get("title") or "(unknown)")


def _decision(s: dict) -> str:
    raw = str(s.get("transcode_decision") or "").strip().lower()
    return _DECISIONS.get(raw, raw)


def _sessions(activity: dict) -> list[dict]:
    rows = activity.get("sessions") if isinstance(activity, dict) else None
    return [s for s in (rows or []) if isinstance(s, dict)
            and s.get("session_id")][:MAX_SESSIONS]


def _line(s: dict) -> str:
    detail = ", ".join(x for x in (str(s.get("player") or "").strip(),
                                   _decision(s)) if x)
    text = f"  • <b>{html.escape(_who(s))}</b>: {html.escape(_title(s))}"
    return f"{text} ({html.escape(detail)})" if detail else text


async def now_playing_section(ctx) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """The Status addendum: (HTML text starting with a blank line, Stop button
    rows). ("", []) when Tautulli is not linked; "Nothing playing" with no
    sessions; one line saying so when Tautulli can't be asked."""
    tau = ctx.bot_data.get("tautulli")
    if tau is None:
        return "", []
    try:
        sessions = _sessions(await tau.get_activity())
    except Exception as exc:
        logger.exception("Tautulli get_activity failed")
        return ("\n\n<b>Now playing:</b>\n  Couldn't ask Tautulli. "
                f"{html.escape(user_friendly_message(exc))}"), []
    if not sessions:
        return "\n\n<b>Now playing:</b>\n  Nothing playing", []
    text = "\n\n<b>Now playing:</b>\n" + "\n".join(_line(s) for s in sessions)
    buttons = [InlineKeyboardButton(
        f"⏹ Stop {_who(s)}"[:40], callback_data=f"{STOP_ASK}:{_token(s['session_id'])}")
        for s in sessions]
    return text, [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def _back_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("⬅️ Status", callback_data=ADMIN_STATUS)]


async def _find(ctx, token: str) -> tuple[Optional[dict], Optional[str]]:
    """(session, None) for the live session with this token; (None, reason)
    when it ended or Tautulli can't be asked."""
    tau = ctx.bot_data.get("tautulli")
    if tau is None:
        return None, "Tautulli isn't linked."
    try:
        sessions = _sessions(await tau.get_activity())
    except Exception as exc:
        logger.exception("Tautulli get_activity failed")
        return None, f"Couldn't ask Tautulli. {user_friendly_message(exc)}"
    for s in sessions:
        if _token(s["session_id"]) == token:
            return s, None
    return None, "That stream has already ended."


async def _show(update, ctx, text: str, rows) -> None:
    sent = await edit_or_send(update.callback_query, text, parse_mode="HTML",
                              reply_markup=InlineKeyboardMarkup(rows))
    record_btn(ctx.application, update.effective_user.id, sent)


async def _tapped(update, ctx) -> Optional[tuple[Optional[dict], Optional[str]]]:
    """Admin/DM gate, answer the tap, and look the session up. None when the
    gate refused."""
    if await refuse_outside_admin_dm(update, ctx):
        return None
    q = update.callback_query
    await q.answer()
    return await _find(ctx, q.data.split(":", 1)[1])


async def stop_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """A Stop button (stk:<token>): the confirm card."""
    found = await _tapped(update, ctx)
    if found is None:
        return
    session, why = found
    if session is None:
        await _show(update, ctx, html.escape(why), [_back_row()])
        return
    token = _token(session["session_id"])
    await _show(
        update, ctx,
        f"Stop <b>{html.escape(_who(session))}</b>'s stream of "
        f"{html.escape(_title(session))}?",
        [[InlineKeyboardButton("⏹ Stop", callback_data=f"{STOP_GO}:{token}"),
          InlineKeyboardButton("⏹ Stop with message",
                               callback_data=f"{STOP_MSG}:{token}")],
         [InlineKeyboardButton("🛑 Keep playing", callback_data=ADMIN_STATUS)]])


async def stop_confirmed(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop (stg:<token>) or Stop with message (stm:<token>)."""
    found = await _tapped(update, ctx)
    if found is None:
        return
    session, why = found
    if session is None:
        await _show(update, ctx, html.escape(why), [_back_row()])
        return
    message = (DEFAULT_STOP_MESSAGE
               if update.callback_query.data.startswith(f"{STOP_MSG}:")
               else None)
    try:
        await ctx.bot_data["tautulli"].terminate_session(
            session["session_id"], message)
    except Exception as exc:
        logger.exception("Tautulli terminate_session failed")
        await _show(update, ctx,
                    f"Couldn't stop it. {html.escape(user_friendly_message(exc))}",
                    [[InlineKeyboardButton(
                        "🔁 Try again",
                        callback_data=f"{STOP_ASK}:{_token(session['session_id'])}")],
                     _back_row()])
        return
    await _show(update, ctx,
                f"Stopped <b>{html.escape(_who(session))}</b>'s stream of "
                f"{html.escape(_title(session))}.", [_back_row()])
