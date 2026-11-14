"""The command menu and the /start and /help list share one order: Admin
(the Admin only), then Requests, Issues (/tickets), Account and Help."""
from __future__ import annotations

from unittest.mock import AsyncMock

from tests._handler_harness import make_ctx, make_update

ADMIN_ID = 999
USER_ID = 42
USER_ORDER = ["request", "tickets", "account", "help"]
ADMIN_ORDER = ["admin"] + USER_ORDER


def _listed(text: str) -> list[str]:
    """The commands of the "/cmd - description" lines, in the order shown."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("/") and " - " in line:
            out.append(line[1:].split(" ", 1)[0])
    return out


def test_menu_order_for_everyone_and_admin():
    from bot.app import _ADMIN_BOT_COMMANDS, _BOT_COMMANDS
    assert [c for c, _ in _BOT_COMMANDS] == USER_ORDER
    assert [c for c, _ in _ADMIN_BOT_COMMANDS] == ADMIN_ORDER


async def test_start_lists_commands_in_menu_order(monkeypatch):
    import bot.app as app_mod
    monkeypatch.setattr(app_mod, "_check_connections", AsyncMock(return_value={}))
    monkeypatch.setattr(app_mod, "format_status", lambda s: "ok")

    admin = make_update(text="/start", user_id=ADMIN_ID)
    await app_mod.cmd_start(admin, make_ctx(admin_id=ADMIN_ID))
    assert _listed(admin.effective_message.reply_calls[0]["text"]) == ADMIN_ORDER

    user = make_update(text="/start", user_id=USER_ID)
    await app_mod.cmd_start(user, make_ctx(admin_id=ADMIN_ID))
    assert _listed(user.effective_message.reply_calls[0]["text"]) == USER_ORDER
