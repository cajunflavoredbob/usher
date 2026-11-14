"""The Share Expiry job and the buttons on its DMs.

An hourly job walks the `share_expiries` rows. Three days before the date it
DMs the Admin once (Extend 30 days, Let it end, Remove now) and, when the
Share's username matches a Linked user's stored Plex username, DMs that
holder once. On or after the date it re-lists the Shares: one already gone
loses its row quietly; any other is removed through the same call as
/uninvite, its row dropped and the Admin told. A removal that fails keeps the
row, records the error, is retried every hour, and tells the Admin at most
once a day, with Retry."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from http_util import user_friendly_message
from store import ShareExpiry, UserStore

from bot.admin_hub import refuse_outside_admin_dm
from bot.callback_prefixes import (SHARE_EXTEND, SHARE_LET_END,
                                   SHARE_REMOVE_NOW, SHARE_RETRY)
from bot.invite_flow import remove_share_and_forget
from bot.share_expiry import _DB_TIME, expires_at_text, expiry_date_text
from bot.shared import edit_or_send

logger = logging.getLogger("usher")

SHARE_EXPIRY_INTERVAL_S = 60 * 60
SHARE_EXPIRY_FIRST_DELAY_S = 90
REMIND_BEFORE = timedelta(days=3)
ALERT_EVERY = timedelta(days=1)
EXTEND_DAYS = 30


def _parse(text: str) -> datetime:
    return datetime.strptime(text, _DB_TIME).replace(tzinfo=timezone.utc)


def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime(_DB_TIME)


def _who(row: ShareExpiry) -> str:
    return row.email or row.username or f"Share {row.share_id}"


def _retry_markup(share_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "🔁 Retry", callback_data=f"{SHARE_RETRY}:{share_id}")]])


async def _dm(app, chat_id: int, text: str,
              markup: Optional[InlineKeyboardMarkup] = None) -> bool:
    try:
        await app.bot.send_message(chat_id=chat_id, text=text,
                                   parse_mode="HTML", reply_markup=markup,
                                   disable_web_page_preview=True)
        return True
    except Exception:
        logger.exception("Share expiry DM failed (will retry next tick)")
        return False


async def _remind(app, store: UserStore, row: ShareExpiry,
                  admin_id: int) -> None:
    date = expiry_date_text(row.expires_at)
    if not row.admin_reminded:
        sid = row.share_id
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"➕ Extend {EXTEND_DAYS} days",
                                 callback_data=f"{SHARE_EXTEND}:{sid}"),
            InlineKeyboardButton("👍 Let it end",
                                 callback_data=f"{SHARE_LET_END}:{sid}"),
            InlineKeyboardButton("🗑 Remove now",
                                 callback_data=f"{SHARE_REMOVE_NOW}:{sid}"),
        ]])
        if await _dm(app, admin_id,
                     f"⏳ <b>{html.escape(_who(row))}</b>'s access ends on "
                     f"{date}, in about 3 days.", markup):
            await store.mark_share_expiry_reminded(sid, admin=True)
    if not row.holder_reminded and row.username:
        link = await store.find_by_plex_username(row.username)
        if link is not None and await _dm(
                app, link.telegram_id,
                f"⏳ Your access to the server ends on {date}. If you want "
                "more time, ask the Admin."):
            await store.mark_share_expiry_reminded(row.share_id, holder=True)


async def _alert_failure(app, store: UserStore, row: ShareExpiry, error: str,
                         admin_id: int, now: datetime) -> None:
    """Record a failed removal; tell the Admin unless they were told within
    the last day."""
    due = (row.last_alerted_at is None
           or now - _parse(row.last_alerted_at) >= ALERT_EVERY)
    sent = due and await _dm(
        app, admin_id,
        f"⚠️ Couldn't remove <b>{html.escape(_who(row))}</b> after their "
        f"access ended. {html.escape(error)}\nI'll keep trying every hour "
        "and tell you once a day.", _retry_markup(row.share_id))
    await store.record_share_removal_failure(row.share_id, _stamp(now),
                                             error, sent)


async def run_share_expiries(app, now: Optional[datetime] = None) -> None:
    """One tick: due reminders, then due removals."""
    now = now or datetime.now(timezone.utc)
    store: UserStore = app.bot_data["store"]
    rows = await store.list_share_expiries()
    if not rows:
        return
    admin_id = app.bot_data["admin_id"]
    due = [r for r in rows if _parse(r.expires_at) <= now]
    for row in rows:
        if row not in due and _parse(row.expires_at) - now <= REMIND_BEFORE:
            await _remind(app, store, row, admin_id)
    if not due:
        return
    plex = app.bot_data["plex"]
    link = await store.get(admin_id)
    token = getattr(link, "plex_token", None) if link else None
    if not token:
        logger.warning("Share expiry removal skipped: the Admin has no Plex "
                       "token (link Plex with /link)")
        for row in due:
            await _alert_failure(
                app, store, row,
                "Your Plex link is missing: run /link, then tap Retry.",
                admin_id, now)
        return
    try:
        machine_id, _ = await plex.get_owned_server(token)
        present = {s.id for s in await plex.list_shares(token, machine_id)}
    except Exception as exc:
        logger.exception("share listing failed during expiry")
        for row in due:
            await _alert_failure(app, store, row, user_friendly_message(exc),
                                 admin_id, now)
        return
    for row in due:
        # The Admin may have extended or cleared it since the first read.
        fresh = await store.get_share_expiry(row.share_id)
        if fresh is None or _parse(fresh.expires_at) > now:
            continue
        if row.share_id not in present:
            await store.clear_share_expiry(row.share_id)
            continue
        try:
            await remove_share_and_forget(store, plex, token, row.share_id)
        except Exception as exc:
            logger.exception("expired share removal failed")
            await _alert_failure(app, store, row, user_friendly_message(exc),
                                 admin_id, now)
            continue
        await _dm(app, admin_id,
                  f"⏳ Removed <b>{html.escape(_who(row))}</b>: their expiry "
                  f"date ({expiry_date_text(row.expires_at)}) passed.")


async def share_expiry_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await run_share_expiries(ctx.application)
    except Exception:
        logger.exception("Share expiry job failed")


# --- The buttons on the DMs ------------------------------------------------

async def share_expiry_action(update: Update,
                              ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Extend 30 days (shext:<id>), Let it end (shend:<id>), Remove now
    (shnow:<id>) and Retry (shrty:<id>). Admin only; each edits its DM to say
    what happened."""
    if await refuse_outside_admin_dm(update, ctx):
        return
    q = update.callback_query
    await q.answer()
    try:
        action, raw = q.data.split(":")
        share_id = int(raw)
    except ValueError:
        return
    store: UserStore = ctx.bot_data["store"]
    row = await store.get_share_expiry(share_id)
    if row is None:
        await edit_or_send(q, "That Share no longer has an expiry: nothing "
                              "to do.")
        return
    who = html.escape(_who(row))
    if action == SHARE_LET_END:
        await edit_or_send(
            q, f"👍 <b>{who}</b> ends on {expiry_date_text(row.expires_at)} "
               "as planned.", parse_mode="HTML")
    elif action == SHARE_EXTEND:
        base = max(_parse(row.expires_at), datetime.now(timezone.utc))
        new_at = expires_at_text(EXTEND_DAYS, base)
        await store.set_share_expiry(share_id, row.email, row.username,
                                     new_at)
        await edit_or_send(
            q, f"➕ <b>{who}</b> now ends on {expiry_date_text(new_at)}.",
            parse_mode="HTML")
    else:
        await _remove_from_button(q, ctx, store, row, who)


async def _remove_from_button(q, ctx, store: UserStore, row: ShareExpiry,
                              who: str) -> None:
    link = await store.get(ctx.bot_data["admin_id"])
    if link is None or not link.plex_token:
        await edit_or_send(q, "Your /link is no longer usable; /link first.",
                           reply_markup=_retry_markup(row.share_id))
        return
    try:
        gone = await remove_share_and_forget(
            store, ctx.bot_data["plex"], link.plex_token, row.share_id)
    except Exception as exc:
        logger.exception("share removal failed")
        error = user_friendly_message(exc)
        await store.record_share_removal_failure(
            row.share_id, _stamp(datetime.now(timezone.utc)), error, False)
        await edit_or_send(
            q, f"⚠️ Couldn't remove <b>{who}</b>. {html.escape(error)}",
            reply_markup=_retry_markup(row.share_id), parse_mode="HTML")
        return
    await edit_or_send(
        q, "✅ Already removed." if gone else f"✅ Removed <b>{who}</b>.",
        parse_mode="HTML")
