"""A slow flow step must not revive a conversation that a newer command ended.

A top-level command force-ends the user's open flows (bot.shared). A step that
was already running keeps going and, when it returns, PTB stores the state it
returned. guard_flow_steps compares a per-user generation counter, bumped by
every force-end, so such a step ends the conversation instead.
"""
from __future__ import annotations

import asyncio
import datetime

from telegram import Bot, Chat, Message, MessageEntity, Update, User
from telegram.ext import (
    ApplicationBuilder, CallbackContext, CommandHandler, ConversationHandler,
    MessageHandler, filters,
)

from bot import tickets
from bot.shared import (
    AWAIT_TICKET_REPLY, flow_step_is_stale, force_end_flow_conversations,
    guard_flow_steps,
)
from tests._handler_harness import make_ctx, make_update

CHAT_ID = USER_ID = 555
_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
_MSG_IDS = iter(range(1, 10_000))
_BOT = Bot("123:abc")
_BOT._bot_user = User(1, "usher", True, username="usherbot")  # skip the network get_me()


def _update(text: str) -> Update:
    chat = Chat(CHAT_ID, "private")
    user = User(USER_ID, "u", False)
    entities = None
    if text.startswith("/"):
        entities = (MessageEntity(MessageEntity.BOT_COMMAND, 0, len(text.split()[0])),)
    msg = Message(next(_MSG_IDS), _NOW, chat, from_user=user, text=text, entities=entities)
    msg.set_bot(_BOT)
    return Update(next(_MSG_IDS), message=msg)


class _Rig:
    """A real ConversationHandler (guarded) with one slow step per scenario."""

    def __init__(self):
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.texts: list[str] = []
        self.app = ApplicationBuilder().token("123:abc").build()
        self.conv = ConversationHandler(
            entry_points=[CommandHandler("go", self.slow_entry),
                          CommandHandler("fast", self.fast_entry)],
            states={
                1: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.slow_step)],
                2: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.take_text)],
            },
            fallbacks=[],
            per_user=True, per_chat=True,
        )
        guard_flow_steps(self.conv)
        self.app.bot_data["flow_convs"] = [self.conv]
        self.key = (CHAT_ID, USER_ID)

    async def slow_entry(self, update, ctx):
        self.started.set()
        await self.release.wait()
        return 1

    async def fast_entry(self, update, ctx):
        return 1

    async def slow_step(self, update, ctx):
        self.started.set()
        await self.release.wait()
        return 2

    async def take_text(self, update, ctx):
        self.texts.append(update.message.text)
        return ConversationHandler.END

    def ctx_for(self, update):
        return CallbackContext.from_update(update, self.app)

    async def feed(self, update):
        """Route one update through the conversation like PTB's dispatcher."""
        check = self.conv.check_update(update)
        if check is None:
            return None
        return await self.conv.handle_update(update, self.app, check, self.ctx_for(update))

    def command_arrives(self, text: str = "/status"):
        upd = _update(text)
        force_end_flow_conversations(self.ctx_for(upd), upd)


async def test_slow_entry_returning_after_force_end_does_not_revive_conversation():
    rig = _Rig()
    task = asyncio.create_task(rig.feed(_update("/go")))
    await asyncio.wait_for(rig.started.wait(), 5)
    rig.command_arrives()
    rig.release.set()
    await task
    assert rig.key not in rig.conv._conversations


async def test_next_text_is_not_swallowed_after_slow_step_was_force_ended():
    rig = _Rig()
    await rig.feed(_update("/fast"))
    assert rig.conv._conversations[rig.key] == 1
    task = asyncio.create_task(rig.feed(_update("typed while slow")))
    await asyncio.wait_for(rig.started.wait(), 5)
    rig.command_arrives()
    assert rig.key not in rig.conv._conversations
    rig.release.set()
    await task
    assert rig.conv._conversations.get(rig.key) is None
    assert rig.conv.check_update(_update("next plain text")) is None


