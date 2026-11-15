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

from bot.callback_prefixes import (REQUESTS_BEHALF, REQUESTS_NEW,
                                   RQ_BEHALF_PICK, RQ_GO, RQ_MEDIA)
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


async def test_a_jump_in_another_chat_names_no_person_at_its_confirm(h):
    h.app.bot_data["seerr"].get_media_title = AsyncMock(
        return_value=("Movie C", "2022"))
    await _pick_person_1(h)  # the DM flow waits for a title
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=7000, chat_id=GROUP_ID))
    await _tap_in_group(h, "rqfi:movie:222", 7000)
    assert "Request <b>Movie C (2022)</b>?" in h.sent[-1]
    assert "Person 1" not in h.sent[-1]


# --- the pick belongs to the DM: other chats never clear or read it ---------


async def test_a_bare_request_in_a_group_keeps_the_dm_pick(h):
    await _pick_person_1(h)
    await _say_in_group(h, "/request")
    version = await _to_confirm(h)
    assert "for <b>Person 1</b>" in h.sent[-1]
    await h.tap(f"{RQ_GO}:{version}", _last_menu(h))
    assert _filed_user_ids(h) == [21]


async def test_a_trending_command_in_a_group_keeps_the_dm_pick(h):
    await _pick_person_1(h)
    await _say_in_group(h, "/trending")
    version = await _to_confirm(h)
    assert "for <b>Person 1</b>" in h.sent[-1]
    await h.tap(f"{RQ_GO}:{version}", _last_menu(h))
    assert _filed_user_ids(h) == [21]


async def test_a_new_request_tap_in_a_group_keeps_the_dm_pick(h):
    await _pick_person_1(h)
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=7001, chat_id=GROUP_ID))
    await _tap_in_group(h, REQUESTS_NEW, 7001)
    assert h.user_data["rq_for"]["seerr_id"] == 21
    version = await _to_confirm(h)
    assert "for <b>Person 1</b>" in h.sent[-1]
    await h.tap(f"{RQ_GO}:{version}", _last_menu(h))
    assert _filed_user_ids(h) == [21]


async def test_a_request_it_instead_tap_in_a_group_keeps_the_dm_pick(h):
    h.app.bot_data["seerr"].get_media_title = AsyncMock(
        return_value=("Movie C", "2022"))
    await _pick_person_1(h)
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=7000, chat_id=GROUP_ID))
    await _tap_in_group(h, "rqfi:movie:222:i", 7000)
    assert h.user_data["rq_for"]["seerr_id"] == 21
    version = await _to_confirm(h)
    assert "for <b>Person 1</b>" in h.sent[-1]
    await h.tap(f"{RQ_GO}:{version}", _last_menu(h))
    assert _filed_user_ids(h) == [21]


async def test_a_group_request_names_nobody_and_files_as_the_admin(h):
    await _pick_person_1(h)
    await _say_in_group(h, "/request movie b")
    version = h.user_data["rq_search_version"]
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=7002, chat_id=GROUP_ID))
    await _tap_in_group(h, f"{RQ_MEDIA}:{version}:movie:444", 7002)
    assert "Request <b>Movie B (2021)</b>?" in h.sent[-1]
    assert "Person 1" not in h.sent[-1]
    await _tap_in_group(h, f"{RQ_GO}:{version}", 7002)
    assert _filed_user_ids(h) == [None]
    # The group flow never read Person 1's quota.
    assert all(c.args[0] != 21
               for c in h.app.bot_data["seerr"].get_quota.call_args_list)


async def test_a_pick_cleared_from_a_group_flow_refuses_the_title(h):
    await _pick_person_1(h)
    # A /request flow in the group, then its /cancel: ending it clears the
    # Request keys the DM flow shares, the pick among them.
    await _say_in_group(h, "/request movie b")
    await _say_in_group(h, "/cancel")
    assert "rq_for" not in h.user_data
    await h.say("movie b")
    assert CLEARED in h.sent[-1]
    assert _filed_user_ids(h) == []
    h.app.bot_data["seerr"].search.assert_awaited_once()  # only the group's


