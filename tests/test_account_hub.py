"""Tests for the shared Hub pieces (bot.hub) and the Account Hub (/account)."""
from __future__ import annotations

from types import SimpleNamespace

from telegram import InlineKeyboardButton

from bot import hub
from bot.callback_prefixes import HUB_OPEN, HUB_RETRY, LINK_HELP


def _btns(markup):
    return [[(b.text, b.callback_data) for b in row]
            for row in markup.inline_keyboard]


def test_hub_rows_never_exceed_two():
    buttons = [InlineKeyboardButton(f"B{i}", callback_data=f"x:{i}")
               for i in range(5)]
    rows = hub.hub_rows(buttons)
    assert [len(r) for r in rows] == [2, 2, 1]
    assert all(len(r) <= hub.HUB_MAX_ROW for r in rows)


def test_end_markup_without_retry_is_one_back_button():
    hub.register_hub("demo", "Demo", None)
    try:
        assert _btns(hub.end_markup("demo")) == [[("⬅️ Demo", f"{HUB_OPEN}:demo")]]
    finally:
        hub.HUBS.pop("demo", None)


def test_end_markup_try_again_and_back_share_one_row():
    hub.register_hub("demo", "Demo", None)
    try:
        rows = _btns(hub.end_markup("demo", LINK_HELP))
    finally:
        hub.HUBS.pop("demo", None)
    assert rows == [[("🔁 Try again", LINK_HELP), ("⬅️ Demo", f"{HUB_OPEN}:demo")]]


def test_hub_origin_set_read_and_clear():
    ctx = SimpleNamespace(user_data={})
    assert hub.hub_origin(ctx) is None
    hub.set_hub_origin(ctx, "account")
    assert ctx.user_data[hub.HUB_ORIGIN_KEY] == "account"
    assert hub.hub_origin(ctx) == "account"
    hub.set_hub_origin(ctx, None)
    assert hub.HUB_ORIGIN_KEY not in ctx.user_data
    hub.set_hub_origin(ctx, None)  # clearing twice is harmless


def test_hub_retry_prefix_not_matched_by_open_pattern():
    import re
    assert not re.match(fr"^{HUB_OPEN}:[a-z]+$", f"{HUB_RETRY}:linkplat:desktop")


# --- Account Hub and Link/Unlink from it -------------------------------------

import dataclasses
from unittest.mock import AsyncMock

from telegram.constants import ChatType
from telegram.ext import ConversationHandler

from bot import link_flow
from bot.callback_prefixes import (ACCOUNT_LINK, ACCOUNT_UNLINK, LINK_CONSENT,
                                   LINK_PLATFORM, RELINK)
from bot.shared import (AWAIT_LINK_CONSENT, AWAIT_PLATFORM_CHOICE,
                        global_btn_gate, record_btn)

from tests._handler_harness import make_ctx, make_mapping, make_update

BACK = ("⬅️ Account", f"{HUB_OPEN}:account")


def _dm(upd):
    upd.effective_message.chat = SimpleNamespace(type=ChatType.PRIVATE)
    return upd


def _flat(markup):
    return [pair for row in _btns(markup) for pair in row]


def _pin(code="abcd"):
    return SimpleNamespace(id=1, auth_url="https://plex.example.com/auth", code=code)


def _gate_update(user_id, message_id, chat_id=100):
    q = SimpleNamespace(
        message=SimpleNamespace(message_id=message_id, chat_id=chat_id),
        from_user=SimpleNamespace(id=user_id), answers=[])

    async def _answer(text="", show_alert=False):
        q.answers.append(text)

    async def _edit_reply_markup(reply_markup=None):
        pass

    q.answer = _answer
    q.edit_message_reply_markup = _edit_reply_markup
    return SimpleNamespace(callback_query=q)