async def test_step_with_no_force_end_keeps_its_own_state():
    rig = _Rig()
    rig.release.set()
    await rig.feed(_update("/go"))
    assert rig.conv._conversations[rig.key] == 1
    await rig.feed(_update("hello"))
    assert rig.conv._conversations[rig.key] == 2


async def test_step_that_ends_its_own_flow_still_ends_it():
    rig = _Rig()
    rig.release.set()
    await rig.feed(_update("/go"))
    await rig.feed(_update("hello"))
    await rig.feed(_update("final"))
    assert rig.texts == ["final"]
    assert rig.key not in rig.conv._conversations


async def test_stale_step_leaves_a_conversation_the_user_restarted_meanwhile():
    rig = _Rig()
    await rig.feed(_update("/fast"))          # state 1
    task = asyncio.create_task(rig.feed(_update("slow text")))
    await asyncio.wait_for(rig.started.wait(), 5)
    rig.command_arrives()
    await rig.feed(_update("/fast"))          # the user starts the flow again
    assert rig.conv._conversations[rig.key] == 1
    rig.release.set()
    await task                                # slow_step would store state 2
    assert rig.conv._conversations[rig.key] == 1


async def test_force_end_inside_a_step_does_not_make_that_step_stale():
    """Entry points call force_end_flow_conversations themselves, then return
    their own state: that state must stand."""
    app = ApplicationBuilder().token("123:abc").build()

    async def entry(update, ctx):
        force_end_flow_conversations(ctx, update)
        assert not flow_step_is_stale(ctx)
        return 1

    async def step(update, ctx):
        return ConversationHandler.END

    conv = ConversationHandler(
        entry_points=[CommandHandler("go", entry)],
        states={1: [MessageHandler(filters.TEXT, step)]},
        fallbacks=[], per_user=True, per_chat=True,
    )
    guard_flow_steps(conv)
    app.bot_data["flow_convs"] = [conv]
    upd = _update("/go")
    check = conv.check_update(upd)
    await conv.handle_update(upd, app, check, CallbackContext.from_update(upd, app))
    assert conv._conversations[(CHAT_ID, USER_ID)] == 1


async def test_step_overtaken_before_its_own_force_end_stays_overtaken():
    """A command arrives mid-step, then the step itself calls the force-end
    helper: its own call must not forgive the earlier overtake."""
    app = ApplicationBuilder().token("123:abc").build()
    release, started = asyncio.Event(), asyncio.Event()

    async def entry(update, ctx):
        started.set()
        await release.wait()
        force_end_flow_conversations(ctx, update)
        return 1

    conv = ConversationHandler(
        entry_points=[CommandHandler("go", entry)],
        states={1: [MessageHandler(filters.TEXT, entry)]},
        fallbacks=[], per_user=True, per_chat=True,
    )
    guard_flow_steps(conv)
    app.bot_data["flow_convs"] = [conv]
    upd = _update("/go")
    check = conv.check_update(upd)
    ctx = CallbackContext.from_update(upd, app)
    task = asyncio.create_task(conv.handle_update(upd, app, check, ctx))
    await asyncio.wait_for(started.wait(), 5)
    other = _update("/status")
    force_end_flow_conversations(CallbackContext.from_update(other, app), other)
    release.set()
    await task
    assert (CHAT_ID, USER_ID) not in conv._conversations


async def test_guard_covers_entry_points_states_fallbacks_and_timeout():
    async def cb(update, ctx):
        return None
    conv = ConversationHandler(
        entry_points=[CommandHandler("a", cb)],
        states={1: [CommandHandler("b", cb)],
                ConversationHandler.TIMEOUT: [CommandHandler("c", cb)]},
        fallbacks=[CommandHandler("cancel", cb)],
    )
    guard_flow_steps(conv)
    guard_flow_steps(conv)  # idempotent
    handlers = (list(conv.entry_points) + list(conv.fallbacks)
                + [h for hs in conv.states.values() for h in hs])
    assert len(handlers) == 4
    for h in handlers:
        assert h.callback is not cb
        assert h.callback.__wrapped__ is cb


# --- Ticket reply target ----------------------------------------------------