async def test_a_plain_request_after_a_behalf_one_files_as_the_admin(h):
    await _pick_person_1(h)
    version = await _to_confirm(h)
    await h.tap(f"{RQ_GO}:{version}", _last_menu(h))
    await h.say("/request movie b")
    version = h.user_data["rq_search_version"]
    await h.tap(f"{RQ_MEDIA}:{version}:movie:444", _last_menu(h))
    assert " for " not in h.sent[-1]
    await h.tap(f"{RQ_GO}:{version}", _last_menu(h))
    assert _filed_user_ids(h) == [21, None]


async def test_a_new_request_tap_in_the_dm_starts_a_plain_request(h):
    await _pick_person_1(h)
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=5002, chat_id=CHAT_ID))
    await h.tap(REQUESTS_NEW, 5002)
    assert "rq_for" not in h.user_data
    version = await _to_confirm(h)
    assert " for " not in h.sent[-1]
    await h.tap(f"{RQ_GO}:{version}", _last_menu(h))
    assert _filed_user_ids(h) == [None]


async def test_the_confirm_draw_refuses_a_behalf_flow_without_its_person():
    from bot.request_flow import _show_confirm
    from tests._handler_harness import make_update
    ctx = make_ctx(admin_id=USER_ID, user_data={
        "rq_media": {"type": "movie", "tmdb_id": 444, "title": "Movie B",
                     "year": "2021"},
        "rq_for_picked": True, "rq_search_version": 3})
    upd = make_update(callback_data=f"{RQ_MEDIA}:3:movie:444", user_id=USER_ID)
    await _show_confirm(upd, ctx)
    texts = [e["text"] for e in upd.callback_query.edits]
    assert texts and CLEARED in texts[-1]
    assert not any(t.startswith("Request <b>") for t in texts)


async def test_a_group_request_after_the_pick_was_cleared_still_files(h):
    """The lost-pick refusal is the DM flow's: a later /request in the group
    is the Admin's own and goes ahead."""
    await _pick_person_1(h)
    await _say_in_group(h, "/request movie b")
    await _say_in_group(h, "/cancel")
    await _say_in_group(h, "/request movie b")
    version = h.user_data["rq_search_version"]
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=7003, chat_id=GROUP_ID))
    await _tap_in_group(h, f"{RQ_MEDIA}:{version}:movie:444", 7003)
    assert "Request <b>Movie B (2021)</b>?" in h.sent[-1]
    await _tap_in_group(h, f"{RQ_GO}:{version}", 7003)
    assert _filed_user_ids(h) == [None]


def _quota(remaining: int):
    from seerr import Quota, QuotaBucket
    bucket = QuotaBucket(days=7, limit=5, used=5 - remaining,
                         remaining=remaining, restricted=False)
    return Quota(movie=bucket, tv=bucket)


async def test_the_confirm_shows_the_picked_persons_quota(h):
    seerr = h.app.bot_data["seerr"]
    seerr.get_quota = AsyncMock(return_value=_quota(2))
    await _pick_person_1(h)
    await _to_confirm(h)
    assert "Quota: 2 of 5 requests left" in h.sent[-1]
    assert [c.args[0] for c in seerr.get_quota.call_args_list] == [21]


async def test_a_users_confirm_shows_their_own_quota_despite_a_leftover_pick():
    """Only the Admin's quota line reads a Request for someone pick."""
    from bot.request_flow import _show_confirm
    from tests._handler_harness import make_update
    # make_link's seerr_id is 7.
    ctx = make_ctx(admin_id=1, link=make_link(telegram_id=USER_ID),
                   user_data={
        "rq_media": {"type": "movie", "tmdb_id": 444, "title": "Movie B",
                     "year": "2021"},
        "rq_for": {"seerr_id": 21, "name": "Person 1", "telegram_id": 1001},
        "rq_search_version": 3})
    ctx.bot_data["seerr"].get_quota = AsyncMock(return_value=_quota(2))
    upd = make_update(callback_data=f"{RQ_MEDIA}:3:movie:444", user_id=USER_ID)
    await _show_confirm(upd, ctx)
    assert [c.args[0] for c in ctx.bot_data["seerr"].get_quota.call_args_list] == [7]
    assert "Quota: 2 of 5 requests left" in upd.callback_query.edits[-1]["text"]
