"""The Share Expiry job and the buttons on its DMs.

An hourly job walks the `share_expiries` rows. Three days before the date it
DMs the Admin once (Extend 30 days, Let it end, Remove now) and, when the
Share's username matches a Linked user's stored Plex username, DMs that
holder once. On or after the date it re-lists the Shares: one already gone
loses its row quietly and its holder is unlinked, unless that listing names
the same Plex username on another Share; any other is removed through the
same call as /uninvite, which the job gives a check that re-reads the row
right before the removal and leaves a Share whose date moved or was cleared;
its row is dropped and the Admin told. While the job removes a Share, Extend on it is refused. A
removal that fails keeps the row, records the error, is retried every hour,
and tells the Admin at most once a day, with Retry. A row with no username
(an Invite stored before it was accepted) makes the job read the listing
every tick, while the Admin is linked, until the listing names that Share's
username, which it then stores for the holder reminder and the unlink."""
from __future__ import annotations

import dataclasses
import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from const import db_time as _stamp, parse_db_time as _parse
from http_util import user_friendly_message
from store import ShareExpiry, UserStore

from bot.admin_hub import refuse_outside_admin_dm
from bot.callback_prefixes import (SHARE_EXTEND, SHARE_KEEP, SHARE_LET_END,
                                   SHARE_REMOVE_NOW, SHARE_REMOVE_RETRY,
                                   SHARE_REMOVE_YES, SHARE_RETRY)
from bot.invite_flow import (forget_gone_share, remove_share_and_forget,
                             removal_prompt)
from bot.share_expiry import expires_at_text, expiry_date_text
from bot.shared import edit_or_send

logger = logging.getLogger("usher")

SHARE_EXPIRY_INTERVAL_S = 60 * 60
SHARE_EXPIRY_FIRST_DELAY_S = 90
REMIND_BEFORE = timedelta(days=3)
ALERT_EVERY = timedelta(days=1)
EXTEND_DAYS = 30
# bot_data: {share id: why} for the Shares being removed right now. Extend
# refuses while its row is here, so a slow removal cannot undo a fresh Extend,
# and says why per reason. Whoever sets a mark clears it; nobody clears one
# another set. Not persisted: a restart starts with no removal in flight.
REMOVING_KEY = "share_expiry_removing"
BY_DATE = "date"    # the hourly check: the expiry date passed
BY_ADMIN = "admin"  # the Admin chose Remove now (or its Retry)
BY_RETRY = "retry"  # the Retry of the hourly check's failed removal
_REMOVING_NOTE = {
    BY_DATE: "This Share is being removed now because its date passed.",
    BY_ADMIN: "This Share is being removed now because you chose Remove.",
    BY_RETRY: ("This Share is being removed now because you chose Retry "
               "after its date passed."),
}
# Any button on a DM whose Expiry row is gone: the Share was removed (by the
# job or by the Admin elsewhere), or the Admin cleared the expiry; the bot
# cannot tell which.
OUT_OF_DATE = ("This reminder is out of date: the Share was removed or its "
               "expiry was cleared. Open the Share view to check.")


def _who(row: ShareExpiry) -> str:
    return row.email or row.username or f"Share {row.share_id}"


def _retry_markup(share_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "🔁 Retry", callback_data=f"{SHARE_RETRY}:{share_id}")]])


def _remove_retry_markup(share_id: int) -> InlineKeyboardMarkup:
    """Retry for a failed Remove now: the Admin chose to remove, so it does
    not look at the date (unlike the job alert's Retry)."""
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "🔁 Retry", callback_data=f"{SHARE_REMOVE_RETRY}:{share_id}")]])


def _stamp_token(expires_at: str) -> str:
    """The stored date as 14 digits, carried by the Extend button so a second
    tap (or a tap on an old reminder) can tell the date has moved."""
    return "".join(ch for ch in expires_at if ch.isdigit())


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


def _ends_in(expires_at: str, now: datetime) -> str:
    """", in about 3 days" for the reminder, from the row's real date: whole
    days left, "in less than a day" under one, nothing once it has passed."""
    left = _parse(expires_at) - now
    if left <= timedelta(0):
        return ""
    if left < timedelta(days=1):
        return ", in less than a day"
    days = round(left / timedelta(days=1))
    return f", in about {days} day{'s' if days != 1 else ''}"


