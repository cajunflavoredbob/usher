"""Tests for /invite and /uninvite: gates, the library multi-select,
confirmation, the Plex calls, and the uninvite confirm path."""
from __future__ import annotations

from unittest.mock import AsyncMock

from telegram.ext import ConversationHandler

from plex import PlexLibrarySection, PlexShare
from bot.invite_flow import (
    cmd_uninvite,
    invite_libs_done,
    invite_send,
    invite_start,
    invite_toggle_lib,
    uninvite_go,
    invite_cancel,
)
from bot.shared import INV_CONFIRM, INV_EMAIL, INV_LIBS

from tests._handler_harness import make_ctx, make_mapping, make_update

ADMIN_ID = 999


def _sections():
    return [PlexLibrarySection(id=101, title="Movies", type="movie"),
            PlexLibrarySection(id=102, title="TV Shows", type="show"),
            PlexLibrarySection(id=103, title="Movies 4K", type="movie")]


def _plex_ctx(**kwargs):
    ctx = make_ctx(admin_id=ADMIN_ID,
                   mapping=make_mapping(telegram_id=ADMIN_ID), **kwargs)
    ctx.args = []
    plex = AsyncMock()
    plex.get_owned_server.return_value = ("machine-1", "server1")
    plex.get_library_sections.return_value = _sections()
    plex.list_shares.return_value = []
    ctx.bot_data["plex"] = plex
    return ctx


def _dm(upd):
    upd.effective_chat.type = "private"
    return upd


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def test_invite_non_admin_blocked():
    ctx = _plex_ctx()
    upd = _dm(make_update(text="/invite", user_id=42))
    state = await invite_start(upd, ctx)
    assert state == ConversationHandler.END
    assert "Admin only" in upd.effective_message.reply_calls[0]["text"]


async def test_invite_requires_dm():
    ctx = _plex_ctx()
    upd = make_update(text="/invite", user_id=ADMIN_ID)
    upd.effective_chat.type = "supergroup"
    state = await invite_start(upd, ctx)
    assert state == ConversationHandler.END
    assert "DM me" in upd.effective_message.reply_calls[0]["text"]


async def test_invite_requires_link():
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=None)
    ctx.args = []
    ctx.bot_data["plex"] = AsyncMock()
    upd = _dm(make_update(text="/invite", user_id=ADMIN_ID))
    state = await invite_start(upd, ctx)
    assert state == ConversationHandler.END
    assert "/link" in upd.effective_message.reply_calls[0]["text"]


async def test_invite_with_arg_shows_library_picker_all_selected():
    ctx = _plex_ctx()
    ctx.args = ["friend@example.com"]
    upd = _dm(make_update(text="/invite friend@example.com", user_id=ADMIN_ID))
    state = await invite_start(upd, ctx)
    assert state == INV_LIBS
    assert ctx.user_data["inv_selected"] == {101, 102, 103}
    call = upd.effective_message.reply_calls[-1]
    assert "friend@example.com" in call["text"]
    cbs = _callbacks(call["reply_markup"])
    assert "invl:1:101" in cbs and "invd:1" in cbs


async def test_invite_toggle_excludes_library():
    ctx = _plex_ctx(user_data={
        "inv_sections": _sections(), "inv_selected": {101, 102, 103},
        "inv_version": 1, "inv_email": "friend@example.com",
        "inv_machine": "machine-1", "inv_server_name": "server1",
    })
    upd = make_update(callback_data="invl:1:103", user_id=ADMIN_ID)
    state = await invite_toggle_lib(upd, ctx)
    assert state == INV_LIBS
    assert ctx.user_data["inv_selected"] == {101, 102}


async def test_invite_done_shows_confirm_with_titles():
    ctx = _plex_ctx(user_data={
        "inv_sections": _sections(), "inv_selected": {101, 102},
        "inv_version": 1, "inv_email": "friend@example.com",
        "inv_machine": "machine-1", "inv_server_name": "server1",
    })
    upd = make_update(callback_data="invd:1", user_id=ADMIN_ID)
    state = await invite_libs_done(upd, ctx)
    assert state == INV_CONFIRM
    text = upd.callback_query.edits[0]["text"]
    assert "Movies, TV Shows" in text and "Movies 4K" not in text
    assert "invgo:1" in _callbacks(upd.callback_query.edits[0]["reply_markup"])


