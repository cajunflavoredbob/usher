"""Access asks: a stranger whose Plex sign-in Seerr rejected asks the Admin
for an Invite. See the Access ask entry in GLOSSARY.md.

The Link flow's rejected-sign-in message carries "Ask for access" (see
`offer_markup`, `remember_sign_in`). The tap shows what the Admin will see
and a Send button; Send reads the person's Plex username and email from
plex.tv with the fresh token (kept in memory only, for ACCESS_ASK_TOKEN_TTL_S)
and stores one Access ask, then DMs the Admin with Approve and Ignore.

Approve is an entry point of the invite conversation (bot.invite_flow); it
calls `approve_access_ask` once the Invite is sent. Ignore is handled here and
tells the person nothing."""
from __future__ import annotations

import html
import logging
import time
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from http_util import user_friendly_message
from store import AccessAsk, UserStore

from bot.admin_hub import refuse_outside_admin_dm
from bot.callback_prefixes import (ACCESS_APPROVE, ACCESS_ASK, ACCESS_CANCEL,
                                   ACCESS_IGNORE, ACCESS_SEND)
from bot.hub import end_markup
from bot.shared import (edit_or_send, end_action, record_btn,
                        try_begin_action)
from const import ACCESS_ASK_IGNORE_DAYS, ACCESS_ASK_TOKEN_TTL_S

logger = logging.getLogger("usher")

# user_data key: {"token", "hub", "at"} left by the rejected sign-in.
_STATE_KEY = "access_ask"

ENDED_TOAST = "That sign-in has ended. Link again to ask for access."
REFUSALS = {
    "open": "✅ Your ask is already waiting for the Admin.",
    "ignored": ("The Admin can't take another ask from you right now. "
                f"Try again in {ACCESS_ASK_IGNORE_DAYS} days."),
    "full": ("The Admin has too many asks waiting right now. Try again "
             "later."),
}


def remember_sign_in(ctx: ContextTypes.DEFAULT_TYPE, token: str,
                     hub: Optional[str]) -> None:
    """Keep the rejected sign-in's token in memory (never stored) so a
    later Send can read the person's Plex username and email."""
    ctx.user_data[_STATE_KEY] = {"token": token, "hub": hub, "at": time.time()}


def forget_sign_in(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.pop(_STATE_KEY, None)


def _state(ctx: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    st = ctx.user_data.get(_STATE_KEY)
    if not st or time.time() - st.get("at", 0) > ACCESS_ASK_TOKEN_TTL_S:
        forget_sign_in(ctx)
        return None
    return st


def offer_markup(hub: Optional[str],
                 retry: Optional[str] = None) -> InlineKeyboardMarkup:
    """Keyboard of the rejected-sign-in message: "Ask for access", then the
    Hub's end row (Try again and the back button) when started from a Hub."""
    rows = [[InlineKeyboardButton("🙋 Ask for access",
                                  callback_data=ACCESS_ASK)]]
    if hub is not None:
        rows += end_markup(hub, retry).inline_keyboard
    return InlineKeyboardMarkup(rows)


def _end(hub: Optional[str]) -> Optional[InlineKeyboardMarkup]:
    return None if hub is None else end_markup(hub)


async def _refuse_group(update: Update) -> bool:
    if update.effective_chat.type == "private":
        return False
    await update.callback_query.answer("Please DM me for this.",
                                       show_alert=True)
    return True


def _preview_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📤 Send", callback_data=ACCESS_SEND),
        InlineKeyboardButton("🛑 Cancel", callback_data=ACCESS_CANCEL),
    ]])


_PREVIEW = ("🙋 <b>Ask the Admin for access</b>\n\n"
            "The Admin will see your Telegram name, and the username and "
            "email of the Plex account you just signed in with. They can "
            "then invite you, or ignore the ask. Send it?")


async def access_ask_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """"Ask for access" tap: show what the Admin will see. Nothing is stored
    yet."""
    q = update.callback_query
    if await _refuse_group(update):
        return
    st = _state(ctx)
    if st is None:
        await q.answer(ENDED_TOAST, show_alert=True)
        return
    await q.answer()
    sent = await edit_or_send(q, _PREVIEW, reply_markup=_preview_markup(),
                              parse_mode="HTML")
    record_btn(ctx.application, update.effective_user.id, sent)


async def access_ask_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if await _refuse_group(update):
        return
    st = _state(ctx)
    await q.answer()
    hub = st["hub"] if st else None
    forget_sign_in(ctx)
    sent = await edit_or_send(q, "Cancelled. Nothing was sent.",
                              reply_markup=_end(hub))
    if hub is not None:
        record_btn(ctx.application, update.effective_user.id, sent)


def ask_text(ask: AccessAsk, *, username: Optional[str] = None,
             share_line: Optional[str] = None,
             outcome: Optional[str] = None) -> str:
    """The Admin DM's text, rebuilt from the stored ask (plus the extras only
    known at send time). `outcome` replaces the buttons' question once the
    Admin has acted."""
    who = f"<b>{html.escape(ask.telegram_name or 'Unknown')}</b>"
    if username:
        who += f" (@{html.escape(username)})"
    lines = ["🙋 <b>Access ask</b>", f"Telegram: {who}",
             f"Plex username: {html.escape(ask.plex_username or '?')}",
             f"Plex email: {html.escape(ask.plex_email or '?')}"]
    if share_line:
        lines.append(share_line)
    if outcome:
        lines += ["", outcome]
    return "\n".join(lines)