def _reminder_card(row: ShareExpiry, now: Optional[datetime] = None
                   ) -> tuple[str, InlineKeyboardMarkup]:
    """The Admin's reminder: text and Extend / Let it end / Remove now."""
    now = now or datetime.now(timezone.utc)
    sid = row.share_id
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"➕ Extend {EXTEND_DAYS} days",
                             callback_data=(f"{SHARE_EXTEND}:{sid}:"
                                            f"{_stamp_token(row.expires_at)}")),
        InlineKeyboardButton("👍 Let it end",
                             callback_data=f"{SHARE_LET_END}:{sid}"),
        InlineKeyboardButton("🗑 Remove now",
                             callback_data=f"{SHARE_REMOVE_NOW}:{sid}"),
    ]])
    date = expiry_date_text(row.expires_at)
    return (f"⏳ <b>{html.escape(_who(row))}</b>'s access ends on "
            f"{date}{_ends_in(row.expires_at, now)}.", markup)


async def _remind(app, store: UserStore, row: ShareExpiry,
                  admin_id: int, now: datetime) -> None:
    date = expiry_date_text(row.expires_at)
    if not row.admin_reminded:
        text, markup = _reminder_card(row, now)
        if await _dm(app, admin_id, text, markup):
            await store.mark_share_expiry_reminded(row.share_id, admin=True)
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


async def _fill_usernames(store: UserStore, rows: list[ShareExpiry],
                          shares: list) -> list[ShareExpiry]:
    """Store the username the listing now reports for each row that has
    none, and return the rows as they now are. A pending Invite lists no
    username yet; once accepted, its holder's shows up here, and the holder
    reminder and the gone path's unlink both need it."""
    listed = {s.id: s.username for s in shares if s.username}
    out = []
    for row in rows:
        name = listed.get(row.share_id) if not row.username else None
        if name and await store.fill_share_expiry_username(row.share_id, name):
            row = dataclasses.replace(row, username=name)
        out.append(row)
    return out


async def run_share_expiries(app, now: Optional[datetime] = None) -> None:
    """One tick: due reminders, then due removals."""
    now = now or datetime.now(timezone.utc)
    store: UserStore = app.bot_data["store"]
    rows = await store.list_share_expiries()
    if not rows:
        return
    admin_id = app.bot_data["admin_id"]
    any_due = any(_parse(r.expires_at) <= now for r in rows)
    # A row with no username (an Invite stored before it was accepted) needs
    # the listing too: it is the only place the holder's username shows up,
    # and it must be learnt while the Share is still listed.
    unnamed = any(not r.username for r in rows)
    plex = app.bot_data["plex"]
    token: Optional[str] = None
    shares: Optional[list] = None
    listing_error: Optional[Exception] = None
    if any_due or unnamed:
        link = await store.get(admin_id)
        token = getattr(link, "plex_token", None) if link else None
    if token:
        try:
            machine_id, _ = await plex.get_owned_server(token)
            shares = list(await plex.list_shares(token, machine_id))
        except Exception as exc:
            logger.exception("share listing failed during expiry")
            listing_error = exc
    if shares is not None and unnamed:
        rows = await _fill_usernames(store, rows, shares)
    due = [r for r in rows if _parse(r.expires_at) <= now]
    for row in rows:
        if row not in due and _parse(row.expires_at) - now <= REMIND_BEFORE:
            await _remind(app, store, row, admin_id, now)
    if not due:
        return
    if not token:
        logger.warning("Share expiry removal skipped: the Admin has no Plex "
                       "token (link Plex with /link)")
        for row in due:
            await _alert_failure(
                app, store, row,
                "Your Plex link is missing: run /link, then tap Retry.",
                admin_id, now)
        return
    if shares is None:
        for row in due:
            await _alert_failure(app, store, row,
                                 user_friendly_message(listing_error, admin=True),
                                 admin_id, now)
        return
    removing: dict[int, str] = app.bot_data.setdefault(REMOVING_KEY, {})
    for row in due:
        if row.share_id in removing:
            # A removal the Admin's button already started keeps its own mark
            # and reports its own result: the job leaves the row to it rather
            # than call Plex a second time for the same Share.
            continue
        removing[row.share_id] = BY_DATE
        try:
            if await _remove_due(app, store, plex, token, row, shares,
                                 admin_id, now):
                # Removed this tick: no longer a Share that keeps a later
                # gone row's holder linked.
                shares = [s for s in shares if s.id != row.share_id]
        finally:
            removing.pop(row.share_id, None)