async def test_account_hub_unlinked_offers_link_only():
    ctx = make_ctx(mapping=None)
    upd = make_update(text="/account")
    await link_flow.cmd_account(upd, ctx)
    reply = upd.effective_message.reply_calls[0]
    assert reply["text"].startswith("Not linked.")
    assert _flat(reply["reply_markup"]) == [("🔗 Link", ACCOUNT_LINK)]


async def test_account_hub_linked_offers_unlink_only():
    ctx = make_ctx(mapping=make_mapping())
    upd = make_update(text="/account")
    await link_flow.cmd_account(upd, ctx)
    reply = upd.effective_message.reply_calls[0]
    assert reply["text"] == "🔗 Linked as <b>User1</b>."
    assert reply["parse_mode"] == "HTML"
    assert _flat(reply["reply_markup"]) == [("🔓 Unlink", ACCOUNT_UNLINK)]


async def test_account_hub_escapes_name_and_falls_back_to_plex_username():
    m = dataclasses.replace(make_mapping(plex_username="plex<b>"), seerr_display="")
    ctx = make_ctx(mapping=m)
    upd = make_update(text="/account")
    await link_flow.cmd_account(upd, ctx)
    assert upd.effective_message.reply_calls[0]["text"] == \
        "🔗 Linked as <b>plex&lt;b&gt;</b>."


async def test_account_hub_message_recorded_with_gate():
    ctx = make_ctx(mapping=None)
    for mid in (900, 901, 902):
        record_btn(ctx.application, 42, SimpleNamespace(message_id=mid, chat_id=100))
    upd = make_update(text="/account")
    await link_flow.cmd_account(upd, ctx)
    hub_id = ctx.application.bot_data["btn_msgs"][42][-1]["message_id"]
    assert hub_id not in (900, 901, 902)
    await global_btn_gate(_gate_update(42, hub_id),
                          SimpleNamespace(application=ctx.application))


async def test_hub_unlink_ends_with_back_button_then_shows_unlinked_hub():
    ctx = make_ctx(mapping=make_mapping())
    upd = make_update(callback_data=ACCOUNT_UNLINK)
    ctx.user_data["relink_resume"] = {"kind": "x"}
    await link_flow.account_unlink(upd, ctx)
    ctx.bot_data["store"].unlink.assert_called_once_with(42)
    assert "relink_resume" not in ctx.user_data
    edit = upd.callback_query.edits[0]
    assert edit["text"].startswith("🔓 Unlinked.")
    assert _flat(edit["reply_markup"]) == [BACK]
    ctx.bot_data["store"].get.return_value = None

    back = make_update(callback_data=BACK[1])
    await hub.hub_open_callback(back, ctx)
    shown = back.callback_query.edits[0]
    assert shown["text"].startswith("Not linked.")
    assert _flat(shown["reply_markup"]) == [("🔗 Link", ACCOUNT_LINK)]