async def _slow_token(release, started):
    started.set()
    await release.wait()
    return True, "tok", False


async def test_reply_start_that_was_force_ended_does_not_set_reply_target(monkeypatch):
    release, started = asyncio.Event(), asyncio.Event()

    async def token_for(ctx, uid):
        return await _slow_token(release, started)

    monkeypatch.setattr(tickets, "token_for", token_for)
    conv = tickets._ticket_conversation()
    guard_flow_steps(conv)
    reply_start = conv.entry_points[0].callback
    upd = make_update(callback_data="tkr:42", user_id=999)
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["flow_convs"] = []
    task = asyncio.create_task(reply_start(upd, ctx))
    await asyncio.wait_for(started.wait(), 5)
    force_end_flow_conversations(ctx, make_update(callback_data="x", user_id=999))
    release.set()
    state = await task
    assert state == ConversationHandler.END
    assert "tk_reply_id" not in ctx.user_data
    assert "tk_close_after" not in ctx.user_data
    assert upd.callback_query.message.reply_calls == []


async def test_close_with_comment_start_that_was_force_ended_does_not_set_reply_target(monkeypatch):
    release, started = asyncio.Event(), asyncio.Event()

    async def require_admin(q, ctx, action_label=""):
        started.set()
        await release.wait()
        return True

    monkeypatch.setattr(tickets, "require_admin", require_admin)
    conv = tickets._ticket_conversation()
    guard_flow_steps(conv)
    close_start = conv.entry_points[1].callback
    upd = make_update(callback_data="tkcc:42", user_id=999)
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["flow_convs"] = []
    task = asyncio.create_task(close_start(upd, ctx))
    await asyncio.wait_for(started.wait(), 5)
    force_end_flow_conversations(ctx, make_update(callback_data="x", user_id=999))
    release.set()
    state = await task
    assert state == ConversationHandler.END
    assert "tk_reply_id" not in ctx.user_data
    assert upd.callback_query.message.reply_calls == []


async def test_reply_start_without_force_end_still_sets_reply_target():
    conv = tickets._ticket_conversation()
    guard_flow_steps(conv)
    upd = make_update(callback_data="tkr:42", user_id=999)
    ctx = make_ctx(admin_id=999)
    state = await conv.entry_points[0].callback(upd, ctx)
    assert state == AWAIT_TICKET_REPLY
    assert ctx.user_data["tk_reply_id"] == 42


# --- Link flow PIN polling marker ---------------------------------------------

def _pin_plex(ctx, release, started):
    from types import SimpleNamespace

    async def request_pin(strong=True):
        started.set()
        await release.wait()
        return SimpleNamespace(id=1, auth_url="https://example.invalid/auth", code="abcd")

    ctx.bot_data["plex"].request_pin = request_pin


async def test_platform_step_force_ended_during_pin_request_starts_no_poll():
    from bot import link_flow
    release, started = asyncio.Event(), asyncio.Event()
    conv = link_flow._link_conversation()
    guard_flow_steps(conv)
    step = conv.states[link_flow.AWAIT_PLATFORM_CHOICE][0].callback
    upd = make_update(callback_data="lnkplat:desktop", user_id=999)
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["flow_convs"] = []
    _pin_plex(ctx, release, started)
    task = asyncio.create_task(step(upd, ctx))
    await asyncio.wait_for(started.wait(), 5)
    force_end_flow_conversations(ctx, make_update(callback_data="x", user_id=999))
    release.set()
    state = await asyncio.wait_for(task, 5)
    assert state == ConversationHandler.END
    assert "link_active_loop" not in ctx.user_data
    assert upd.callback_query.edits == []