async def _remove_due(app, store: UserStore, plex, token: str,
                      row: ShareExpiry, shares: list, admin_id: int,
                      now: datetime) -> bool:
    """Remove one due Share (or forget a gone one); True when this removal
    left it gone from Plex (removed, or Plex answered it was already gone)."""
    async def still_due() -> bool:
        # The Admin may have extended or cleared it since the first read.
        fresh = await store.get_share_expiry(row.share_id)
        return fresh is not None and _parse(fresh.expires_at) <= now

    if not await still_due():
        return False
    if all(s.id != row.share_id for s in shares):
        await forget_gone_share(store, row.share_id, None, shares)
        return False
    try:
        result = await remove_share_and_forget(store, plex, token,
                                               row.share_id,
                                               still_due=still_due)
    except Exception as exc:
        logger.exception("expired share removal failed")
        await _alert_failure(app, store, row, user_friendly_message(exc, admin=True),
                             admin_id, now)
        return False
    if result is None:
        return False
    await _dm(app, admin_id,
              f"⏳ Removed <b>{html.escape(_who(row))}</b>: their expiry "
              f"date ({expiry_date_text(row.expires_at)}) passed.")
    return True


async def share_expiry_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await run_share_expiries(ctx.application)
    except Exception:
        logger.exception("Share expiry job failed")


# --- The buttons on the DMs ------------------------------------------------

async def share_expiry_action(update: Update,
                              ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Extend 30 days (shext:<id>:<date digits>), Let it end (shend:<id>), Remove now
    (shnow:<id>, which asks to confirm: Remove shnoy:<id>, Keep shnok:<id>)
    and two Retries: shrty:<id> on the job's failure alert, which removes
    only while the date is still due, and shnor:<id> after a failed Remove
    now, which removes whatever the date. Admin only; each edits its DM to
    say what happened. The DMs are not recorded buttons (a reminder can sit for days),
    so these prefixes are exempt from the button gate (bot.shared); this
    handler is the check: Admin and DM only, and it re-reads the row."""
    if await refuse_outside_admin_dm(update, ctx):
        return
    q = update.callback_query
    await q.answer()
    try:
        action, raw, *rest = q.data.split(":")
        share_id = int(raw)
    except ValueError:
        return
    token = rest[0] if len(rest) == 1 else None
    store: UserStore = ctx.bot_data["store"]
    row = await store.get_share_expiry(share_id)
    if row is None:
        await edit_or_send(q, OUT_OF_DATE)
        return
    who = html.escape(_who(row))
    if action == SHARE_LET_END:
        await edit_or_send(
            q, f"👍 <b>{who}</b> ends on {expiry_date_text(row.expires_at)} "
               "as planned.", parse_mode="HTML")
    elif (action == SHARE_EXTEND
          and share_id in ctx.bot_data.get(REMOVING_KEY, ())):
        _text, markup = _reminder_card(row)
        await edit_or_send(
            q, _REMOVING_NOTE.get(ctx.bot_data[REMOVING_KEY][share_id],
                                  _REMOVING_NOTE[BY_DATE]),
            reply_markup=markup)
    elif action == SHARE_EXTEND:
        base = max(_parse(row.expires_at), datetime.now(timezone.utc))
        new_at = expires_at_text(EXTEND_DAYS, base)
        # A button carries the date its reminder was sent for (an older one
        # carries none: it compares against the row just read). Only a row
        # still on that date moves, atomically, so a second tap or an old
        # reminder cannot extend again.
        expected = row.expires_at if token is None else token
        if not await store.extend_share_expiry(share_id, expected, new_at):
            row = await store.get_share_expiry(share_id)
            text = (OUT_OF_DATE if row is None
                    else f"👍 <b>{who}</b> was already extended: now ends "
                         f"on {expiry_date_text(row.expires_at)}.")
            await edit_or_send(q, text, parse_mode="HTML")
            return
        await edit_or_send(
            q, f"➕ <b>{who}</b> now ends on {expiry_date_text(new_at)}.",
            parse_mode="HTML")
    elif action == SHARE_REMOVE_NOW:
        await _confirm_removal(q, ctx, store, row)
    elif action == SHARE_KEEP:
        text, markup = _reminder_card(row)
        await edit_or_send(q, text, reply_markup=markup, parse_mode="HTML")
    elif (action == SHARE_RETRY
          and _parse(row.expires_at) > datetime.now(timezone.utc)):
        # Extended since the failed removal: the Admin chose to keep it.
        await edit_or_send(
            q, f"👍 <b>{who}</b> now ends on "
               f"{expiry_date_text(row.expires_at)}: left as it is.",
            parse_mode="HTML")
    else:
        # The confirmed Remove, a Retry after a failed Remove now (shnor),
        # or a Retry of a removal that is still due (shrty, checked above).
        # A failure keeps the kind of Retry that failed: the date-checked
        # one is checked again on every tap.
        retry = (_retry_markup if action == SHARE_RETRY
                 else _remove_retry_markup)
        await _remove_from_button(q, ctx, store, row, who, retry,
                                  only_if_due=action == SHARE_RETRY)


async def _confirm_removal(q, ctx, store: UserStore, row: ShareExpiry) -> None:
    """Remove now: look the Share up live and ask, on the same DM, with the
    Share view's confirm text (Remove / Keep)."""
    sid = row.share_id
    link = await store.get(ctx.bot_data["admin_id"])
    if link is None or not link.plex_token:
        text, markup = _reminder_card(row)
        await edit_or_send(
            q, "Your /link is no longer usable; /link first.\n\n" + text,
            reply_markup=markup, parse_mode="HTML")
        return
    plex = ctx.bot_data["plex"]
    try:
        machine_id, _ = await plex.get_owned_server(link.plex_token)
        shares = await plex.list_shares(link.plex_token, machine_id)
    except Exception as exc:
        logger.exception("share listing failed for Remove now")
        await edit_or_send(
            q, f"⚠️ Couldn't check the Share. "
               f"{html.escape(user_friendly_message(exc, admin=True))}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "🔁 Try again", callback_data=f"{SHARE_REMOVE_NOW}:{sid}")]]),
            parse_mode="HTML")
        return
    share = next((s for s in shares if s.id == sid), None)
    if share is None:
        await forget_gone_share(store, sid, row.username, shares)
        await edit_or_send(q, "✅ Already removed.")
        return
    text, _flag = removal_prompt(share)
    await edit_or_send(
        q, text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "✅ Remove", callback_data=f"{SHARE_REMOVE_YES}:{sid}"),
            InlineKeyboardButton(
                "🛑 Keep", callback_data=f"{SHARE_KEEP}:{sid}"),
        ]]))


