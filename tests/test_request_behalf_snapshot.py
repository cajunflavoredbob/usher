"""The Admin's Request for someone files for the person its confirm named, or
files nothing: never the Admin's own Request in its place. Driven through a
real PTB Application with the real request conversation, reset_stale_flows and
global_btn_gate wired as in bot/app.py; only the Telegram network calls and
the Seerr client are faked. Reuses the rig of test_issue_resume_superseded."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update, User
from telegram.ext import Application, TypeHandler

from bot.callback_prefixes import (REQUESTS_BEHALF, RQ_BEHALF_PICK, RQ_GO,
                                   RQ_MEDIA)
from bot.request_flow import _request_conversation
from bot.shared import (global_btn_gate, guard_flow_steps, record_btn,
                        reset_stale_flows)
from seerr import CreatedRequest, MediaResult, REQUEST_STATUS_APPROVED
from tests._handler_harness import make_ctx, make_link
from tests.test_issue_resume_superseded import CHAT_ID, USER_ID, _Harness

GROUP_ID = -100
CLEARED = "The person you picked was cleared"


@pytest.fixture
async def h(monkeypatch):
    app = Application.builder().token("1:x").build()
    app.bot._bot_user = User(id=1, first_name="Usher", is_bot=True,
                             username="UsherBot")
    app._initialized = True
    harness = _Harness(app, monkeypatch)
    # The tapper is the Admin.
    ctx = make_ctx(admin_id=USER_ID, link=make_link(telegram_id=USER_ID))
    app.bot_data.update(ctx.bot_data)
    app.bot_data["store"].list_linked_users = AsyncMock(
        return_value=[(1000, 20, "Person 0"), (1001, 21, "Person 1")])
    seerr = app.bot_data["seerr"]
    seerr.get_my_permissions = AsyncMock(return_value=0)
    seerr.search = AsyncMock(return_value=[
        MediaResult("movie", 444, "Movie B", "2021", None)])
    seerr.create_request = AsyncMock(return_value=CreatedRequest(
        id=9, status=REQUEST_STATUS_APPROVED))
    request_conv = _request_conversation()
    app.bot_data["conversations"] = {"request": request_conv}
    app.bot_data["flow_convs"] = [request_conv]
    guard_flow_steps(request_conv)
    app.add_handler(TypeHandler(Update, reset_stale_flows), group=-2)
    app.add_handler(TypeHandler(Update, global_btn_gate), group=-1)
    app.add_handler(request_conv)
    yield harness


def _last_menu(h) -> int:
    return h.app.bot_data["btn_msgs"][USER_ID][-1]["message_id"]


async def _pick_person_1(h) -> None:
    """Request for someone from the Requests Hub, Person 1 picked."""
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=5000, chat_id=CHAT_ID))
    await h.tap(REQUESTS_BEHALF, 5000)
    await h.tap(f"{RQ_BEHALF_PICK}:21", 5000)
    assert h.user_data["rq_for"]["seerr_id"] == 21


async def _to_confirm(h) -> int:
    """Search, pick Movie B, land on the confirm. Returns its version."""
    await h.say("movie b")
    version = h.user_data["rq_search_version"]
    await h.tap(f"{RQ_MEDIA}:{version}:movie:444", _last_menu(h))
    return version


async def _say_in_group(h, text: str) -> None:
    h._ids += 1
    msg = h._message_dict(h._ids, text)
    msg["chat"] = {"id": GROUP_ID, "type": "group", "title": "g"}
    await h.app.process_update(h._update({"update_id": h._ids,
                                          "message": msg}))


async def _tap_in_group(h, data: str, message_id: int) -> None:
    h._ids += 1
    msg = h._message_dict(message_id)
    msg["chat"] = {"id": GROUP_ID, "type": "group", "title": "g"}
    msg["from"] = {"id": 1, "is_bot": True, "first_name": "Bot"}
    await h.app.process_update(h._update({
        "update_id": h._ids,
        "callback_query": {
            "id": str(h._ids), "chat_instance": "ci", "data": data,
            "from": {"id": USER_ID, "is_bot": False, "first_name": "U"},
            "message": msg}}))


def _filed_user_ids(h) -> list:
    return [c.kwargs["user_id"]
            for c in h.app.bot_data["seerr"].create_request.call_args_list]


async def test_on_behalf_submit_files_for_the_picked_person(h):
    await _pick_person_1(h)
    version = await _to_confirm(h)
    assert "for <b>Person 1</b>" in h.sent[-1]
    await h.tap(f"{RQ_GO}:{version}", _last_menu(h))
    assert _filed_user_ids(h) == [21]
    assert any("Approved for Person 1" in t for t in h.sent)
    # The finished flow takes its snapshot with it.
    assert "rq_confirm_for" not in h.user_data


async def test_admins_own_submit_files_for_nobody_else(h):
    await h.say("/request movie b")
    version = h.user_data["rq_search_version"]
    await h.tap(f"{RQ_MEDIA}:{version}:movie:444", _last_menu(h))
    assert " for " not in h.sent[-1]
    await h.tap(f"{RQ_GO}:{version}", _last_menu(h))
    assert _filed_user_ids(h) == [None]


async def test_a_refused_jump_tap_at_the_confirm_keeps_the_person(h):
    await _pick_person_1(h)
    version = await _to_confirm(h)
    confirm = _last_menu(h)
    # A Request it instead tap while the /request is open is refused.
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=5001, chat_id=CHAT_ID))
    await h.tap("rqfi:movie:222:i", 5001)
    assert "Finish or /cancel the open request first." in h.sent[-1]
    await h.tap(f"{RQ_GO}:{version}", confirm)
    assert _filed_user_ids(h) == [21]


async def test_a_refused_jump_tap_before_the_confirm_keeps_the_person(h):
    await _pick_person_1(h)
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=5001, chat_id=CHAT_ID))
    await h.tap("rqfi:movie:222:i", 5001)
    assert "Finish or /cancel the open request first." in h.sent[-1]
    version = await _to_confirm(h)
    assert "for <b>Person 1</b>" in h.sent[-1]
    await h.tap(f"{RQ_GO}:{version}", _last_menu(h))
    assert _filed_user_ids(h) == [21]


async def test_a_request_in_a_group_mid_confirm_files_nothing(h):
    await _pick_person_1(h)
    version = await _to_confirm(h)
    confirm = _last_menu(h)
    assert "for <b>Person 1</b>" in h.sent[-1]
    await _say_in_group(h, "/request")  # opens the Hub there, ends nothing here
    await h.tap(f"{RQ_GO}:{version}", confirm)
    assert _filed_user_ids(h) == []
    assert CLEARED in h.sent[-1]


async def test_the_admins_own_confirm_still_files_after_a_group_command(h):
    await h.say("/request movie b")
    version = h.user_data["rq_search_version"]
    await h.tap(f"{RQ_MEDIA}:{version}:movie:444", _last_menu(h))
    confirm = _last_menu(h)
    await _say_in_group(h, "/request")
    await h.tap(f"{RQ_GO}:{version}", confirm)
    assert _filed_user_ids(h) == [None]


async def test_a_jump_in_another_chat_is_the_admins_own_request(h):
    h.app.bot_data["seerr"].get_media_title = AsyncMock(
        return_value=("Movie C", "2022"))
    await _pick_person_1(h)  # the DM flow waits for a title
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=7000, chat_id=GROUP_ID))
    await _tap_in_group(h, "rqfi:movie:222", 7000)
    assert "Request <b>Movie C (2022)</b>?" in h.sent[-1]
    assert "Person 1" not in h.sent[-1]