async def test_didnt_work_tap_force_ended_during_pin_request_starts_no_poll():
    from bot import link_flow
    release, started = asyncio.Event(), asyncio.Event()
    upd = make_update(callback_data=link_flow.LINK_HELP, user_id=999)
    ctx = make_ctx(admin_id=999, user_data={"link_active_loop": "old"})
    ctx.bot_data["flow_convs"] = []
    _pin_plex(ctx, release, started)
    task = asyncio.create_task(link_flow.cmd_link_didnt_work(upd, ctx))
    await asyncio.wait_for(started.wait(), 5)
    force_end_flow_conversations(ctx, make_update(callback_data="x", user_id=999))
    release.set()
    await asyncio.wait_for(task, 5)
    assert "link_active_loop" not in ctx.user_data
    assert upd.callback_query.edits == []


async def test_didnt_work_pin_failure_after_force_end_leaves_no_retry_marker(monkeypatch):
    from unittest.mock import AsyncMock
    from bot import link_flow
    from bot.hub import set_hub_origin
    release, started = asyncio.Event(), asyncio.Event()
    end = AsyncMock()
    monkeypatch.setattr(link_flow, "end_flow", end)
    upd = make_update(callback_data=link_flow.LINK_HELP, user_id=999)
    ctx = make_ctx(admin_id=999, user_data={"link_active_loop": "old"})
    ctx.bot_data["flow_convs"] = []
    set_hub_origin(ctx, "account")

    async def request_pin(strong=True):
        started.set()
        await release.wait()
        raise RuntimeError("plex down")

    ctx.bot_data["plex"].request_pin = request_pin
    task = asyncio.create_task(link_flow.cmd_link_didnt_work(upd, ctx))
    await asyncio.wait_for(started.wait(), 5)
    force_end_flow_conversations(ctx, make_update(callback_data="x", user_id=999))
    release.set()
    await asyncio.wait_for(task, 5)
    assert link_flow.LINK_RETRY_KEY not in ctx.user_data
    end.assert_not_awaited()  # no failure message for a flow the user left


async def test_platform_step_pin_failure_after_force_end_sends_no_failure_message(monkeypatch):
    from unittest.mock import AsyncMock
    from bot import link_flow
    release, started = asyncio.Event(), asyncio.Event()
    end = AsyncMock()
    monkeypatch.setattr(link_flow, "end_flow", end)
    conv = link_flow._link_conversation()
    guard_flow_steps(conv)
    step = conv.states[link_flow.AWAIT_PLATFORM_CHOICE][0].callback
    upd = make_update(callback_data="lnkplat:desktop", user_id=999)
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["flow_convs"] = []

    async def request_pin(strong=True):
        started.set()
        await release.wait()
        raise RuntimeError("plex down")

    ctx.bot_data["plex"].request_pin = request_pin
    task = asyncio.create_task(step(upd, ctx))
    await asyncio.wait_for(started.wait(), 5)
    force_end_flow_conversations(ctx, make_update(callback_data="x", user_id=999))
    release.set()
    assert await asyncio.wait_for(task, 5) == ConversationHandler.END
    end.assert_not_awaited()


# --- Wiring over the real app -------------------------------------------------

def _all_conversations(app):
    return [h for group in app.handlers.values() for h in group
            if isinstance(h, ConversationHandler)]


def test_every_step_of_every_registered_conversation_is_guarded(monkeypatch):
    """Builds the app the way production does. A conversation added without
    guard_flow_steps (or left out of flow_convs) fails here."""
    from pathlib import Path
    from unittest.mock import MagicMock
    import bot.app as app_mod

    monkeypatch.setattr(app_mod, "_build_clients_from_settings", lambda app: None)
    settings = MagicMock()
    settings.settings.telegram_bot_token = "123:abc"
    settings.settings.admin_telegram_id = 1
    app = app_mod._build_app(settings, b"secret", MagicMock(), MagicMock(),
                             Path("."), Path("settings.json"), ":memory:",
                             8080, "127.0.0.1")
    convs = _all_conversations(app)
    assert len(convs) == 6
    assert all(c in app.bot_data["flow_convs"] for c in convs)
    for conv in convs:
        handlers = (list(conv.entry_points) + list(conv.fallbacks)
                    + [h for hs in conv.states.values() for h in hs])
        assert handlers
        for h in handlers:
            assert getattr(h.callback, "_flow_guarded", False), (conv.name, h)
