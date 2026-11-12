"""Tests for the revoked-token recovery entry point (bot.link_flow.cmd_relink):
one tap clears the dead token and drops the user into the platform-choice
step of the existing link conversation, skipping the consent step."""
from __future__ import annotations

from telegram.ext import ConversationHandler

from bot.callback_prefixes import HUB_RETRY, LINK_HELP, LINK_PLATFORM, RELINK
from bot.link_flow import (
    _link_conversation, cmd_link_didnt_work, cmd_relink, hub_retry_linkplat,
)
from bot.shared import AWAIT_PLATFORM_CHOICE
from tests._handler_harness import make_ctx, make_update


async def test_relink_enters_platform_choice():
    upd = make_update(callback_data=RELINK, user_id=42)
    ctx = make_ctx(admin_id=999)
    state = await cmd_relink(upd, ctx)
    assert state == AWAIT_PLATFORM_CHOICE
    ctx.bot_data["store"].unlink.assert_not_called()
    edit = upd.callback_query.edits[0]
    assert "signed back in" in edit["text"]
    callbacks = [b.callback_data
                 for row in edit["reply_markup"].inline_keyboard for b in row]
    assert f"{LINK_PLATFORM}:desktop" in callbacks
    assert f"{LINK_PLATFORM}:mobile" in callbacks


def test_relink_is_conversation_entry_point():
    """The button must enter the link conversation itself, or the
    platform-choice tap that follows would fall on the floor."""
    conv = _link_conversation()
    patterns = [getattr(ep, "pattern", None) for ep in conv.entry_points]
    assert any(p is not None and p.pattern == f"^{RELINK}$" for p in patterns)


async def test_relink_keeps_existing_link_until_new_one_is_stored():
    """A plex.tv outage after the tap must not leave a working user unlinked:
    the old link is only replaced by _finalize_link's upsert."""
    upd = make_update(callback_data=RELINK, user_id=42)
    ctx = make_ctx(admin_id=999)
    await cmd_relink(upd, ctx)
    ctx.bot_data["store"].unlink.assert_not_called()


def _in_group(upd):
    upd.effective_chat.type = "group"
    return upd


async def test_relink_refused_in_group():
    upd = _in_group(make_update(callback_data=RELINK, user_id=42))
    ctx = make_ctx(admin_id=999)
    state = await cmd_relink(upd, ctx)
    assert state == ConversationHandler.END
    assert upd.callback_query.edits == []
    assert upd.message.reply_calls[0]["text"] == "Please DM me to link your account."


async def test_hub_retry_linkplat_refused_in_group():
    upd = _in_group(make_update(
        callback_data=f"{HUB_RETRY}:linkplat:desktop", user_id=42))
    ctx = make_ctx(admin_id=999)
    state = await hub_retry_linkplat(upd, ctx)
    assert state == ConversationHandler.END
    ctx.bot_data["plex"].request_pin.assert_not_called()
    assert upd.message.reply_calls[0]["text"] == "Please DM me to link your account."


async def test_didnt_work_refused_in_group():
    upd = _in_group(make_update(callback_data=LINK_HELP, user_id=42))
    ctx = make_ctx(admin_id=999)
    ctx.user_data["link_active_loop"] = "abc"
    await cmd_link_didnt_work(upd, ctx)
    ctx.bot_data["plex"].request_pin.assert_not_called()
    assert upd.message.reply_calls[0]["text"] == "Please DM me to link your account."


async def test_didnt_work_without_live_link_flow_does_nothing():
    upd = make_update(callback_data=LINK_HELP, user_id=42)
    ctx = make_ctx(admin_id=999)
    await cmd_link_didnt_work(upd, ctx)
    ctx.bot_data["plex"].request_pin.assert_not_called()
    assert upd.callback_query.edits == []
    assert upd.callback_query.answers[-1][0]


async def _run_strong_pin(monkeypatch, poll_result):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from bot import link_flow
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["plex"].request_pin.return_value = SimpleNamespace(
        id=1, auth_url="https://plex.example.com/auth", code="abcd")
    monkeypatch.setattr(link_flow, "_poll_with_cancel",
                        AsyncMock(return_value=poll_result))
    monkeypatch.setattr(link_flow, "_finalize_link", AsyncMock())
    upd = make_update(callback_data=f"{HUB_RETRY}:linkplat:desktop", user_id=42)
    await hub_retry_linkplat(upd, ctx)
    return ctx


async def test_strong_pin_success_clears_link_active_loop(monkeypatch):
    ctx = await _run_strong_pin(monkeypatch, "tok")
    assert "link_active_loop" not in ctx.user_data


async def test_strong_pin_timeout_clears_link_active_loop(monkeypatch):
    ctx = await _run_strong_pin(monkeypatch, None)
    assert "link_active_loop" not in ctx.user_data


async def _weak_timeout(monkeypatch, ctx):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from bot import link_flow
    ctx.bot_data["plex"].request_pin.return_value = SimpleNamespace(
        id=2, auth_url="https://plex.example.com/auth", code="wxyz")
    monkeypatch.setattr(link_flow, "_poll_with_cancel",
                        AsyncMock(return_value=None))
    monkeypatch.setattr(link_flow, "_finalize_link", AsyncMock())
    ctx.user_data["link_active_loop"] = "old"
    await cmd_link_didnt_work(make_update(callback_data=LINK_HELP, user_id=42), ctx)


async def test_try_again_after_weak_pin_timeout_issues_new_code(monkeypatch):
    ctx = make_ctx(admin_id=999)
    await _weak_timeout(monkeypatch, ctx)
    assert "link_active_loop" not in ctx.user_data
    ctx.bot_data["plex"].request_pin.reset_mock()
    upd = make_update(callback_data=LINK_HELP, user_id=42)
    await cmd_link_didnt_work(upd, ctx)
    ctx.bot_data["plex"].request_pin.assert_awaited_once()
    assert "WXYZ" in upd.callback_query.edits[0]["text"]


async def test_stray_tap_without_any_marker_is_refused(monkeypatch):
    ctx = make_ctx(admin_id=999)
    await _weak_timeout(monkeypatch, ctx)
    ctx.user_data.clear()
    ctx.bot_data["plex"].request_pin.reset_mock()
    await cmd_link_didnt_work(make_update(callback_data=LINK_HELP, user_id=42), ctx)
    ctx.bot_data["plex"].request_pin.assert_not_called()

