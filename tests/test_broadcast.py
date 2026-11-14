"""Admin Broadcast: the Admin Hub button, the preview, the rate-spaced job
that sends to every Linked user but the Admin, and resuming after a restart."""
from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import ConversationHandler

import bot.broadcast as bc
from bot.admin_hub import render_admin_hub
from bot.broadcast import (HEADER, _broadcast_conversation, broadcast_cancel,
                           broadcast_send, broadcast_start, broadcast_text,
                           resume_broadcasts, run_broadcast)
from bot.callback_prefixes import (BROADCAST_CANCEL, BROADCAST_SEND,
                                   BROADCAST_START, HUB_OPEN)
from bot.hub import hub_rows
from bot.shared import BC_CONFIRM, BC_TEXT
from const import BROADCAST_MAX_CHARS

from tests._handler_harness import make_ctx, make_update

ADMIN = 999


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.fixture(autouse=True)
def _no_spacing(monkeypatch):
    monkeypatch.setattr(bc, "BROADCAST_SEND_INTERVAL_S", 0)


def _ctx(store, *, user_data=None):
    ctx = make_ctx(admin_id=ADMIN, user_data=user_data)
    ctx.bot_data["store"] = store
    ctx.bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=700, chat_id=1))
    ctx.bot.edit_message_text = AsyncMock(
        return_value=SimpleNamespace(message_id=200, chat_id=100))
    ctx.application.bot = ctx.bot
    return ctx


async def _link(store, *ids):
    for tid in ids:
        await store.link_with_plex(telegram_id=tid, seerr_id=tid,
                                   seerr_display=f"u{tid}", plex_token="t",
                                   plex_uuid="u", plex_username=f"p{tid}")


def _db(store):
    return sqlite3.connect(store.path)


def _tap(data, user_id=ADMIN):
    return make_update(callback_data=data, user_id=user_id)


async def _drain(ctx):
    for task in list(ctx.application.bot_data.get("broadcast_tasks") or ()):
        await task


async def _preview(store, text="Maintenance at 9pm *tonight* <b>x</b>"):
    ctx = _ctx(store)
    upd = make_update(text=text, user_id=ADMIN)
    state = await broadcast_text(upd, ctx)
    return ctx, upd, state


# --- the Hub button and the prompt -------------------------------------------

async def test_hub_has_a_broadcast_button_and_at_most_two_per_row():
    for sab in (None, object()):
        ctx = make_ctx(admin_id=ADMIN)
        ctx.bot_data["sabnzbd"] = sab
        view = await render_admin_hub(None, ctx)
        assert ("📣 Broadcast", BROADCAST_START) in [
            (b.text, b.callback_data) for b in view.buttons]
        assert all(len(r) <= 2 for r in hub_rows(view.buttons))


async def test_hub_button_edits_into_the_prompt(fresh_store):
    ctx = _ctx(fresh_store)
    upd = _tap(BROADCAST_START)
    assert await broadcast_start(upd, ctx) == BC_TEXT
    assert "Linked user" in upd.callback_query.edits[0]["text"]


async def test_non_admin_is_refused(fresh_store):
    ctx = _ctx(fresh_store)
    upd = _tap(BROADCAST_START, user_id=42)
    assert await broadcast_start(upd, ctx) == ConversationHandler.END
    assert upd.callback_query.answers[-1] == ("Admin only.", True)
    assert upd.callback_query.edits == []


async def test_group_chat_is_refused(fresh_store):
    ctx = _ctx(fresh_store)
    upd = _tap(BROADCAST_START)
    upd.effective_chat.type = "group"
    assert await broadcast_start(upd, ctx) == ConversationHandler.END
    assert upd.callback_query.answers[-1] == ("DM me /admin", True)


async def test_text_and_send_steps_refuse_a_non_admin_too(fresh_store):
    await _link(fresh_store, 1)
    ctx = _ctx(fresh_store, user_data={"bc_text": "hi"})
    upd = _tap(BROADCAST_SEND, user_id=42)
    assert await broadcast_send(upd, ctx) == ConversationHandler.END
    typed = make_update(text="hi", user_id=42)
    assert await broadcast_text(typed, ctx) == ConversationHandler.END
    assert typed.effective_message.reply_calls[0]["text"] == "Admin only."
    ctx.bot.send_message.assert_not_called()
    assert _db(fresh_store).execute(
        "SELECT COUNT(*) FROM broadcasts").fetchone()[0] == 0