async def test_invite_send_calls_plex_with_selected_sections():
    ctx = _plex_ctx(user_data={
        "inv_sections": _sections(), "inv_selected": {101, 102},
        "inv_version": 1, "inv_email": "friend@example.com",
        "inv_machine": "machine-1", "inv_server_name": "server1",
    })
    upd = make_update(callback_data="invgo:1", user_id=ADMIN_ID)
    state = await invite_send(upd, ctx)
    assert state == ConversationHandler.END
    args = ctx.bot_data["plex"].invite_to_server.call_args.args
    assert args[1] == "machine-1"
    assert args[2] == "friend@example.com"
    assert args[3] == [101, 102]
    assert "Invite sent" in upd.callback_query.edits[-1]["text"]


async def test_invite_stale_version_toasts():
    ctx = _plex_ctx(user_data={"inv_version": 2, "inv_selected": {101}})
    upd = make_update(callback_data="invgo:1", user_id=ADMIN_ID)
    state = await invite_send(upd, ctx)
    assert state == INV_CONFIRM
    ctx.bot_data["plex"].invite_to_server.assert_not_awaited()
    assert any(alert for _, alert in upd.callback_query.answers)


async def test_uninvite_confirms_then_removes():
    ctx = _plex_ctx()
    ctx.args = ["friend@example.com"]
    ctx.bot_data["plex"].list_shares.return_value = [
        PlexShare(id=71, email="friend@example.com", username="friend",
                  accepted=True, all_libraries=False),
    ]
    upd = _dm(make_update(text="/uninvite friend@example.com", user_id=ADMIN_ID))
    await cmd_uninvite(upd, ctx)
    call = upd.effective_message.reply_calls[-1]
    assert "friend@example.com" in call["text"]
    assert "uninv:71:a" in _callbacks(call["reply_markup"])
    assert "uninvk" in _callbacks(call["reply_markup"])
    ctx.bot_data["plex"].remove_share.assert_not_awaited()  # confirm required

    go = make_update(callback_data="uninv:71:a", user_id=ADMIN_ID)
    await uninvite_go(go, ctx)
    assert ctx.bot_data["plex"].remove_share.call_args.args[1] == 71
    assert "removed" in go.callback_query.edits[0]["text"]


async def test_uninvite_pending_invite_wording():
    """A pending invite is cancelled, not 'access removed'."""
    ctx = _plex_ctx()
    ctx.args = ["frank@example.com"]
    ctx.bot_data["plex"].list_shares.return_value = [
        PlexShare(id=9, email="frank@example.com", username="",
                  accepted=False, all_libraries=True),
    ]
    upd = _dm(make_update(text="/uninvite frank@example.com", user_id=ADMIN_ID))
    await cmd_uninvite(upd, ctx)
    card = upd.effective_message.reply_calls[-1]
    assert "PENDING invite" in card["text"]
    assert "uninv:9:p" in _callbacks(card["reply_markup"])
    go = make_update(callback_data="uninv:9:p", user_id=ADMIN_ID)
    await uninvite_go(go, ctx)
    assert "Invite cancelled" in go.callback_query.edits[0]["text"]


async def test_invite_cancel_stale_version_toasts():
    """Cancel on a superseded card must not kill the active invite."""
    ctx = _plex_ctx(user_data={"inv_version": 2, "inv_email": "x@y.zz"})
    upd = make_update(callback_data="invx:1", user_id=ADMIN_ID)
    state = await invite_cancel(upd, ctx)
    assert state is None
    assert ctx.user_data.get("inv_email") == "x@y.zz"  # draft survives
    assert any(alert for _, alert in upd.callback_query.answers)

    live = make_update(callback_data="invx:2", user_id=ADMIN_ID)
    state = await invite_cancel(live, ctx)
    assert state == ConversationHandler.END
    assert "inv_email" not in ctx.user_data


async def test_uninvite_ambiguous_match_asks():
    ctx = _plex_ctx()
    ctx.args = ["fr"]
    ctx.bot_data["plex"].list_shares.return_value = [
        PlexShare(id=1, email="fred@example.com", username="fred",
                  accepted=True, all_libraries=True),
        PlexShare(id=2, email="frank@example.com", username="frank",
                  accepted=False, all_libraries=True),
    ]
    upd = _dm(make_update(text="/uninvite fr", user_id=ADMIN_ID))
    await cmd_uninvite(upd, ctx)
    assert "matches 2 shares" in upd.effective_message.reply_calls[-1]["text"]
    ctx.bot_data["plex"].remove_share.assert_not_awaited()


async def test_uninvite_go_non_admin_blocked():
    ctx = _plex_ctx()
    upd = make_update(callback_data="uninv:71", user_id=42)
    await uninvite_go(upd, ctx)
    ctx.bot_data["plex"].remove_share.assert_not_awaited()
