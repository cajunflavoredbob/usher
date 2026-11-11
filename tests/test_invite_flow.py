"""Tests for /invite and /uninvite: gates, the library multi-select,
confirmation, the Plex calls, and the uninvite confirm path. Then the Admin
Hub (/admin): the Admin-and-DM gate, Invite started from the Hub, the Share
list with its Share view and removal, Status, typed /uninvite and the
Admin's menu."""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock

from telegram.ext import ConversationHandler

from bot import hub
from bot.admin_hub import ADMIN_HUB, cmd_admin
from bot.callback_prefixes import (ADMIN_INVITE, ADMIN_SHARES, ADMIN_STATUS,
                                   HUB_OPEN, SHARE_MATCHES, SHARE_REMOVE,
                                   SHARE_VIEW, UNINV_KEEP)
from bot.invite_flow import (_INV_KEYS, _invite_conversation, _invite_timeout,
                             admin_shares, cmd_uninvite, invite_cancel,
                             invite_email, invite_hub_start,
                             invite_libs_done, invite_send, invite_start,
                             invite_toggle_lib, share_matches, share_remove,
                             share_view, uninvite_cancel, uninvite_go)
from bot.shared import INV_CONFIRM, INV_EMAIL, INV_LIBS
from http_util import PermanentAPIError, TransientAPIError
from plex import PlexLibrarySection, PlexShare

from tests._handler_harness import make_ctx, make_link, make_update

ADMIN_ID = 999


def _sections():
    return [PlexLibrarySection(id=101, title="Movies", type="movie"),
            PlexLibrarySection(id=102, title="TV Shows", type="show"),
            PlexLibrarySection(id=103, title="Movies 4K", type="movie")]


def _plex_ctx(**kwargs):
    ctx = make_ctx(admin_id=ADMIN_ID,
                   link=make_link(telegram_id=ADMIN_ID), **kwargs)
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
    assert upd.effective_message.reply_calls[0]["text"] == "DM me /admin"


async def test_invite_requires_link():
    ctx = make_ctx(admin_id=ADMIN_ID, link=None)
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
    view = upd.effective_message.reply_calls[-1]
    assert "friend@example.com" in view["text"]
    assert "shrm:71" in _callbacks(view["reply_markup"])
    rm = make_update(callback_data="shrm:71", user_id=ADMIN_ID)
    await share_remove(rm, ctx)
    call = rm.callback_query.edits[0]
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
    assert "Pending Invite" in upd.effective_message.reply_calls[-1]["text"]
    rm = make_update(callback_data="shrm:9", user_id=ADMIN_ID)
    await share_remove(rm, ctx)
    card = rm.callback_query.edits[0]
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


async def test_uninvite_ambiguous_match_shows_buttons():
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
    reply = upd.effective_message.reply_calls[-1]
    assert reply["text"].startswith('2 Shares match "fr".')
    assert {"shv:1", "shv:2"} <= set(_callbacks(reply["reply_markup"]))
    ctx.bot_data["plex"].remove_share.assert_not_awaited()


async def test_uninvite_go_non_admin_blocked():
    ctx = _plex_ctx()
    upd = make_update(callback_data="uninv:71", user_id=42)
    await uninvite_go(upd, ctx)
    ctx.bot_data["plex"].remove_share.assert_not_awaited()


# --- the Admin Hub (/admin) ------------------------------------------------------
USER_ID = 42
BACK = ("⬅️ Admin", f"{HUB_OPEN}:admin")
RETRY_INVITE = ("🔁 Try again", ADMIN_INVITE)


def _flat(markup):
    return [(b.text, b.callback_data)
            for row in markup.inline_keyboard for b in row]


def _share(id, email, accepted=True, username="", all_libraries=False,
           libraries=()):
    return PlexShare(id=id, email=email, username=username,
                     accepted=accepted, all_libraries=all_libraries,
                     libraries=tuple(libraries))


def _shares_ctx(shares=(), **kwargs):
    ctx = make_ctx(admin_id=ADMIN_ID,
                   link=make_link(telegram_id=ADMIN_ID), **kwargs)
    ctx.args = []
    plex = AsyncMock()
    plex.get_owned_server.return_value = ("machine-1", "server1")
    plex.get_library_sections.return_value = [
        PlexLibrarySection(id=101, title="Movies", type="movie")]
    plex.list_shares.return_value = list(shares)
    ctx.bot_data["plex"] = plex
    return ctx


def _group(upd):
    upd.effective_chat.type = "group"
    return upd


