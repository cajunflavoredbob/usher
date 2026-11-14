"""Now playing on Status, and the Stop confirm, with a fake Tautulli."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import app as app_mod
from bot.nowplaying import (DEFAULT_STOP_MESSAGE, _token, stop_ask,
                            stop_confirmed)

from tests._handler_harness import make_ctx, make_update

ADMIN = 999

SESSIONS = [
    {"session_id": "sess-aaa", "friendly_name": "Alice",
     "full_title": "Big Movie", "player": "Living Room TV",
     "transcode_decision": "direct play"},
    {"session_id": "sess-bbb", "friendly_name": "Bob",
     "full_title": "Long Show - Pilot", "player": "Phone",
     "transcode_decision": "transcode"},
]


def _tau(sessions=SESSIONS):
    return SimpleNamespace(
        get_activity=AsyncMock(return_value={"sessions": list(sessions)}),
        terminate_session=AsyncMock(),
        ping=AsyncMock(return_value="v2"))


def _ctx(tau):
    ctx = make_ctx(admin_id=ADMIN, bot_data_overrides={"tautulli": tau})
    for key in ("seerr", "radarr", "sonarr", "sabnzbd"):
        ctx.bot_data.setdefault(key, None)
    return ctx


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


async def _status(tau, user_id=ADMIN):
    ctx = _ctx(tau)
    upd = make_update(text="/status", user_id=user_id)
    await app_mod.cmd_status(upd, ctx)
    return upd, ctx


async def test_two_sessions_render_two_lines_and_two_stop_buttons():
    upd, _ = await _status(_tau())
    call = upd.effective_message.reply_calls[-1]
    assert "Alice" in call["text"] and "Big Movie" in call["text"]
    assert "Living Room TV, direct play" in call["text"]
    assert "Bob" in call["text"] and "Phone, transcode" in call["text"]
    buttons = _buttons(call["reply_markup"])
    assert len(buttons) == 2
    assert all(b.text.startswith("⏹ Stop") for b in buttons)
    assert len(call["reply_markup"].inline_keyboard[0]) == 2


async def test_three_sessions_wrap_at_two_per_row():
    sessions = SESSIONS + [{"session_id": "sess-ccc", "friendly_name": "Cy",
                            "full_title": "X"}]
    upd, _ = await _status(_tau(sessions))
    rows = upd.effective_message.reply_calls[-1]["reply_markup"].inline_keyboard
    assert [len(r) for r in rows] == [2, 1]


async def test_hub_status_keeps_back_row_below_stop_buttons():
    ctx = _ctx(_tau())
    upd = make_update(callback_data="admt", user_id=ADMIN)
    await app_mod.admin_hub_status(upd, ctx)
    markup = upd.callback_query.edits[-1]["reply_markup"]
    assert len(markup.inline_keyboard) == 2
    assert markup.inline_keyboard[-1][0].callback_data == "hubo:admin"


async def test_no_sessions_says_nothing_playing():
    upd, _ = await _status(_tau([]))
    call = upd.effective_message.reply_calls[-1]
    assert "Nothing playing" in call["text"]
    assert call["reply_markup"] is None


async def test_no_tautulli_has_no_section():
    ctx = make_ctx(admin_id=ADMIN)
    for key in ("seerr", "radarr", "sonarr", "sabnzbd", "tautulli"):
        ctx.bot_data[key] = None
    upd = make_update(text="/status", user_id=ADMIN)
    await app_mod.cmd_status(upd, ctx)
    call = upd.effective_message.reply_calls[-1]
    assert "Now playing" not in call["text"]
    assert call["reply_markup"] is None


async def test_stop_tap_opens_confirm_with_both_choices():
    tau = _tau()
    ctx = _ctx(tau)
    tok = _token("sess-bbb")
    upd = make_update(callback_data=f"stk:{tok}", user_id=ADMIN)
    await stop_ask(upd, ctx)
    edit = upd.callback_query.edits[-1]
    assert "Bob" in edit["text"] and "Long Show - Pilot" in edit["text"]
    data = [b.callback_data for b in _buttons(edit["reply_markup"])]
    assert f"stg:{tok}" in data and f"stm:{tok}" in data
    tau.terminate_session.assert_not_awaited()


async def test_confirm_stop_posts_session_id_without_message():
    tau = _tau()
    ctx = _ctx(tau)
    upd = make_update(callback_data=f"stg:{_token('sess-bbb')}", user_id=ADMIN)
    await stop_confirmed(upd, ctx)
    tau.terminate_session.assert_awaited_once_with("sess-bbb", None)
    assert "Stopped" in upd.callback_query.edits[-1]["text"]


async def test_stop_with_message_passes_default_message():
    tau = _tau()
    ctx = _ctx(tau)
    upd = make_update(callback_data=f"stm:{_token('sess-aaa')}", user_id=ADMIN)
    await stop_confirmed(upd, ctx)
    tau.terminate_session.assert_awaited_once_with("sess-aaa",
                                                   DEFAULT_STOP_MESSAGE)


async def test_non_admin_tap_is_refused_and_nothing_stopped():
    tau = _tau()
    ctx = _ctx(tau)
    for fn, prefix in ((stop_ask, "stk"), (stop_confirmed, "stg"),
                       (stop_confirmed, "stm")):
        upd = make_update(callback_data=f"{prefix}:{_token('sess-aaa')}",
                          user_id=123)
        await fn(upd, ctx)
        assert upd.callback_query.answers == [("Admin only.", True)]
        assert upd.callback_query.edits == []
    tau.terminate_session.assert_not_awaited()
    tau.get_activity.assert_not_awaited()


async def test_ended_stream_is_not_stopped():
    tau = _tau([])
    ctx = _ctx(tau)
    upd = make_update(callback_data=f"stg:{_token('sess-aaa')}", user_id=ADMIN)
    await stop_confirmed(upd, ctx)
    tau.terminate_session.assert_not_awaited()
    assert "already ended" in upd.callback_query.edits[-1]["text"]


async def test_terminate_failure_offers_try_again():
    tau = _tau()
    tau.terminate_session.side_effect = RuntimeError("boom")
    ctx = _ctx(tau)
    upd = make_update(callback_data=f"stg:{_token('sess-aaa')}", user_id=ADMIN)
    await stop_confirmed(upd, ctx)
    edit = upd.callback_query.edits[-1]
    assert "Couldn't stop it" in edit["text"]
    assert any(b.text.startswith("🔁") for b in _buttons(edit["reply_markup"]))


async def test_client_terminate_session_is_post_without_message_when_none():
    import httpx
    from tautulli import TautulliClient
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.params["cmd"],
                     request.content.decode()))
        return httpx.Response(200, json={"response": {
            "result": "success", "message": None, "data": {}}})

    c = TautulliClient("http://tau.test:8181", "k")
    c._client = httpx.AsyncClient(base_url="http://tau.test:8181",
                                  transport=httpx.MockTransport(handler))
    await c.terminate_session("abc")
    await c.terminate_session("abc", "bye")
    await c.close()
    assert seen[0][:2] == ("POST", "terminate_session")
    assert "session_id=abc" in seen[0][2] and "message" not in seen[0][2]
    assert "message=bye" in seen[1][2]


async def test_activity_failure_is_reported_without_buttons():
    tau = _tau()
    tau.get_activity.side_effect = RuntimeError("boom")
    upd, _ = await _status(tau)
    call = upd.effective_message.reply_calls[-1]
    assert "Couldn't ask Tautulli" in call["text"]
    assert call["reply_markup"] is None


async def test_stop_outside_a_dm_is_refused():
    tau = _tau()
    ctx = _ctx(tau)
    upd = make_update(callback_data=f"stg:{_token('sess-aaa')}", user_id=ADMIN)
    upd.effective_chat.type = "group"
    await stop_confirmed(upd, ctx)
    assert upd.callback_query.answers == [("DM me /admin", True)]
    tau.terminate_session.assert_not_awaited()