async def _share_line(ctx: ContextTypes.DEFAULT_TYPE,
                      email: str) -> Optional[str]:
    """Whether the Plex email already has a Share. Best effort: None when
    the Admin has no usable Plex link, owns no server or plex.tv fails."""
    if not email:
        return None
    try:
        store: UserStore = ctx.bot_data["store"]
        link = await store.get(ctx.bot_data["admin_id"])
        if link is None or not link.plex_token:
            return None
        plex = ctx.bot_data["plex"]
        machine_id, _name = await plex.get_owned_server(link.plex_token)
        shares = await plex.list_shares(link.plex_token, machine_id)
    except Exception as exc:
        logger.info("access ask: share lookup skipped: %s", exc)
        return None
    for share in shares:
        if share.email and share.email.lower() == email.lower():
            state = "accepted" if share.accepted else "an Invite, not accepted yet"
            return (f"ℹ️ This email already has a Share ({state}): they "
                    "may need help linking, not access.")
    return "No Share for this email yet."


def _admin_kb(ask_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve",
                             callback_data=f"{ACCESS_APPROVE}:{ask_id}"),
        InlineKeyboardButton("🚫 Ignore",
                             callback_data=f"{ACCESS_IGNORE}:{ask_id}"),
    ]])


async def access_ask_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Send tap: read the person's Plex account, store one open ask and DM
    the Admin."""
    q = update.callback_query
    if await _refuse_group(update):
        return
    st = _state(ctx)
    if st is None:
        await q.answer(ENDED_TOAST, show_alert=True)
        return
    user = update.effective_user
    key = f"access_ask:{user.id}"
    if not try_begin_action(ctx, key):
        await q.answer()
        return
    try:
        await q.answer()
        await _send(update, ctx, st)
    finally:
        end_action(ctx, key)


async def _send(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                st: dict) -> None:
    q = update.callback_query
    user = update.effective_user
    hub = st["hub"]
    admin_id = ctx.bot_data.get("admin_id")

    async def show(text: str, markup=None) -> None:
        sent = await edit_or_send(q, text, reply_markup=markup,
                                  parse_mode="HTML")
        if markup is not None:
            record_btn(ctx.application, user.id, sent)

    if not admin_id:
        forget_sign_in(ctx)
        await show("There is no Admin to ask.", _end(hub))
        return
    store: UserStore = ctx.bot_data["store"]
    try:
        account = await ctx.bot_data["plex"].get_account(st["token"])
    except Exception as exc:
        logger.exception("plex account lookup for an access ask failed")
        await show("Couldn't read your Plex account. "
                   f"{html.escape(user_friendly_message(exc))}\n\n"
                   "Send it again?", _preview_markup())
        return
    name = getattr(user, "full_name", None) or "Unknown"
    ask_id, reason = await store.create_access_ask(
        user.id, name, account.username, account.email)
    if ask_id is None:
        forget_sign_in(ctx)
        await show(REFUSALS[reason], _end(hub))
        return
    ask = await store.get_access_ask(ask_id)
    try:
        share_line = await _share_line(ctx, account.email)
        sent = await ctx.bot.send_message(
            chat_id=admin_id,
            text=ask_text(ask, username=getattr(user, "username", None),
                          share_line=share_line),
            parse_mode="HTML", reply_markup=_admin_kb(ask_id))
        record_btn(ctx.application, admin_id, sent)
        await store.set_access_ask_message(
            ask_id, getattr(sent, "message_id", None))
    except Exception as exc:
        logger.exception("access ask DM to the Admin failed")
        await store.delete_access_ask(ask_id)
        await show("Couldn't reach the Admin. "
                   f"{html.escape(user_friendly_message(exc))}\n\n"
                   "Send it again?", _preview_markup())
        return
    forget_sign_in(ctx)
    await show("📨 Sent to the Admin. If they approve, you'll get a message "
               "here to accept the Invite in Plex and link again.", _end(hub))


async def access_ask_ignore(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin DM's Ignore: mark the ask ignored and edit the DM. The person
    is not told."""
    if await refuse_outside_admin_dm(update, ctx):
        return
    q = update.callback_query
    try:
        ask_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await q.answer()
        return
    store: UserStore = ctx.bot_data["store"]
    ask = await store.get_access_ask(ask_id)
    if ask is None or not await store.decide_access_ask(ask_id, "ignored"):
        await q.answer("That ask was already handled.", show_alert=True)
        return
    await q.answer()
    await edit_or_send(q, ask_text(ask, outcome="🚫 Ignored."),
                       parse_mode="HTML")


async def approve_access_ask(ctx: ContextTypes.DEFAULT_TYPE,
                             ask_id: int) -> None:
    """The Invite for an ask was sent: mark it approved, DM the person to
    accept it in Plex and link again, and update the Admin DM. Never raises:
    the Invite already went out."""
    store: UserStore = ctx.bot_data["store"]
    try:
        ask = await store.get_access_ask(ask_id)
        if ask is None or not await store.decide_access_ask(ask_id, "approved"):
            return
    except Exception:
        logger.exception("couldn't mark access ask %s approved", ask_id)
        return
    try:
        await ctx.bot.send_message(
            chat_id=ask.telegram_id,
            text=("✅ The Admin approved your ask and sent you an Invite to "
                  "the Plex server. Accept it in Plex (check your email), "
                  "then /link again."))
    except TelegramError as exc:
        logger.info("couldn't tell %s their access ask was approved: %s",
                    ask.telegram_id, exc)
    if ask.admin_message_id:
        try:
            await ctx.bot.edit_message_text(
                chat_id=ctx.bot_data["admin_id"],
                message_id=ask.admin_message_id,
                text=ask_text(ask, outcome="✅ Approved: Invite sent."),
                parse_mode="HTML")
        except TelegramError as exc:
            logger.info("couldn't update the access ask DM: %s", exc)
