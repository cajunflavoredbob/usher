"""Tests for the "most recent command wins" flow-reset gate (bot.shared).

A top-level command must abandon any in-progress conversation so a half-
finished /issue can't intercept text meant for the next flow.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import MessageEntity
from telegram.ext import CommandHandler, ConversationHandler

from bot.shared import command_name, reset_stale_flows

CHAT_ID = 555
USER_ID = 555  # DM: chat_id == user_id


async def _noop(update, ctx):
    return None


def _make_conv():
    """A real per_user+per_chat ConversationHandler we can seed by hand."""
    return ConversationHandler(
        entry_points=[CommandHandler("x", _noop)],
        states={1: [CommandHandler("y", _noop)]},
        fallbacks=[],
        per_user=True,
        per_chat=True,
    )


def _update(text: str | None, *, is_command: bool):
    entities = ()
    if is_command and text:
        entities = (SimpleNamespace(type=MessageEntity.BOT_COMMAND, offset=0,
                                    length=len(text.split()[0])),)
    msg = SimpleNamespace(text=text, entities=entities, reply_text=AsyncMock())
    return SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=CHAT_ID),
        effective_user=SimpleNamespace(id=USER_ID),
    )


def _ctx(convs, user_data=None, names=None):
    bot_data = {"flow_convs": convs}
    if names:
        bot_data["conversations"] = names
    return SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data),
        user_data=user_data if user_data is not None else {},
    )


# --- command_name -----------------------------------------------------------

def test_command_name_parses_bot_command():
    assert command_name(_update("/tickets", is_command=True).effective_message) == "tickets"


def test_command_name_strips_at_botname():
    msg = SimpleNamespace(
        text="/issue@UsherBot foo",
        entities=(SimpleNamespace(type=MessageEntity.BOT_COMMAND, offset=0, length=16),),
    )
    assert command_name(msg) == "issue"


def test_command_name_none_for_plain_text():
    assert command_name(_update("just some words", is_command=False).effective_message) is None


# --- reset_stale_flows ------------------------------------------------------

async def test_command_ends_active_conversation_and_cancels_timeout():
    conv = _make_conv()
    key = (CHAT_ID, USER_ID)
    conv._conversations[key] = 1  # pretend the user is parked in state 1
    removed = []
    conv.timeout_jobs[key] = SimpleNamespace(schedule_removal=lambda: removed.append(True))

    ud = {"tk_reply_id": 7, "tk_close_after": True}
    await reset_stale_flows(_update("/tickets", is_command=True), _ctx([conv], ud))

    assert key not in conv._conversations  # flow abandoned
    assert removed == [True]              # timeout job cancelled
    assert "tk_reply_id" not in ud        # free-text markers cleared
    assert "tk_close_after" not in ud


async def test_plain_text_leaves_conversation_intact():
    conv = _make_conv()
    key = (CHAT_ID, USER_ID)
    conv._conversations[key] = 1
    await reset_stale_flows(_update("solo a star wars story", is_command=False), _ctx([conv]))
    assert conv._conversations[key] == 1  # untouched


async def test_cancel_command_is_exempt():
    conv = _make_conv()
    key = (CHAT_ID, USER_ID)
    conv._conversations[key] = 1
    await reset_stale_flows(_update("/cancel", is_command=True), _ctx([conv]))
    assert conv._conversations[key] == 1  # left alive so the convo's /cancel fallback fires


async def test_no_active_conversation_is_noop():
    conv = _make_conv()  # nothing seeded
    # Should not raise even though this user has no conversation key.
    await reset_stale_flows(_update("/status", is_command=True), _ctx([conv]))
    assert (CHAT_ID, USER_ID) not in conv._conversations


# --- the notice -------------------------------------------------------------

def _live_conv():
    conv = _make_conv()
    conv._conversations[(CHAT_ID, USER_ID)] = 1
    return conv


async def test_ending_a_named_flow_says_so():
    conv = _live_conv()
    upd = _update("/help", is_command=True)
    await reset_stale_flows(upd, _ctx([conv], names={"issue": conv}))
    upd.effective_message.reply_text.assert_awaited_once_with(
        "Stopped your unfinished /issue.")


async def test_ending_an_unnamed_flow_uses_generic_notice():
    conv = _live_conv()  # button-started flow: not in the command map
    upd = _update("/help", is_command=True)
    await reset_stale_flows(upd, _ctx([conv], names={"resolve": conv}))
    upd.effective_message.reply_text.assert_awaited_once_with(
        "Stopped what you were in the middle of.")


async def test_no_notice_when_nothing_was_running():
    upd = _update("/help", is_command=True)
    await reset_stale_flows(upd, _ctx([_make_conv()], names={}))
    upd.effective_message.reply_text.assert_not_awaited()


async def test_no_notice_for_cancel_or_plain_text():
    conv = _live_conv()
    for text, is_cmd in (("/cancel", True), ("hello", False)):
        upd = _update(text, is_command=is_cmd)
        await reset_stale_flows(upd, _ctx([conv], names={"issue": conv}))
        upd.effective_message.reply_text.assert_not_awaited()


async def test_restarting_the_same_flow_is_silent():
    conv = _live_conv()
    upd = _update("/issue", is_command=True)
    await reset_stale_flows(upd, _ctx([conv], names={"issue": conv}))
    upd.effective_message.reply_text.assert_not_awaited()


async def test_two_live_flows_get_the_generic_notice():
    a, b = _live_conv(), _live_conv()
    names = {"link": a, "resolve": b}
    for text in ("/help", "/link"):
        upd = _update(text, is_command=True)
        a._conversations[(CHAT_ID, USER_ID)] = 1
        b._conversations[(CHAT_ID, USER_ID)] = 1
        await reset_stale_flows(upd, _ctx([a, b], names=names))
        upd.effective_message.reply_text.assert_awaited_once_with(
            "Stopped what you were in the middle of.")


def _cmd_msg(text, length, bot_username):
    return SimpleNamespace(
        text=text,
        entities=(SimpleNamespace(type=MessageEntity.BOT_COMMAND, offset=0,
                                  length=length),),
        get_bot=lambda: SimpleNamespace(username=bot_username),
    )


def test_command_for_another_bot_is_not_ours():
    assert command_name(_cmd_msg("/weather@OtherBot", 17, "UsherBot")) is None


def test_command_for_this_bot_matches_any_case():
    assert command_name(_cmd_msg("/issue@usherbot", 15, "UsherBot")) == "issue"
