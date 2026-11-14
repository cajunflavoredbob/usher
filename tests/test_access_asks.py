"""Access asks: the "Ask for access" button on the rejected-sign-in message,
what Send stores and DMs, the limits, and the Admin's Approve (into the invite
flow with the email filled in) and Ignore."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.ext import ConversationHandler

from bot.access_asks import (REFUSALS, access_ask_cancel, access_ask_ignore,
                             access_ask_send, access_ask_start,
                             remember_sign_in)
from bot.callback_prefixes import (ACCESS_APPROVE, ACCESS_ASK, ACCESS_CANCEL,
                                   ACCESS_IGNORE, ACCESS_SEND, HUB_OPEN)
from bot.invite_flow import (_invite_conversation, invite_access_start,
                             invite_cancel, invite_libs_done, invite_send)
from bot.shared import INV_LIBS
from const import ACCESS_ASK_MAX_OPEN
from plex import PlexAccount, PlexLibrarySection, PlexShare

from tests._handler_harness import make_ctx, make_link, make_update

ADMIN_ID = 999
STRANGER = 42


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _ctx(store, *, user_data=None, shares=()):
    ctx = make_ctx(admin_id=ADMIN_ID, user_data=user_data)
    ctx.bot_data["store"] = store
    ctx.bot.send_message.return_value = SimpleNamespace(message_id=555,
                                                        chat_id=ADMIN_ID)
    plex = AsyncMock()
    plex.get_account.return_value = PlexAccount(username="newbie",
                                                email="new@example.com")
    plex.get_owned_server.return_value = ("machine-1", "server1")
    plex.get_library_sections.return_value = [
        PlexLibrarySection(id=101, title="Movies", type="movie")]
    plex.list_shares.return_value = list(shares)
    ctx.bot_data["plex"] = plex
    return ctx


async def _link_admin(store):
    await store.link_with_plex(telegram_id=ADMIN_ID, seerr_id=1,
                               seerr_display="Admin", plex_token="admin-tok",
                               plex_uuid="u", plex_username="admin")


def _stranger_update(data, user_id=STRANGER):
    upd = make_update(callback_data=data, user_id=user_id)
    upd.effective_user.full_name = "Newbie N"
    upd.effective_user.username = "newbie_tg"
    return upd


async def _send_ask(store, user_id=STRANGER, ctx=None):
    """One stranger's Send tap; returns (ctx, update)."""
    ctx = ctx or _ctx(store)
    remember_sign_in(ctx, "fresh-tok", None)
    upd = _stranger_update(ACCESS_SEND, user_id)
    await access_ask_send(upd, ctx)
    return ctx, upd


def _db(store):
    return sqlite3.connect(store.path)


# --- the button and the preview ---------------------------------------------