def _nine_shares():
    shares = [_share(i, f"friend{i}@example.com") for i in range(1, 9)]
    shares.append(_share(9, "pending9@example.com", accepted=False))
    return shares


# --- /admin and its gate ---------------------------------------------------------

async def test_admin_shows_three_button_hub():
    ctx = _shares_ctx()
    upd = make_update(text="/admin", user_id=ADMIN_ID)
    await cmd_admin(upd, ctx)
    reply = upd.effective_message.reply_calls[0]
    assert reply["text"].startswith("🛠 <b>Admin</b>")
    assert reply["parse_mode"] == "HTML"
    assert _flat(reply["reply_markup"]) == [
        ("📨 Invite", ADMIN_INVITE), ("👥 Shares", f"{ADMIN_SHARES}:0"),
        ("📶 Status", ADMIN_STATUS)]


async def test_admin_from_user_says_admin_only():
    ctx = _shares_ctx()
    upd = make_update(text="/admin", user_id=USER_ID)
    await cmd_admin(upd, ctx)
    assert [c["text"] for c in upd.effective_message.reply_calls] == ["Admin only."]


async def test_admin_in_group_says_dm_me():
    ctx = _shares_ctx()
    upd = _group(make_update(text="/admin", user_id=ADMIN_ID))
    await cmd_admin(upd, ctx)
    assert [c["text"] for c in upd.effective_message.reply_calls] == ["DM me /admin"]


async def test_typed_invite_in_group_says_dm_me():
    ctx = _shares_ctx()
    upd = _group(make_update(text="/invite", user_id=ADMIN_ID))
    state = await invite_start(upd, ctx)
    assert state == ConversationHandler.END
    assert [c["text"] for c in upd.effective_message.reply_calls] == ["DM me /admin"]


async def test_invite_shares_status_buttons_refuse_non_admin():
    import bot.app as app_mod
    for handler, data in ((admin_shares, f"{ADMIN_SHARES}:0"),
                          (share_view, f"{SHARE_VIEW}:1"),
                          (share_remove, f"{SHARE_REMOVE}:1"),
                          (invite_hub_start, ADMIN_INVITE),
                          (app_mod.admin_hub_status, ADMIN_STATUS)):
        ctx = _shares_ctx(shares=[_share(1, "friend@example.com")])
        upd = make_update(callback_data=data, user_id=USER_ID)
        await handler(upd, ctx)
        assert upd.callback_query.answers == [("Admin only.", True)]
        assert upd.callback_query.edits == []
        ctx.bot_data["plex"].list_shares.assert_not_awaited()


# --- Invite from the Hub -----------------------------------------------------------

async def test_hub_invite_edits_hub_into_email_prompt():
    ctx = _shares_ctx()
    upd = make_update(callback_data=ADMIN_INVITE, user_id=ADMIN_ID)
    state = await invite_hub_start(upd, ctx)
    assert state == INV_EMAIL
    assert upd.callback_query.edits[0]["text"] == (
        "Who am I inviting? Reply with their email (or Plex username).")
    assert hub.hub_origin(ctx) == ADMIN_HUB


def test_hub_invite_is_invite_conversation_entry():
    conv = _invite_conversation()
    patterns = {ep.pattern.pattern for ep in conv.entry_points
                if getattr(ep, "pattern", None) is not None}
    assert f"^{ADMIN_INVITE}$" in patterns


def _draft_ctx(origin=ADMIN_HUB):
    ctx = _shares_ctx(user_data={
        "inv_sections": [PlexLibrarySection(id=101, title="Movies", type="movie")],
        "inv_selected": {101}, "inv_version": 1,
        "inv_email": "friend@example.com", "inv_machine": "machine-1",
        "inv_server_name": "server1"})
    hub.set_hub_origin(ctx, origin)
    return ctx


async def test_hub_invite_success_offers_admin_back():
    ctx = _draft_ctx()
    upd = make_update(callback_data="invgo:1", user_id=ADMIN_ID)
    state = await invite_send(upd, ctx)
    assert state == ConversationHandler.END
    edit = upd.callback_query.edits[-1]
    assert "Invite sent" in edit["text"]
    assert _flat(edit["reply_markup"]) == [BACK]


async def test_hub_invite_failure_offers_try_again_and_back():
    ctx = _draft_ctx()
    ctx.bot_data["plex"].invite_to_server.side_effect = RuntimeError("boom")
    upd = make_update(callback_data="invgo:1", user_id=ADMIN_ID)
    await invite_send(upd, ctx)
    edit = upd.callback_query.edits[-1]
    assert edit["text"].startswith("Couldn't send the invite.")
    assert _flat(edit["reply_markup"]) == [RETRY_INVITE, BACK]


