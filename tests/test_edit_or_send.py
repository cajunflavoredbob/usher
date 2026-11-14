"""edit_or_send: a refused edit falls back to a new message, except when
Telegram refuses because the text is unchanged (a double tap)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import telegram

from bot.shared import edit_or_send


def _q(error):
    message = SimpleNamespace(reply_text=AsyncMock(return_value="new"))
    return SimpleNamespace(edit_message_text=AsyncMock(side_effect=error),
                           message=message)


async def test_unchanged_text_is_not_resent():
    q = _q(telegram.error.BadRequest(
        "Message is not modified: specified new message content and reply "
        "markup are exactly the same"))
    assert await edit_or_send(q, "same") is q.message
    q.message.reply_text.assert_not_awaited()


async def test_other_refused_edit_falls_back_to_a_new_message():
    q = _q(telegram.error.BadRequest("Message to edit not found"))
    assert await edit_or_send(q, "text") == "new"
    q.message.reply_text.assert_awaited_once()