async def test_group_chat_is_refused_at_every_step(fresh_store):
    ctx = _ctx(fresh_store, user_data={"bc_text": "hi"})
    for call, upd in (
            (broadcast_send, _tap(BROADCAST_SEND)),
            (broadcast_cancel, _tap(BROADCAST_CANCEL)),
            (broadcast_text, make_update(text="hi", user_id=ADMIN))):
        upd.effective_chat.type = "group"
        assert await call(upd, ctx) == ConversationHandler.END
    ctx.bot.send_message.assert_not_called()


async def test_typed_cancel_sends_nothing(fresh_store):
    ctx = _ctx(fresh_store, user_data={"bc_text": "hi"})
    upd = make_update(text="/cancel", user_id=ADMIN)
    assert await broadcast_cancel(upd, ctx) == ConversationHandler.END
    reply = upd.effective_message.reply_calls[0]
    assert "Nothing was sent" in reply["text"]
    assert "bc_text" not in ctx.user_data


# --- preview and cancel ---------------------------------------------------------

async def test_preview_shows_header_text_and_count_without_the_admin(
        fresh_store):
    await _link(fresh_store, ADMIN, 1, 2)
    ctx, upd, state = await _preview(fresh_store)
    assert state == BC_CONFIRM
    reply = upd.effective_message.reply_calls[0]
    assert HEADER in reply["text"] and "*tonight* <b>x</b>" in reply["text"]
    assert "2 Linked users" in reply["text"]
    assert "parse_mode" not in reply
    assert _callbacks(reply["reply_markup"]) == [BROADCAST_SEND,
                                                 BROADCAST_CANCEL]


async def test_overlong_text_is_refused(fresh_store):
    ctx = _ctx(fresh_store)
    upd = make_update(text="x" * (BROADCAST_MAX_CHARS + 1), user_id=ADMIN)
    assert await broadcast_text(upd, ctx) == BC_TEXT
    assert "bc_text" not in ctx.user_data


async def test_cancel_sends_nothing(fresh_store):
    await _link(fresh_store, 1)
    ctx, _upd, _ = await _preview(fresh_store)
    upd = _tap(BROADCAST_CANCEL)
    assert await broadcast_cancel(upd, ctx) == ConversationHandler.END
    edit = upd.callback_query.edits[0]
    assert "Nothing was sent" in edit["text"]
    assert _callbacks(edit["reply_markup"]) == [f"{HUB_OPEN}:admin"]
    ctx.bot.send_message.assert_not_called()
    assert _db(fresh_store).execute(
        "SELECT COUNT(*) FROM broadcasts").fetchone()[0] == 0


# --- Send and the report --------------------------------------------------------

async def test_send_reaches_every_linked_user_but_the_admin_with_exact_text(
        fresh_store):
    await _link(fresh_store, ADMIN, 1, 2, 3)
    text = "Down 9pm *tonight* <b>x</b> [a](b) _c_"
    ctx, _upd, _ = await _preview(fresh_store, text)
    upd = _tap(BROADCAST_SEND)
    assert await broadcast_send(upd, ctx) == ConversationHandler.END
    await _drain(ctx)
    calls = ctx.bot.send_message.await_args_list
    assert sorted(c.kwargs["chat_id"] for c in calls) == [1, 2, 3]
    for c in calls:
        assert c.kwargs["text"] == f"{HEADER}\n\n{text}"
        assert "parse_mode" not in c.kwargs


async def test_report_edits_the_preview_with_matching_counts(fresh_store):
    await _link(fresh_store, 1, 2, 3)
    ctx, _upd, _ = await _preview(fresh_store)

    async def send(chat_id, text, **kw):
        if chat_id == 2:
            raise Forbidden("Forbidden: bot was blocked by the user")
        return SimpleNamespace(message_id=1, chat_id=chat_id)
    ctx.bot.send_message = AsyncMock(side_effect=send)
    upd = _tap(BROADCAST_SEND)
    await broadcast_send(upd, ctx)
    await _drain(ctx)
    kw = ctx.bot.edit_message_text.await_args.kwargs
    assert kw["message_id"] == upd.callback_query.message.message_id
    assert "Sent: 2" in kw["text"] and "Couldn't reach: 1" in kw["text"]
    assert _callbacks(kw["reply_markup"]) == [f"{HUB_OPEN}:admin"]
    rows = dict(_db(fresh_store).execute(
        "SELECT telegram_id, status FROM broadcast_recipients").fetchall())
    assert rows == {1: "sent", 2: "failed", 3: "sent"}