async def test_invite_already_sharing_400_names_uninvite():
    ctx = _draft_ctx()
    ctx.bot_data["plex"].invite_to_server.side_effect = PermanentAPIError(
        "You're already sharing this server", status_code=400, service="plex")
    upd = make_update(callback_data="invgo:1", user_id=ADMIN_ID)
    state = await invite_send(upd, ctx)
    assert state == ConversationHandler.END
    text = upd.callback_query.edits[-1]["text"]
    assert "friend@example.com already has a Share" in text
    assert "/uninvite" in text
    assert "Couldn't send the invite." not in text
    assert not any(k in ctx.user_data for k in _INV_KEYS)


async def test_hub_invite_already_sharing_offers_back_only():
    ctx = _draft_ctx()
    ctx.bot_data["plex"].invite_to_server.side_effect = PermanentAPIError(
        "You're already sharing this server", status_code=400, service="plex")
    upd = make_update(callback_data="invgo:1", user_id=ADMIN_ID)
    await invite_send(upd, ctx)
    edit = upd.callback_query.edits[-1]
    assert "already has a Share" in edit["text"]
    assert _flat(edit["reply_markup"]) == [BACK]


async def test_invite_other_400_keeps_generic_reply():
    ctx = _draft_ctx()
    ctx.bot_data["plex"].invite_to_server.side_effect = PermanentAPIError(
        "Invalid email", status_code=400, service="plex")
    upd = make_update(callback_data="invgo:1", user_id=ADMIN_ID)
    state = await invite_send(upd, ctx)
    assert state == ConversationHandler.END
    assert upd.callback_query.edits[-1]["text"].startswith(
        "Couldn't send the invite.")
    assert not any(k in ctx.user_data for k in _INV_KEYS)


async def test_hub_invite_server_read_failure_offers_try_again():
    ctx = _shares_ctx()
    hub.set_hub_origin(ctx, ADMIN_HUB)
    ctx.bot_data["plex"].get_owned_server.side_effect = RuntimeError("boom")
    upd = make_update(text="friend@example.com", user_id=ADMIN_ID)
    state = await invite_email(upd, ctx)
    assert state == ConversationHandler.END
    reply = upd.effective_message.reply_calls[-1]
    assert reply["text"].startswith("Couldn't read your Plex server.")
    assert _flat(reply["reply_markup"]) == [RETRY_INVITE, BACK]


async def test_hub_invite_cancel_ends_with_back():
    ctx = _draft_ctx()
    upd = make_update(callback_data="invx:1", user_id=ADMIN_ID)
    state = await invite_cancel(upd, ctx)
    assert state == ConversationHandler.END
    edit = upd.callback_query.edits[0]
    assert edit["text"] == "Cancelled."
    assert _flat(edit["reply_markup"]) == [BACK]


async def test_hub_invite_timeout_offers_try_again():
    ctx = _draft_ctx()
    upd = make_update(text="x", user_id=ADMIN_ID)
    state = await _invite_timeout(upd, ctx)
    assert state == ConversationHandler.END
    kwargs = ctx.bot.send_message.call_args.kwargs
    assert kwargs["text"] == "⏱️ The invite timed out."
    assert _flat(kwargs["reply_markup"]) == [RETRY_INVITE, BACK]


