"""Tests for the revoked-token recovery entry point (bot.link_flow.cmd_relink):
one tap clears the dead token and drops the user into the platform-choice
step of the existing link conversation, skipping the consent step."""
from __future__ import annotations

from bot.callback_prefixes import LINK_PLATFORM, RELINK
from bot.link_flow import _link_conversation, cmd_relink
from bot.shared import AWAIT_PLATFORM_CHOICE
from tests._handler_harness import make_ctx, make_update


async def test_relink_unlinks_and_enters_platform_choice():
    upd = make_update(callback_data=RELINK, user_id=42)
    ctx = make_ctx(admin_id=999)
    state = await cmd_relink(upd, ctx)
    assert state == AWAIT_PLATFORM_CHOICE
    ctx.bot_data["store"].unlink.assert_called_once_with(42)
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
