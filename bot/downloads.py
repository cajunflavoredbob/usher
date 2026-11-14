"""The Admin Hub's Downloads view: SABnzbd's queue state with Pause, Pause 30
minutes, Resume and speed limit buttons. Admin-only and DM-only through the
Admin gate, edited in place, ending with "⬅️ Admin". Hidden without SABnzbd.

Every tap sends its SABnzbd call and then re-reads the queue and re-renders.
SABnzbd ignores a percent limit while its maximum bandwidth is unset, so a
percent tap that leaves `speedlimit_abs` empty says to set the maximum first."""
from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from http_util import user_friendly_message

from bot.admin_hub import ADMIN_HUB, refuse_outside_admin_dm
from bot.callback_prefixes import (DOWNLOADS_LIMIT, DOWNLOADS_PAUSE,
                                   DOWNLOADS_PAUSE_30, DOWNLOADS_RESUME)
from bot.hub import back_button, try_again_button
from bot.shared import edit_or_send, record_btn

logger = logging.getLogger("usher")

LIMITS = (25, 50, 75)
NO_MAX_MESSAGE = ("SABnzbd has no maximum speed set, so it ignores a percent "
                  "limit. Set the maximum bandwidth in SABnzbd first.")


def _has(value: str) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _size(num_bytes: str) -> str:
    try:
        n = float(num_bytes)
    except (TypeError, ValueError):
        return num_bytes
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return num_bytes


def _limit_text(st: dict) -> str:
    if _has(st["speedlimit_abs"]):
        text = _size(st["speedlimit_abs"]) + "/s"
        if _has(st["speedlimit"]):
            text += f" ({st['speedlimit']}%)"
        return text
    return "no limit"


def _left_text(st: dict) -> str:
    try:
        mb = float(st["mbleft"])
    except (TypeError, ValueError):
        return "nothing"
    if mb <= 0:
        return "nothing"
    left = f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"
    if st["timeleft"] and st["timeleft"] != "0:00:00":
        return f"{left} ({st['timeleft']})"
    return left


def render_text(st: dict, note: str = "") -> str:
    if st["paused"]:
        state = "⏸ Paused"
        if st["pause_int"] and st["pause_int"] != "0":
            state += f" ({html.escape(st['pause_int'])} left)"
    else:
        state = "▶️ Running"
    lines = ["⏯ <b>Downloads</b>", state,
             f"Speed: {html.escape(st['speed'] or '0')}B/s",
             f"Limit: {html.escape(_limit_text(st))}",
             f"Left: {html.escape(_left_text(st))}"]
    if note:
        lines.append("\n" + html.escape(note))
    return "\n".join(lines)


def _markup(st: dict) -> InlineKeyboardMarkup:
    toggle = (InlineKeyboardButton("▶️ Resume", callback_data=DOWNLOADS_RESUME)
              if st["paused"] else
              InlineKeyboardButton("⏸ Pause", callback_data=DOWNLOADS_PAUSE))
    return InlineKeyboardMarkup([
        [toggle, InlineKeyboardButton("⏱ Pause 30 minutes",
                                      callback_data=DOWNLOADS_PAUSE_30)],
        [InlineKeyboardButton(f"{p}%", callback_data=f"{DOWNLOADS_LIMIT}:{p}")
         for p in LIMITS]
        + [InlineKeyboardButton("No limit",
                                callback_data=f"{DOWNLOADS_LIMIT}:0")],
        [back_button(ADMIN_HUB)],
    ])


async def _show(update, ctx, text: str, markup) -> None:
    sent = await edit_or_send(update.callback_query, text, parse_mode="HTML",
                              reply_markup=markup)
    record_btn(ctx.application, update.effective_user.id, sent)


async def downloads(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """The Downloads button (dlv), Pause (dlp), Pause 30 minutes (dlt),
    Resume (dlr) and a limit (dll:<percent>)."""
    if await refuse_outside_admin_dm(update, ctx):
        return
    q = update.callback_query
    await q.answer()
    sab = ctx.bot_data.get("sabnzbd")
    if sab is None:
        await _show(update, ctx, "SABnzbd isn't linked.",
                    InlineKeyboardMarkup([[back_button(ADMIN_HUB)]]))
        return
    action, _, arg = q.data.partition(":")
    note = ""
    try:
        percent = 0
        if action == DOWNLOADS_PAUSE:
            await sab.pause()
        elif action == DOWNLOADS_PAUSE_30:
            await sab.pause_for(30)
        elif action == DOWNLOADS_RESUME:
            await sab.resume()
        elif action == DOWNLOADS_LIMIT:
            percent = int(arg)
            await sab.set_speed_limit(percent)
        st = await sab.get_queue_state()
        if percent and not _has(st["speedlimit_abs"]):
            note = NO_MAX_MESSAGE
    except Exception as exc:
        logger.exception("SABnzbd downloads call failed")
        await _show(update, ctx,
                    f"Couldn't reach SABnzbd. {html.escape(user_friendly_message(exc))}",
                    InlineKeyboardMarkup([[try_again_button(q.data),
                                           back_button(ADMIN_HUB)]]))
        return
    await _show(update, ctx, render_text(st, note), _markup(st))