async def test_typed_invite_cancel_keeps_old_text_and_no_buttons():
    ctx = _shares_ctx()
    await invite_start(make_update(text="/invite", user_id=ADMIN_ID), ctx)
    assert hub.hub_origin(ctx) is None
    ctx.user_data["inv_version"] = 1
    upd = make_update(callback_data="invx:1", user_id=ADMIN_ID)
    await invite_cancel(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert edit["text"] == "Cancelled. /invite to start over."
    assert edit.get("reply_markup") is None


# --- the Share list --------------------------------------------------------------

async def test_shares_pages_eight_then_one():
    ctx = _shares_ctx(shares=_nine_shares())
    upd = make_update(callback_data=f"{ADMIN_SHARES}:0", user_id=ADMIN_ID)
    await admin_shares(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert edit["text"].startswith("👥 <b>Shares</b> (9)")
    assert "Page 1 of 2." in edit["text"]
    flat = _flat(edit["reply_markup"])
    views = [b for b in flat if b[1].startswith(f"{SHARE_VIEW}:")]
    assert len(views) == 8
    assert ("Next ▶️", f"{ADMIN_SHARES}:1") in flat
    assert not any(t == "◀️ Prev" for t, _ in flat)
    assert flat[-1] == BACK

    upd2 = make_update(callback_data=f"{ADMIN_SHARES}:1", user_id=ADMIN_ID)
    await admin_shares(upd2, ctx)
    flat2 = _flat(upd2.callback_query.edits[0]["reply_markup"])
    views2 = [b for b in flat2 if b[1].startswith(f"{SHARE_VIEW}:")]
    assert len(views2) == 1
    assert ("◀️ Prev", f"{ADMIN_SHARES}:0") in flat2
    assert not any(t == "Next ▶️" for t, _ in flat2)
    assert flat2[-1] == BACK


async def test_shares_marks_pending():
    ctx = _shares_ctx(shares=_nine_shares())
    upd = make_update(callback_data=f"{ADMIN_SHARES}:1", user_id=ADMIN_ID)
    await admin_shares(upd, ctx)
    flat = _flat(upd.callback_query.edits[0]["reply_markup"])
    assert ("⏳ pending9@example.com (pending)", f"{SHARE_VIEW}:9") in flat
    upd0 = make_update(callback_data=f"{ADMIN_SHARES}:0", user_id=ADMIN_ID)
    await admin_shares(upd0, ctx)
    flat0 = _flat(upd0.callback_query.edits[0]["reply_markup"])
    assert ("friend1@example.com", f"{SHARE_VIEW}:1") in flat0


async def test_shares_empty_says_so_with_admin_back():
    ctx = _shares_ctx(shares=[])
    upd = make_update(callback_data=f"{ADMIN_SHARES}:0", user_id=ADMIN_ID)
    await admin_shares(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert "No one has a Share" in edit["text"]
    assert _flat(edit["reply_markup"]) == [BACK]


async def test_shares_fetch_failure_offers_try_again():
    ctx = _shares_ctx()
    ctx.bot_data["plex"].list_shares.side_effect = TransientAPIError(
        "Plex is down", status_code=503, service="Plex")
    upd = make_update(callback_data=f"{ADMIN_SHARES}:0", user_id=ADMIN_ID)
    await admin_shares(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert edit["text"].startswith("Couldn't list Shares.")
    assert _flat(edit["reply_markup"]) == [
        ("🔁 Try again", f"{ADMIN_SHARES}:0"), BACK]


# --- the Share view and removal ----------------------------------------------------

async def test_share_view_remove_confirm_removes_and_offers_admin():
    ctx = _shares_ctx(shares=[_share(71, "friend@example.com")])
    view = make_update(callback_data=f"{SHARE_VIEW}:71", user_id=ADMIN_ID)
    await share_view(view, ctx)
    edit = view.callback_query.edits[0]
    assert "friend@example.com" in edit["text"]
    assert "Accepted." in edit["text"]
    assert _flat(edit["reply_markup"]) == [
        ("🗑 Remove", f"{SHARE_REMOVE}:71"), ("⬅️ Shares", f"{ADMIN_SHARES}:0")]

    rm = make_update(callback_data=f"{SHARE_REMOVE}:71", user_id=ADMIN_ID)
    await share_remove(rm, ctx)
    card = rm.callback_query.edits[0]
    assert card["text"].startswith("Remove <b>friend@example.com</b>'s access")
    assert _flat(card["reply_markup"]) == [
        ("✅ Remove", "uninv:71:a"), ("🛑 Keep", UNINV_KEEP)]
    ctx.bot_data["plex"].remove_share.assert_not_awaited()

    go = make_update(callback_data="uninv:71:a", user_id=ADMIN_ID)
    await uninvite_go(go, ctx)
    assert ctx.bot_data["plex"].remove_share.call_args.args[1] == 71
    done = go.callback_query.edits[-1]
    assert done["text"] == "✅ Access removed."
    assert _flat(done["reply_markup"]) == [BACK]


async def test_share_view_shows_libraries():
    ctx = _shares_ctx(shares=[_share(5, "new@example.com", accepted=False,
                                   libraries=("Movies", "TV Shows"))])
    upd = make_update(callback_data=f"{SHARE_VIEW}:5", user_id=ADMIN_ID)
    await share_view(upd, ctx)
    text = upd.callback_query.edits[0]["text"]
    assert "Pending Invite, not accepted yet." in text
    assert "Libraries: Movies, TV Shows" in text


async def test_share_view_all_libraries():
    ctx = _shares_ctx(shares=[_share(5, "all@example.com", all_libraries=True)])
    upd = make_update(callback_data=f"{SHARE_VIEW}:5", user_id=ADMIN_ID)
    await share_view(upd, ctx)
    assert "Libraries: all" in upd.callback_query.edits[0]["text"]


async def test_share_gone_ends_with_back():
    ctx = _shares_ctx(shares=[])
    for handler, data in ((share_view, f"{SHARE_VIEW}:5"),
                          (share_remove, f"{SHARE_REMOVE}:5")):
        upd = make_update(callback_data=data, user_id=ADMIN_ID)
        await handler(upd, ctx)
        edit = upd.callback_query.edits[0]
        assert edit["text"].startswith("That Share is gone.")
        assert _flat(edit["reply_markup"]) == [BACK]


async def test_uninvite_keep_offers_admin_back():
    ctx = _shares_ctx()
    upd = make_update(callback_data=UNINV_KEEP, user_id=ADMIN_ID)
    await uninvite_cancel(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert edit["text"] == "Cancelled, nothing changed."
    assert _flat(edit["reply_markup"]) == [BACK]


async def test_remove_failure_offers_try_again():
    ctx = _shares_ctx()
    ctx.bot_data["plex"].remove_share.side_effect = RuntimeError("boom")
    upd = make_update(callback_data="uninv:71:p", user_id=ADMIN_ID)
    await uninvite_go(upd, ctx)
    edit = upd.callback_query.edits[-1]
    assert edit["text"].startswith("Couldn't remove the share.")
    assert _flat(edit["reply_markup"]) == [("🔁 Try again", "uninv:71:p"), BACK]


# --- typed /uninvite ---------------------------------------------------------------

async def test_uninvite_one_match_shows_view():
    ctx = _shares_ctx(shares=[_share(71, "friend@example.com"),
                            _share(72, "other@example.com")])
    ctx.args = ["friend@"]
    upd = make_update(text="/uninvite friend@", user_id=ADMIN_ID)
    await cmd_uninvite(upd, ctx)
    reply = upd.effective_message.reply_calls[-1]
    assert "friend@example.com" in reply["text"]
    assert _flat(reply["reply_markup"]) == [
        ("🗑 Remove", f"{SHARE_REMOVE}:71"), ("⬅️ Shares", f"{ADMIN_SHARES}:0")]
    ctx.bot_data["plex"].remove_share.assert_not_awaited()


async def test_uninvite_two_matches_shows_buttons():
    ctx = _shares_ctx(shares=[_share(1, "fred@example.com"),
                            _share(2, "frank@example.com", accepted=False),
                            _share(3, "other@example.com")])
    ctx.args = ["fr"]
    upd = make_update(text="/uninvite fr", user_id=ADMIN_ID)
    await cmd_uninvite(upd, ctx)
    reply = upd.effective_message.reply_calls[-1]
    assert reply["text"].startswith('2 Shares match "fr".')
    assert _flat(reply["reply_markup"]) == [
        ("⏳ frank@example.com (pending)", f"{SHARE_VIEW}:2"),
        ("fred@example.com", f"{SHARE_VIEW}:1"), BACK]
    ctx.bot_data["plex"].remove_share.assert_not_awaited()


async def test_uninvite_many_matches_page_like_the_share_list():
    ctx = _shares_ctx(shares=_nine_shares() + [_share(10, "zed@other.example")])
    ctx.args = ["Example.com"]
    upd = make_update(text="/uninvite Example.com", user_id=ADMIN_ID)
    await cmd_uninvite(upd, ctx)
    reply = upd.effective_message.reply_calls[-1]
    assert reply["text"].startswith('9 Shares match "example.com".')
    assert "Page 1 of 2." in reply["text"]
    flat = _flat(reply["reply_markup"])
    assert len([b for b in flat if b[1].startswith(f"{SHARE_VIEW}:")]) == 8
    assert ("Next ▶️", f"{SHARE_MATCHES}:1") in flat
    assert not any(t == "◀️ Prev" for t, _ in flat)
    assert flat[-1] == BACK

    upd2 = make_update(callback_data=f"{SHARE_MATCHES}:1", user_id=ADMIN_ID)
    await share_matches(upd2, ctx)
    edit = upd2.callback_query.edits[0]
    assert edit["text"].startswith('9 Shares match "example.com".')
    assert "Page 2 of 2." in edit["text"]
    flat2 = _flat(edit["reply_markup"])
    assert [b for b in flat2 if b[1].startswith(f"{SHARE_VIEW}:")] == [
        ("⏳ pending9@example.com (pending)", f"{SHARE_VIEW}:9")]
    assert ("◀️ Prev", f"{SHARE_MATCHES}:0") in flat2
    assert not any(t == "Next ▶️" for t, _ in flat2)
    assert flat2[-1] == BACK


async def test_uninvite_match_page_without_a_search_ends_with_back():
    ctx = _shares_ctx(shares=_nine_shares())
    upd = make_update(callback_data=f"{SHARE_MATCHES}:1", user_id=ADMIN_ID)
    await share_matches(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert edit["text"] == "That search expired. Type /uninvite again."
    assert _flat(edit["reply_markup"]) == [BACK]
    ctx.bot_data["plex"].list_shares.assert_not_awaited()


# --- Status -------------------------------------------------------------------------

async def test_status_in_group_says_dm_me(monkeypatch):
    import bot.app as app_mod
    check = AsyncMock(return_value={"Seerr": "✅ 1.0"})
    monkeypatch.setattr(app_mod, "_check_connections", check)
    ctx = _shares_ctx()
    upd = _group(make_update(text="/status", user_id=ADMIN_ID))
    await app_mod.cmd_status(upd, ctx)
    assert [c["text"] for c in upd.effective_message.reply_calls] == ["DM me /admin"]
    check.assert_not_awaited()


async def test_hub_status_shows_connection_status_with_back(monkeypatch):
    import bot.app as app_mod
    monkeypatch.setattr(app_mod, "_check_connections",
                        AsyncMock(return_value={"Seerr": "✅ 1.0"}))
    monkeypatch.setattr(app_mod, "format_status", lambda s: "Seerr ok")
    ctx = _shares_ctx()
    upd = make_update(callback_data=ADMIN_STATUS, user_id=ADMIN_ID)
    await app_mod.admin_hub_status(upd, ctx)
    edit = upd.callback_query.edits[-1]
    assert edit["text"] == "<b>Connection status:</b>\nSeerr ok"
    assert _flat(edit["reply_markup"]) == [BACK]


# --- menu, /start and registration ----------------------------------------------------

def test_admin_menu_lists_admin_only():
    from bot.app import _ADMIN_BOT_COMMANDS, _BOT_COMMANDS
    admin = [c for c, _ in _ADMIN_BOT_COMMANDS]
    assert "admin" in admin
    for gone in ("invite", "uninvite", "status"):
        assert gone not in admin
    assert "admin" not in [c for c, _ in _BOT_COMMANDS]


async def test_start_lists_admin_line_for_admin_only(monkeypatch):
    import bot.app as app_mod
    monkeypatch.setattr(app_mod, "_check_connections", AsyncMock(return_value={}))
    monkeypatch.setattr(app_mod, "format_status", lambda s: "ok")
    line = "  /admin - invite someone, see and remove Shares, connection status"
    note = "(/invite, /uninvite and /status still work when typed.)"
    ctx = make_ctx(admin_id=ADMIN_ID)
    upd = make_update(text="/start", user_id=ADMIN_ID)
    await app_mod.cmd_start(upd, ctx)
    text = upd.effective_message.reply_calls[0]["text"]
    assert line in text and note in text
    for old in ("/invite -", "/uninvite -", "/status -"):
        assert old not in text
    user = make_update(text="/start", user_id=USER_ID)
    await app_mod.cmd_start(user, make_ctx(admin_id=ADMIN_ID))
    user_text = user.effective_message.reply_calls[0]["text"]
    assert "/admin" not in user_text


def test_admin_hub_handlers_registered():
    src = Path("bot/app.py").read_text(encoding="utf-8")
    last = src.index("CallbackQueryHandler(unmatched_callback)")
    assert 'CommandHandler("admin", cmd_admin)' in src
    for handler, pattern in (
            ("admin_shares", r'fr"\^\{ADMIN_SHARES\}:\\d\+\$"'),
            ("share_view", r'fr"\^\{SHARE_VIEW\}:\\d\+\$"'),
            ("share_remove", r'fr"\^\{SHARE_REMOVE\}:\\d\+\$"'),
            ("share_matches", r'fr"\^\{SHARE_MATCHES\}:\\d\+\$"'),
            ("admin_hub_status", r'fr"\^\{ADMIN_STATUS\}\$"')):
        found = re.search(
            rf"CallbackQueryHandler\({handler},\s*pattern={pattern}\)", src)
        assert found and found.start() < last, handler