async def test_ask_tap_previews_and_stores_nothing(fresh_store):
    ctx = _ctx(fresh_store)
    remember_sign_in(ctx, "fresh-tok", None)
    upd = _stranger_update(ACCESS_ASK)
    await access_ask_start(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert "email" in edit["text"] and "Telegram name" in edit["text"]
    assert _callbacks(edit["reply_markup"]) == [ACCESS_SEND, ACCESS_CANCEL]
    ctx.bot_data["plex"].get_account.assert_not_called()
    assert _db(fresh_store).execute(
        "SELECT COUNT(*) FROM access_asks").fetchone()[0] == 0


async def test_ask_tap_without_a_fresh_sign_in_is_refused(fresh_store):
    ctx = _ctx(fresh_store)
    upd = _stranger_update(ACCESS_ASK)
    await access_ask_start(upd, ctx)
    assert upd.callback_query.edits == []
    assert upd.callback_query.answers[0][1] is True


async def test_cancel_sends_nothing_and_forgets_the_sign_in(fresh_store):
    ctx = _ctx(fresh_store)
    remember_sign_in(ctx, "fresh-tok", None)
    upd = _stranger_update(ACCESS_CANCEL)
    await access_ask_cancel(upd, ctx)
    assert "Nothing was sent" in upd.callback_query.edits[0]["text"]
    assert "access_ask" not in ctx.user_data
    ctx.bot.send_message.assert_not_called()


# --- Send ---------------------------------------------------------------------

async def test_send_creates_one_open_ask_and_one_admin_dm(fresh_store):
    ctx, upd = await _send_ask(fresh_store)
    ctx.bot_data["plex"].get_account.assert_awaited_once_with("fresh-tok")
    row = _db(fresh_store).execute(
        "SELECT telegram_id, telegram_name, plex_username, plex_email, "
        "status, admin_message_id FROM access_asks").fetchall()
    assert len(row) == 1
    assert row[0][:5] == (STRANGER, "Newbie N", "newbie", "new@example.com",
                          "open")
    assert row[0][5] is not None
    ctx.bot.send_message.assert_awaited_once()
    kwargs = ctx.bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == ADMIN_ID
    assert "Newbie N" in kwargs["text"] and "@newbie_tg" in kwargs["text"]
    assert "newbie" in kwargs["text"] and "new@example.com" in kwargs["text"]
    ask_id = _db(fresh_store).execute("SELECT id FROM access_asks").fetchone()[0]
    assert _callbacks(kwargs["reply_markup"]) == [
        f"{ACCESS_APPROVE}:{ask_id}", f"{ACCESS_IGNORE}:{ask_id}"]
    assert "Sent to the Admin" in upd.callback_query.edits[-1]["text"]


async def test_admin_dm_says_when_the_email_already_has_a_share(fresh_store):
    await _link_admin(fresh_store)
    share = PlexShare(id=5, email="NEW@example.com", username="newbie",
                      accepted=False, all_libraries=True)
    ctx = _ctx(fresh_store, shares=[share])
    await _send_ask(fresh_store, ctx=ctx)
    assert "already has a Share" in ctx.bot.send_message.call_args.kwargs["text"]


async def test_admin_dm_says_when_there_is_no_share(fresh_store):
    await _link_admin(fresh_store)
    ctx = _ctx(fresh_store)
    await _send_ask(fresh_store, ctx=ctx)
    assert "No Share for this email" in ctx.bot.send_message.call_args.kwargs["text"]


async def test_failed_share_lookup_still_sends_the_dm(fresh_store):
    await _link_admin(fresh_store)
    ctx = _ctx(fresh_store)
    ctx.bot_data["plex"].list_shares.side_effect = RuntimeError("plex.tv down")
    await _send_ask(fresh_store, ctx=ctx)
    ctx.bot.send_message.assert_awaited_once()


async def test_second_ask_while_one_is_open_is_refused(fresh_store):
    await _send_ask(fresh_store)
    ctx, upd = await _send_ask(fresh_store)
    ctx.bot.send_message.assert_not_called()
    assert upd.callback_query.edits[-1]["text"] == REFUSALS["open"]
    assert _db(fresh_store).execute(
        "SELECT COUNT(*) FROM access_asks").fetchone()[0] == 1


async def test_ask_within_7_days_of_ignore_is_refused_then_allowed(fresh_store):
    await _send_ask(fresh_store)
    ask_id = _db(fresh_store).execute("SELECT id FROM access_asks").fetchone()[0]
    assert await fresh_store.decide_access_ask(ask_id, "ignored")
    ctx, upd = await _send_ask(fresh_store)
    ctx.bot.send_message.assert_not_called()
    assert upd.callback_query.edits[-1]["text"] == REFUSALS["ignored"]
    assert "7 days" in REFUSALS["ignored"]
    with _db(fresh_store) as c:
        c.execute("UPDATE access_asks SET decided_at = datetime('now', '-8 days')")
    ctx, upd = await _send_ask(fresh_store)
    ctx.bot.send_message.assert_awaited_once()
    assert _db(fresh_store).execute(
        "SELECT status FROM access_asks").fetchall() == [("open",)]


async def test_eleventh_open_ask_is_refused(fresh_store):
    for uid in range(100, 100 + ACCESS_ASK_MAX_OPEN):
        await _send_ask(fresh_store, user_id=uid)
    ctx, upd = await _send_ask(fresh_store, user_id=STRANGER)
    ctx.bot.send_message.assert_not_called()
    assert upd.callback_query.edits[-1]["text"] == REFUSALS["full"]
    assert _db(fresh_store).execute(
        "SELECT COUNT(*) FROM access_asks").fetchone()[0] == ACCESS_ASK_MAX_OPEN


async def test_the_three_refusals_differ():
    assert len(set(REFUSALS.values())) == 3


async def test_failed_admin_dm_stores_no_ask_so_they_can_retry(fresh_store):
    ctx = _ctx(fresh_store)
    ctx.bot.send_message.side_effect = RuntimeError("blocked")
    _, upd = await _send_ask(fresh_store, ctx=ctx)
    assert _db(fresh_store).execute(
        "SELECT COUNT(*) FROM access_asks").fetchone()[0] == 0
    edit = upd.callback_query.edits[-1]
    assert "Couldn't reach the Admin" in edit["text"]
    assert _callbacks(edit["reply_markup"]) == [ACCESS_SEND, ACCESS_CANCEL]


async def test_hub_started_ask_ends_with_the_hub_back_button(fresh_store):
    ctx = _ctx(fresh_store)
    remember_sign_in(ctx, "fresh-tok", "account")
    upd = _stranger_update(ACCESS_SEND)
    await access_ask_send(upd, ctx)
    assert f"{HUB_OPEN}:account" in _callbacks(
        upd.callback_query.edits[-1]["reply_markup"])


# --- Approve ------------------------------------------------------------------

async def _open_ask(store):
    ctx, _ = await _send_ask(store)
    return _db(store).execute("SELECT id FROM access_asks").fetchone()[0], ctx


def _admin_update(data):
    upd = make_update(callback_data=data, user_id=ADMIN_ID)
    upd.effective_chat.type = "private"
    return upd


async def test_approve_lands_on_the_library_picker_with_the_email(fresh_store):
    await _link_admin(fresh_store)
    ask_id, _ = await _open_ask(fresh_store)
    ctx = _ctx(fresh_store)
    upd = _admin_update(f"{ACCESS_APPROVE}:{ask_id}")
    state = await invite_access_start(upd, ctx)
    assert state == INV_LIBS
    assert ctx.user_data["inv_email"] == "new@example.com"
    reply = upd.effective_message.reply_calls[-1]
    assert "new@example.com" in reply["text"] and "Which libraries" in reply["text"]
    assert upd.callback_query.edits == []  # the ask DM is left as it was


async def test_sending_the_invite_marks_approved_and_dms_the_person(fresh_store):
    await _link_admin(fresh_store)
    ask_id, ask_ctx = await _open_ask(fresh_store)
    ctx = _ctx(fresh_store)
    await invite_access_start(_admin_update(f"{ACCESS_APPROVE}:{ask_id}"), ctx)
    version = ctx.user_data["inv_version"]
    await invite_libs_done(_admin_update(f"invd:{version}"), ctx)
    state = await invite_send(_admin_update(f"invgo:{version}"), ctx)
    assert state == ConversationHandler.END
    ctx.bot_data["plex"].invite_to_server.assert_awaited_once()
    assert ctx.bot_data["plex"].invite_to_server.call_args.args[2] == "new@example.com"
    assert _db(fresh_store).execute(
        "SELECT status FROM access_asks").fetchone() == ("approved",)
    dm = ctx.bot.send_message.call_args.kwargs
    assert dm["chat_id"] == STRANGER
    assert "/link" in dm["text"] and "Accept" in dm["text"]
    edited = ctx.bot.edit_message_text.call_args.kwargs
    assert edited["chat_id"] == ADMIN_ID and "Approved" in edited["text"]
    assert "inv_access_ask" not in ctx.user_data


async def test_cancelling_the_invite_leaves_the_ask_open(fresh_store):
    await _link_admin(fresh_store)
    ask_id, _ = await _open_ask(fresh_store)
    ctx = _ctx(fresh_store)
    await invite_access_start(_admin_update(f"{ACCESS_APPROVE}:{ask_id}"), ctx)
    version = ctx.user_data["inv_version"]
    await invite_cancel(_admin_update(f"invx:{version}"), ctx)
    assert _db(fresh_store).execute(
        "SELECT status FROM access_asks").fetchone() == ("open",)
    ctx.bot.send_message.assert_not_called()


async def test_approve_of_a_handled_ask_is_refused(fresh_store):
    await _link_admin(fresh_store)
    ask_id, _ = await _open_ask(fresh_store)
    await fresh_store.decide_access_ask(ask_id, "ignored")
    ctx = _ctx(fresh_store)
    upd = _admin_update(f"{ACCESS_APPROVE}:{ask_id}")
    assert await invite_access_start(upd, ctx) == ConversationHandler.END
    assert "already handled" in upd.callback_query.answers[0][0]


async def test_approve_refuses_non_admin_and_group(fresh_store):
    ask_id, _ = await _open_ask(fresh_store)
    ctx = _ctx(fresh_store)
    upd = make_update(callback_data=f"{ACCESS_APPROVE}:{ask_id}", user_id=STRANGER)
    assert await invite_access_start(upd, ctx) == ConversationHandler.END
    assert upd.callback_query.answers[0] == ("Admin only.", True)
    ctx.bot_data["plex"].get_owned_server.assert_not_called()
    group = make_update(callback_data=f"{ACCESS_APPROVE}:{ask_id}", user_id=ADMIN_ID)
    group.effective_chat.type = "group"
    assert await invite_access_start(group, ctx) == ConversationHandler.END
    assert group.callback_query.answers[0] == ("DM me /admin", True)


def test_approve_is_an_invite_conversation_entry_point():
    patterns = [getattr(ep, "pattern", None)
                for ep in _invite_conversation().entry_points]
    assert any(p is not None and p.pattern == fr"^{ACCESS_APPROVE}:\d+$"
               for p in patterns)


async def test_approve_failure_replies_instead_of_editing_the_ask_dm(fresh_store):
    await _link_admin(fresh_store)
    ask_id, _ = await _open_ask(fresh_store)
    ctx = _ctx(fresh_store)
    ctx.bot_data["plex"].get_owned_server.side_effect = RuntimeError("plex.tv down")
    upd = _admin_update(f"{ACCESS_APPROVE}:{ask_id}")
    await invite_access_start(upd, ctx)
    assert upd.callback_query.edits == []
    assert "Couldn't read your Plex server" in upd.effective_message.reply_calls[-1]["text"]


# --- Ignore -------------------------------------------------------------------

async def test_ignore_edits_the_dm_and_tells_the_person_nothing(fresh_store):
    ask_id, ask_ctx = await _open_ask(fresh_store)
    ctx = _ctx(fresh_store)
    upd = _admin_update(f"{ACCESS_IGNORE}:{ask_id}")
    await access_ask_ignore(upd, ctx)
    assert _db(fresh_store).execute(
        "SELECT status, decided_at FROM access_asks").fetchone()[0] == "ignored"
    edit = upd.callback_query.edits[0]
    assert "Ignored" in edit["text"] and edit.get("reply_markup") is None
    ctx.bot.send_message.assert_not_called()


async def test_ignore_twice_does_nothing_the_second_time(fresh_store):
    ask_id, _ = await _open_ask(fresh_store)
    ctx = _ctx(fresh_store)
    await access_ask_ignore(_admin_update(f"{ACCESS_IGNORE}:{ask_id}"), ctx)
    again = _admin_update(f"{ACCESS_IGNORE}:{ask_id}")
    await access_ask_ignore(again, ctx)
    assert again.callback_query.edits == []


async def test_ignore_refuses_non_admin(fresh_store):
    ask_id, _ = await _open_ask(fresh_store)
    ctx = _ctx(fresh_store)
    upd = make_update(callback_data=f"{ACCESS_IGNORE}:{ask_id}", user_id=STRANGER)
    await access_ask_ignore(upd, ctx)
    assert upd.callback_query.answers[0] == ("Admin only.", True)
    assert upd.callback_query.edits == []
    assert _db(fresh_store).execute(
        "SELECT status FROM access_asks").fetchone() == ("open",)


# --- store --------------------------------------------------------------------

async def test_prune_drops_asks_older_than_30_days(fresh_store):
    await _send_ask(fresh_store, user_id=1)
    await _send_ask(fresh_store, user_id=2)
    with _db(fresh_store) as c:
        c.execute("UPDATE access_asks SET created_at = datetime('now', '-31 days') "
                  "WHERE telegram_id = 1")
    await fresh_store.prune_autofix_history()
    assert _db(fresh_store).execute(
        "SELECT telegram_id FROM access_asks").fetchall() == [(2,)]


async def test_an_approved_ask_can_be_asked_again(fresh_store):
    await _send_ask(fresh_store)
    ask_id = _db(fresh_store).execute("SELECT id FROM access_asks").fetchone()[0]
    await fresh_store.decide_access_ask(ask_id, "approved")
    ctx, _ = await _send_ask(fresh_store)
    ctx.bot.send_message.assert_awaited_once()
    assert _db(fresh_store).execute(
        "SELECT status FROM access_asks").fetchall() == [("open",)]


async def test_plex_account_failure_keeps_the_sign_in_for_a_retry(fresh_store):
    ctx = _ctx(fresh_store)
    ctx.bot_data["plex"].get_account.side_effect = RuntimeError("plex.tv down")
    _, upd = await _send_ask(fresh_store, ctx=ctx)
    assert "Couldn't read your Plex account" in upd.callback_query.edits[-1]["text"]
    assert "access_ask" in ctx.user_data