async def _remove_from_button(q, ctx, store: UserStore, row: ShareExpiry,
                              who: str, retry_markup, *,
                              only_if_due: bool = False) -> None:
    """Remove the Share; a failure keeps `retry_markup`'s Retry on the DM.
    `only_if_due` (the job's Retry) re-reads the row right before the
    removal and leaves a Share whose date moved or was cleared, as the job
    does. The Share is marked as being removed meanwhile (with why), so Extend
    on it is refused rather than racing the removal, and a second Remove tap
    while it runs is ignored. The DM shows a working state during the Plex
    call."""
    link = await store.get(ctx.bot_data["admin_id"])
    if link is None or not link.plex_token:
        await edit_or_send(q, "Your /link is no longer usable; /link first.",
                           reply_markup=retry_markup(row.share_id))
        return
    async def still_due() -> bool:
        fresh = await store.get_share_expiry(row.share_id)
        return (fresh is not None
                and _parse(fresh.expires_at) <= datetime.now(timezone.utc))

    removing: dict[int, str] = ctx.bot_data.setdefault(REMOVING_KEY, {})
    mark = removing.get(row.share_id)
    if mark is not None:
        # A double tap: the removal already running reports. When it is the
        # hourly check's, say so on this DM, which would otherwise sit still.
        if mark == BY_DATE:
            await edit_or_send(
                q, f"⏳ <b>{who}</b> is being removed by the hourly check "
                   "right now.", parse_mode="HTML")
        elif mark == BY_RETRY:
            await edit_or_send(
                q, f"⏳ <b>{who}</b> is being removed by a Retry right now.",
                parse_mode="HTML")
        return
    # Check and mark with no await between, so two taps cannot both pass.
    removing[row.share_id] = BY_RETRY if only_if_due else BY_ADMIN
    try:
        # Without its keyboard, so a second tap has nothing to press.
        await edit_or_send(q, f"⏳ Removing <b>{who}</b>...",
                           parse_mode="HTML")
        gone = await remove_share_and_forget(
            store, ctx.bot_data["plex"], link.plex_token, row.share_id,
            still_due=still_due if only_if_due else None)
    except Exception as exc:
        logger.exception("share removal failed")
        error = user_friendly_message(exc, admin=True)
        await store.record_share_removal_failure(
            row.share_id, _stamp(datetime.now(timezone.utc)), error, False)
        await edit_or_send(
            q, f"⚠️ Couldn't remove <b>{who}</b>. {html.escape(error)}",
            reply_markup=retry_markup(row.share_id), parse_mode="HTML")
        return
    finally:
        removing.pop(row.share_id, None)
    if gone is None:
        # The date moved or the row went since the failure alert.
        fresh = await store.get_share_expiry(row.share_id)
        if fresh is None:
            await edit_or_send(q, OUT_OF_DATE)
        else:
            await edit_or_send(
                q, f"👍 <b>{who}</b> now ends on "
                   f"{expiry_date_text(fresh.expires_at)}: left as it is.",
                parse_mode="HTML")
        return
    await edit_or_send(
        q, "✅ Already removed." if gone else f"✅ Removed <b>{who}</b>.",
        parse_mode="HTML")