async def test_hub_unlink_when_not_linked_says_so_with_back():
    ctx = make_ctx(mapping=None)
    ctx.bot_data["store"].unlink.return_value = False
    upd = make_update(callback_data=ACCOUNT_UNLINK)
    await link_flow.account_unlink(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert edit["text"] == "You weren't linked."
    assert _flat(edit["reply_markup"]) == [BACK]


async def test_hub_open_ignores_unknown_hub():
    ctx = make_ctx()
    upd = make_update(callback_data=f"{HUB_OPEN}:nope")
    await hub.hub_open_callback(upd, ctx)
    assert upd.callback_query.edits == []


async def test_hub_link_button_edits_consent_prompt_in_place_and_sets_origin():
    ctx = make_ctx()
    upd = _dm(make_update(callback_data=ACCOUNT_LINK))
    state = await link_flow.account_link(upd, ctx)
    assert state == AWAIT_LINK_CONSENT
    assert hub.hub_origin(ctx) == "account"
    edit = upd.callback_query.edits[0]
    assert edit["text"].startswith("Sign in with Plex")
    assert _flat(edit["reply_markup"]) == [
        ("✅ Yes, continue", f"{LINK_CONSENT}:yes"),
        ("🛑 Cancel", f"{LINK_CONSENT}:no")]


async def test_hub_link_in_group_keeps_dm_text_with_back():
    ctx = make_ctx()
    upd = make_update(callback_data=ACCOUNT_LINK)
    upd.effective_message.chat = SimpleNamespace(type=ChatType.GROUP)
    state = await link_flow.account_link(upd, ctx)
    assert state == ConversationHandler.END
    edit = upd.callback_query.edits[0]
    assert edit["text"] == "Please DM me to link your account."
    assert _flat(edit["reply_markup"]) == [BACK]


async def test_hub_link_without_seerr_keeps_setup_text_with_back():
    ctx = make_ctx()
    ctx.bot_data["seerr"] = None
    upd = make_update(callback_data=ACCOUNT_LINK)
    upd.effective_message.chat = SimpleNamespace(type=ChatType.PRIVATE)
    state = await link_flow.account_link(upd, ctx)
    assert state == ConversationHandler.END
    reply = upd.effective_message.reply_calls[0]
    assert "isn't set up yet" in reply["text"] or "isn't configured yet" in reply["text"]
    assert _flat(reply["reply_markup"]) == [BACK]
    sent_id = ctx.application.bot_data["btn_msgs"][upd.effective_user.id][-1]["message_id"]
    assert sent_id != upd.effective_message.message_id


async def test_typed_link_without_seerr_has_no_markup():
    ctx = make_ctx()
    ctx.bot_data["seerr"] = None
    upd = make_update(text="/link")
    upd.effective_message.chat = SimpleNamespace(type=ChatType.PRIVATE)
    await link_flow.cmd_link(upd, ctx)
    assert "reply_markup" not in upd.effective_message.reply_calls[0]


async def test_hub_link_consent_no_cancels_with_back():
    ctx = make_ctx()
    hub.set_hub_origin(ctx, "account")
    upd = make_update(callback_data=f"{LINK_CONSENT}:no")
    state = await link_flow.cmd_link_consent(upd, ctx)
    assert state == ConversationHandler.END
    edit = upd.callback_query.edits[0]
    assert edit["text"] == "Cancelled."
    assert _flat(edit["reply_markup"]) == [BACK]


async def test_typed_link_consent_no_keeps_old_text_and_no_buttons():
    ctx = make_ctx()
    upd = make_update(callback_data=f"{LINK_CONSENT}:no")
    await link_flow.cmd_link_consent(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert edit["text"] == "Cancelled. /link to try again later."
    assert "reply_markup" not in edit


async def test_typed_link_cancel_keeps_old_text_and_no_buttons():
    ctx = make_ctx()
    upd = make_update(text="/cancel")
    await link_flow.link_cancel(upd, ctx)
    reply = upd.effective_message.reply_calls[0]
    assert reply["text"] == "Cancelled. /link to try again later."
    assert "reply_markup" not in reply


async def test_hub_link_cancel_command_ends_with_back():
    ctx = make_ctx()
    hub.set_hub_origin(ctx, "account")
    upd = make_update(text="/cancel")
    await link_flow.link_cancel(upd, ctx)
    reply = upd.effective_message.reply_calls[0]
    assert reply["text"] == "Cancelled."
    assert _flat(reply["reply_markup"]) == [BACK]


async def test_hub_link_pin_failure_offers_try_again_and_back():
    ctx = make_ctx()
    ctx.bot_data["plex"].request_pin.side_effect = RuntimeError("down")
    hub.set_hub_origin(ctx, "account")
    upd = make_update(callback_data=f"{LINK_PLATFORM}:mobile")
    state = await link_flow.cmd_link_platform(upd, ctx)
    assert state == ConversationHandler.END
    edit = upd.callback_query.edits[0]
    assert edit["text"].startswith("Couldn't start Plex auth.")
    assert _flat(edit["reply_markup"]) == [
        ("🔁 Try again", f"{HUB_RETRY}:linkplat:mobile"), BACK]


async def test_typed_link_pin_failure_has_no_buttons():
    ctx = make_ctx()
    ctx.bot_data["plex"].request_pin.side_effect = RuntimeError("down")
    upd = make_update(callback_data=f"{LINK_PLATFORM}:mobile")
    await link_flow.cmd_link_platform(upd, ctx)
    assert "reply_markup" not in upd.callback_query.edits[0]


async def test_hub_link_strong_pin_timeout_offers_try_again_and_back(monkeypatch):
    ctx = make_ctx()
    ctx.bot_data["plex"].request_pin.return_value = _pin()
    monkeypatch.setattr(link_flow, "_poll_with_cancel",
                        AsyncMock(return_value=None))
    hub.set_hub_origin(ctx, "account")
    upd = make_update(callback_data=f"{LINK_PLATFORM}:desktop")
    await link_flow.cmd_link_platform(upd, ctx)
    edit = upd.callback_query.edits[-1]
    assert edit["text"] == "⏱️ Plex auth timed out."
    assert _flat(edit["reply_markup"]) == [
        ("🔁 Try again", f"{HUB_RETRY}:linkplat:desktop"), BACK]


async def test_hub_link_weak_pin_failure_offers_try_again_and_back():
    ctx = make_ctx()
    ctx.bot_data["plex"].request_pin.side_effect = RuntimeError("down")
    hub.set_hub_origin(ctx, "account")
    upd = make_update(callback_data=LINK_HELP)
    await link_flow.cmd_link_didnt_work(upd, ctx)
    edit = upd.callback_query.edits[0]
    assert edit["text"].startswith("Couldn't get a fresh code.")
    assert _flat(edit["reply_markup"]) == [("🔁 Try again", LINK_HELP), BACK]


async def test_hub_link_weak_pin_timeout_offers_try_again_and_back(monkeypatch):
    ctx = make_ctx()
    ctx.bot_data["plex"].request_pin.return_value = _pin()
    monkeypatch.setattr(link_flow, "_poll_with_cancel",
                        AsyncMock(return_value=None))
    hub.set_hub_origin(ctx, "account")
    upd = make_update(callback_data=LINK_HELP)
    await link_flow.cmd_link_didnt_work(upd, ctx)
    edit = upd.callback_query.edits[-1]
    assert edit["text"] == "⏱️ Plex auth timed out."
    assert _flat(edit["reply_markup"]) == [("🔁 Try again", LINK_HELP), BACK]


async def test_hub_link_seerr_rejection_offers_try_again_and_back():
    ctx = make_ctx()
    ctx.bot_data["seerr"].login_with_plex = AsyncMock(side_effect=RuntimeError("no"))
    upd = make_update(callback_data=LINK_HELP)
    await link_flow._finalize_link(upd, ctx, "tok", hub="account")
    kwargs = ctx.bot.send_message.call_args.kwargs
    assert "Seerr rejected the sign-in" in kwargs["text"]
    assert _flat(kwargs["reply_markup"]) == [("🔁 Try again", ACCOUNT_LINK), BACK]


async def test_typed_link_seerr_rejection_has_no_buttons():
    ctx = make_ctx()
    ctx.bot_data["seerr"].login_with_plex = AsyncMock(side_effect=RuntimeError("no"))
    upd = make_update(callback_data=LINK_HELP)
    await link_flow._finalize_link(upd, ctx, "tok")
    assert "reply_markup" not in ctx.bot.send_message.call_args.kwargs


async def test_hub_link_success_ends_with_back():
    ctx = make_ctx()
    ctx.bot_data["seerr"].login_with_plex = AsyncMock(return_value=(7, "User1"))
    ctx.bot_data["plex"].get_user = AsyncMock(
        return_value=SimpleNamespace(uuid="u", username="user1plex"))
    ctx.bot_data["store"].link_with_plex = AsyncMock()
    upd = make_update(callback_data=LINK_HELP)
    await link_flow._finalize_link(upd, ctx, "tok", hub="account")
    kwargs = ctx.bot.send_message.call_args.kwargs
    assert kwargs["text"].startswith("✅ Linked as <b>User1</b>.")
    assert _flat(kwargs["reply_markup"]) == [BACK]


async def test_hub_retry_linkplat_reruns_pin_step(monkeypatch):
    ctx = make_ctx()
    ctx.bot_data["plex"].request_pin.return_value = _pin()
    monkeypatch.setattr(link_flow, "_poll_with_cancel",
                        AsyncMock(return_value=None))
    upd = make_update(callback_data=f"{HUB_RETRY}:linkplat:mobile")
    await link_flow.hub_retry_linkplat(upd, ctx)
    assert hub.hub_origin(ctx) == "account"
    ctx.bot_data["plex"].request_pin.assert_awaited_once_with(strong=True)
    first = upd.callback_query.edits[0]
    assert first["text"].startswith("Tap to copy the auth link")


def test_hub_entry_points_registered_on_link_conversation():
    conv = link_flow._link_conversation()
    patterns = {ep.pattern.pattern for ep in conv.entry_points
                if getattr(ep, "pattern", None) is not None}
    assert f"^{ACCOUNT_LINK}$" in patterns
    assert f"^{HUB_RETRY}:linkplat:(desktop|mobile)$" in patterns


async def test_typed_link_clears_hub_origin():
    ctx = make_ctx()
    hub.set_hub_origin(ctx, "account")
    upd = _dm(make_update(text="/link"))
    await link_flow.cmd_link(upd, ctx)
    assert hub.hub_origin(ctx) is None


async def test_relink_button_clears_hub_origin():
    ctx = make_ctx()
    hub.set_hub_origin(ctx, "account")
    upd = make_update(callback_data=RELINK)
    state = await link_flow.cmd_relink(upd, ctx)
    assert state == AWAIT_PLATFORM_CHOICE
    assert hub.hub_origin(ctx) is None


async def test_link_timeout_clears_hub_origin():
    ctx = make_ctx()
    hub.set_hub_origin(ctx, "account")
    await link_flow._link_timeout(make_update(text="x"), ctx)
    assert hub.hub_origin(ctx) is None


def test_menu_lists_account_not_link_or_unlink():
    from bot.app import _ADMIN_BOT_COMMANDS, _BOT_COMMANDS
    for menu in (_BOT_COMMANDS, _ADMIN_BOT_COMMANDS):
        names = [c for c, _ in menu]
        assert names[0] == "account"
        assert "link" not in names and "unlink" not in names


async def test_start_lists_account_and_notes_typed_commands_for_user():
    from bot.app import cmd_start
    ctx = make_ctx(admin_id=999)
    upd = make_update(text="/start", user_id=42)
    await cmd_start(upd, ctx)
    text = upd.effective_message.reply_calls[0]["text"]
    assert "/account - link or unlink your Plex account" in text
    assert ("(/link, /unlink, /issue, /requests, /trending and "
            "/subscriptions still work when typed.)") in text
    assert "/link -" not in text and "/unlink -" not in text


async def test_start_lists_account_and_notes_typed_commands_for_admin(monkeypatch):
    import bot.app as app_mod
    monkeypatch.setattr(app_mod, "_check_connections",
                        AsyncMock(return_value={}))
    monkeypatch.setattr(app_mod, "format_status", lambda s: "ok")
    ctx = make_ctx(admin_id=999)
    upd = make_update(text="/start", user_id=999)
    await app_mod.cmd_start(upd, ctx)
    text = upd.effective_message.reply_calls[0]["text"]
    assert "/account - link or unlink your Plex account" in text
    assert ("(/link, /unlink, /issue, /requests, /trending and "
            "/subscriptions still work when typed.)") in text
    assert "/link -" not in text and "/unlink -" not in text


def test_hub_origin_key_listed_in_conversation_cleanup_keys():
    from bot.app import _CONVERSATION_USER_DATA_KEYS
    assert "hub_origin" in _CONVERSATION_USER_DATA_KEYS
