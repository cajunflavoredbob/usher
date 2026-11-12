"""Direct tests of bot.hub.reply_end, the shared if-hub reply branch."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bot import hub
from bot.callback_prefixes import HUB_OPEN, REQUESTS_MINE
from bot.requests_hub import REQUESTS_HUB


def _run(hub_name, retry=None):
    calls, recorded = [], []
    sent = SimpleNamespace(message_id=7)

    async def reply(text, **kwargs):
        calls.append((text, kwargs))
        return sent

    ctx = SimpleNamespace(application=object())
    orig = hub.record_btn
    hub.record_btn = lambda app, uid, msg: recorded.append((uid, msg))
    try:
        asyncio.run(hub.reply_end(ctx, reply, "hello", hub_name, retry,
                                  user_id=5))
    finally:
        hub.record_btn = orig
    return calls, recorded, sent


def test_no_hub_sends_plain_text_and_records_nothing():
    calls, recorded, _ = _run(None, REQUESTS_MINE)
    assert calls == [("hello", {})]
    assert recorded == []


def test_hub_sends_back_button_and_records():
    calls, recorded, sent = _run(REQUESTS_HUB)
    (text, kwargs), = calls
    assert text == "hello"
    row, = kwargs["reply_markup"].inline_keyboard
    assert [b.callback_data for b in row] == [f"{HUB_OPEN}:{REQUESTS_HUB}"]
    assert recorded == [(5, sent)]


def test_hub_with_retry_adds_try_again_before_back():
    calls, recorded, sent = _run(REQUESTS_HUB, REQUESTS_MINE)
    row, = calls[0][1]["reply_markup"].inline_keyboard
    assert [b.callback_data for b in row] == [
        REQUESTS_MINE, f"{HUB_OPEN}:{REQUESTS_HUB}"]
    assert recorded == [(5, sent)]