async def test_flood_wait_is_waited_out_and_retried(fresh_store, monkeypatch):
    await _link(fresh_store, 1)
    ctx, _upd, _ = await _preview(fresh_store)
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        sleeps.append(s)
        await real_sleep(0)
    monkeypatch.setattr(bc.asyncio, "sleep", fake_sleep)
    ctx.bot.send_message = AsyncMock(side_effect=[
        RetryAfter(3), SimpleNamespace(message_id=1, chat_id=1)])
    await broadcast_send(_tap(BROADCAST_SEND), ctx)
    await _drain(ctx)
    assert 3 in sleeps
    assert _db(fresh_store).execute(
        "SELECT status FROM broadcast_recipients").fetchone()[0] == "sent"


async def test_sends_are_spaced_by_the_interval(fresh_store, monkeypatch):
    await _link(fresh_store, 1, 2, 3)
    monkeypatch.setattr(bc, "BROADCAST_SEND_INTERVAL_S", 0.04)
    ctx, _upd, _ = await _preview(fresh_store)
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        sleeps.append(s)
        await real_sleep(0)
    monkeypatch.setattr(bc.asyncio, "sleep", fake_sleep)
    await broadcast_send(_tap(BROADCAST_SEND), ctx)
    await _drain(ctx)
    assert sleeps == [0.04, 0.04, 0.04]


async def test_send_with_no_other_linked_user_says_so(fresh_store):
    await _link(fresh_store, ADMIN)
    ctx, _upd, _ = await _preview(fresh_store)
    upd = _tap(BROADCAST_SEND)
    await broadcast_send(upd, ctx)
    assert "No Linked users" in upd.callback_query.edits[0]["text"]
    ctx.bot.send_message.assert_not_called()


# --- restart ----------------------------------------------------------------------

async def test_resumed_job_skips_recipients_already_sent(fresh_store):
    bid = await fresh_store.create_broadcast("hello", 100, 200, [1, 2, 3])
    await fresh_store.mark_broadcast_recipient(bid, 1, "sent")
    await fresh_store.mark_broadcast_recipient(bid, 2, "failed")
    ctx = _ctx(fresh_store)
    await resume_broadcasts(ctx.application)
    await _drain(ctx)
    assert [c.kwargs["chat_id"] for c in ctx.bot.send_message.await_args_list
            ] == [3]
    kw = ctx.bot.edit_message_text.await_args.kwargs
    assert "Sent: 2" in kw["text"] and "Couldn't reach: 1" in kw["text"]


async def test_a_finished_broadcast_is_not_resumed_or_reported_twice(
        fresh_store):
    bid = await fresh_store.create_broadcast("hello", 100, 200, [1])
    await fresh_store.mark_broadcast_recipient(bid, 1, "sent")
    ctx = _ctx(fresh_store)
    await run_broadcast(ctx.application, bid)
    await run_broadcast(ctx.application, bid)
    await resume_broadcasts(ctx.application)
    await _drain(ctx)
    assert ctx.bot.edit_message_text.await_count == 1
    ctx.bot.send_message.assert_not_called()


async def test_report_falls_back_to_a_new_message(fresh_store):
    bid = await fresh_store.create_broadcast("hello", 100, 200, [])
    ctx = _ctx(fresh_store)
    ctx.bot.edit_message_text = AsyncMock(side_effect=BadRequest("gone"))
    await run_broadcast(ctx.application, bid)
    assert "Broadcast done" in ctx.bot.send_message.await_args.kwargs["text"]


# --- store ---------------------------------------------------------------------------

async def test_old_finished_broadcasts_are_pruned(fresh_store):
    old = await fresh_store.create_broadcast("old", 1, 1, [5])
    new = await fresh_store.create_broadcast("new", 1, 1, [6])
    live = await fresh_store.create_broadcast("live", 1, 1, [7])
    await fresh_store.finish_broadcast(old)
    await fresh_store.finish_broadcast(new)
    db = _db(fresh_store)
    db.execute("UPDATE broadcasts SET created_at = datetime('now', '-40 days')"
               " WHERE id IN (?, ?)", (old, live))
    db.commit()
    await fresh_store.prune_autofix_history()
    ids = {r[0] for r in _db(fresh_store).execute(
        "SELECT id FROM broadcasts").fetchall()}
    assert ids == {new, live}
    assert _db(fresh_store).execute(
        "SELECT COUNT(*) FROM broadcast_recipients WHERE broadcast_id = ?",
        (old,)).fetchone()[0] == 0


def test_conversation_wires_the_states():
    conv = _broadcast_conversation()
    assert set(conv.states) >= {BC_TEXT, BC_CONFIRM}
