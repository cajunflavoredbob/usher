"""A submit that is still waiting on Seerr must not wipe, or block, a newer
/request or /issue the user started meanwhile. Driven through a real PTB
Application with the real conversations, reset_stale_flows and global_btn_gate
wired as in bot/app.py; only the Telegram network calls and the Seerr/Plex
clients are faked. Reuses the rig of test_issue_resume_superseded."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update, User
from telegram.ext import Application, TypeHandler

from bot.callback_prefixes import RQ_GO, RQ_MEDIA, TICKET_AUTOFIX_CONFIRM
from bot.request_flow import _request_conversation
from bot.shared import (CONFIRM_AUTOFIX, global_btn_gate, guard_flow_steps,
                        record_btn, reset_stale_flows)
from bot.ticket_flow import _new_ticket_conversation
from seerr import CreatedIssue, CreatedRequest, MediaResult, REQUEST_STATUS_APPROVED
from tests._handler_harness import make_ctx, make_link
from tests.test_issue_resume_superseded import CHAT_ID, USER_ID, _Harness


@pytest.fixture
async def h(monkeypatch):
    app = Application.builder().token("1:x").build()
    app.bot._bot_user = User(id=1, first_name="Usher", is_bot=True,
                             username="UsherBot")
    app._initialized = True
    harness = _Harness(app, monkeypatch)
    ctx = make_ctx(admin_id=999, link=make_link(telegram_id=USER_ID))
    app.bot_data.update(ctx.bot_data)
    seerr = app.bot_data["seerr"]
    seerr.get_my_permissions = AsyncMock(return_value=0)
    seerr.search = AsyncMock(return_value=[
        MediaResult("movie", 444, "Movie B", "2021", 4)])
    request_conv = _request_conversation()
    issue_conv = _new_ticket_conversation()
    app.bot_data["conversations"] = {"request": request_conv,
                                     "issue": issue_conv}
    app.bot_data["flow_convs"] = [request_conv, issue_conv]
    for conv in app.bot_data["flow_convs"]:
        guard_flow_steps(conv)
    app.add_handler(TypeHandler(Update, reset_stale_flows), group=-2)
    app.add_handler(TypeHandler(Update, global_btn_gate), group=-1)
    app.add_handler(request_conv)
    app.add_handler(issue_conv)
    harness.request_conv = request_conv
    harness.issue_conv = issue_conv
    yield harness


def _slow_first_call(release: asyncio.Event, result, second: asyncio.Event = None):
    """A Seerr call whose first caller waits for `release` (and the second for
    `second`, when given); other calls return at once."""
    calls = []

    async def call(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            await release.wait()
        elif len(calls) == 2 and second is not None:
            await second.wait()
        return result
    return call, calls


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


async def _open_a_request(h) -> int:
    """Movie A at its confirm screen, started from a jump button. Returns the
    version its Send button carries."""
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=5000, chat_id=CHAT_ID))
    await h.tap("rqfi:movie:222", 5000)
    assert h.user_data["rq_media"]["tmdb_id"] == 222
    return h.user_data["rq_search_version"]


async def _start_b_request_at_the_list(h) -> int:
    await h.say("/request movie b")
    assert h.user_data["rq_results"]
    return h.user_data["rq_search_version"]


async def test_a_slow_request_submit_leaves_a_newer_request_alone(h):
    release = asyncio.Event()
    call, calls = _slow_first_call(
        release, CreatedRequest(id=9, status=REQUEST_STATUS_APPROVED))
    h.app.bot_data["seerr"].create_request = call
    v_a = await _open_a_request(h)
    a = asyncio.ensure_future(h.tap(f"{RQ_GO}:{v_a}", 5000))
    await _settle()
    assert len(calls) == 1  # A is waiting on Seerr
    v_b = await _start_b_request_at_the_list(h)
    release.set()
    await a
    # B's list survives A finishing, and B can be picked and confirmed.
    assert h.user_data.get("rq_results")
    assert h.user_data["rq_search_version"] == v_b
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=5001, chat_id=CHAT_ID))
    await h.tap(f"{RQ_MEDIA}:{v_b}:movie:444", 5001)
    assert h.user_data["rq_media"]["tmdb_id"] == 444
    await h.tap(f"{RQ_GO}:{v_b}", 5001)
    assert [c["tmdb_id"] for c in calls] == [222, 444]
    assert "rq_submitting" not in h.user_data


async def test_a_newer_request_can_be_confirmed_while_the_older_submit_waits(h):
    release, release_b = asyncio.Event(), asyncio.Event()
    call, calls = _slow_first_call(
        release, CreatedRequest(id=9, status=REQUEST_STATUS_APPROVED), release_b)
    h.app.bot_data["seerr"].create_request = call
    v_a = await _open_a_request(h)
    a = asyncio.ensure_future(h.tap(f"{RQ_GO}:{v_a}", 5000))
    await _settle()
    v_b = await _start_b_request_at_the_list(h)
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=5001, chat_id=CHAT_ID))
    await h.tap(f"{RQ_MEDIA}:{v_b}:movie:444", 5001)
    b = asyncio.ensure_future(h.tap(f"{RQ_GO}:{v_b}", 5001))  # B confirms
    await _settle()
    assert [c["tmdb_id"] for c in calls] == [222, 444]
    release.set()  # A finishes first and must leave B's marker alone
    await a
    await h.tap(f"{RQ_GO}:{v_b}", 5001)  # B's double tap is still blocked
    assert len(calls) == 2
    release_b.set()
    await b
    assert "rq_submitting" not in h.user_data


async def test_a_double_tap_on_the_same_request_still_files_once(h):
    release = asyncio.Event()
    call, calls = _slow_first_call(
        release, CreatedRequest(id=9, status=REQUEST_STATUS_APPROVED))
    h.app.bot_data["seerr"].create_request = call
    v_a = await _open_a_request(h)
    a = asyncio.ensure_future(h.tap(f"{RQ_GO}:{v_a}", 5000))
    await _settle()
    await h.tap(f"{RQ_GO}:{v_a}", 5000)
    release.set()
    await a
    assert len(calls) == 1


_DRAFT = dict(media={"title": "Movie", "type": "movie", "seerr_media_id": 3,
                     "year": "2020"},
              issue_type=1, description="no audio")


def _park_issue_at_confirm(h, version: int) -> None:
    """The /issue conversation at its last step with a finished draft."""
    h.user_data.update(_DRAFT)
    h.user_data["search_version"] = version
    key = h.issue_conv._get_key(h._update({
        "update_id": 1, "message": h._message_dict(1, "x")}))
    h.issue_conv._update_state(CONFIRM_AUTOFIX, key)


async def test_a_slow_issue_submit_leaves_a_newer_issue_alone(h):
    release = asyncio.Event()
    call, calls = _slow_first_call(release, CreatedIssue(id=5, url="u"))
    h.app.bot_data["seerr"].create_issue = call
    _park_issue_at_confirm(h, 1)
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=6000, chat_id=CHAT_ID))
    a = asyncio.ensure_future(
        h.tap(f"{TICKET_AUTOFIX_CONFIRM}:1:no", 6000))
    await _settle()
    assert len(calls) == 1
    await h.say("/issue")
    await h.say("dune")  # B's title: the search list
    b_version = h.user_data["search_version"]
    assert h.user_data.get("search_results")
    release.set()
    await a
    assert h.user_data.get("search_results")
    assert h.user_data["search_version"] == b_version
    assert "submitting_ticket" not in h.user_data


async def test_a_newer_issue_can_be_confirmed_while_the_older_submit_waits(h):
    release, release_b = asyncio.Event(), asyncio.Event()
    call, calls = _slow_first_call(release, CreatedIssue(id=5, url="u"), release_b)
    h.app.bot_data["seerr"].create_issue = call
    _park_issue_at_confirm(h, 1)
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=6000, chat_id=CHAT_ID))
    a = asyncio.ensure_future(
        h.tap(f"{TICKET_AUTOFIX_CONFIRM}:1:no", 6000))
    await _settle()
    await h.say("/issue")
    _park_issue_at_confirm(h, 7)  # B reaches its own confirm
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=6001, chat_id=CHAT_ID))
    b = asyncio.ensure_future(h.tap(f"{TICKET_AUTOFIX_CONFIRM}:7:no", 6001))
    await _settle()
    assert len(calls) == 2
    release.set()  # A finishes first and must not wipe B's draft or marker
    await a
    assert h.user_data.get("media")
    await h.tap(f"{TICKET_AUTOFIX_CONFIRM}:7:no", 6001)  # B's double tap
    assert len(calls) == 2
    release_b.set()
    await b
    assert "submitting_ticket" not in h.user_data


async def test_a_double_tap_on_the_same_issue_still_files_once(h):
    release = asyncio.Event()
    call, calls = _slow_first_call(release, CreatedIssue(id=5, url="u"))
    h.app.bot_data["seerr"].create_issue = call
    _park_issue_at_confirm(h, 1)
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=6000, chat_id=CHAT_ID))
    a = asyncio.ensure_future(
        h.tap(f"{TICKET_AUTOFIX_CONFIRM}:1:no", 6000))
    await _settle()
    await h.tap(f"{TICKET_AUTOFIX_CONFIRM}:1:no", 6000)
    release.set()
    await a
    assert len(calls) == 1


# --- Auto-fix Progress card of an in-flight submit ---------------------------

def _autofix_runs(h, monkeypatch, row_ids):
    """Every Auto-fix starts a fresh download, and each enqueued poller row
    gets the next id of `row_ids`."""
    from bot import ticket_flow
    from fix_result import FixResult

    async def fix(*a, **kw):
        return FixResult.success("Deleted file and triggered re-search.",
                                 steps_done=["delete", "search"],
                                 poll_info={"movie_id": 555})
    monkeypatch.setattr(ticket_flow, "run_arr_action", fix)
    h.app.bot_data["settings_store"].settings.daily_autofix_unlimited = True
    store = h.app.bot_data["store"]
    store.add_pending_autofix = AsyncMock(side_effect=list(row_ids))
    store.set_autofix_message = AsyncMock()
    return store


def _park_autofix_at_confirm(h, version: int) -> None:
    _park_issue_at_confirm(h, version)
    h.user_data["media"] = {**h.user_data["media"], "tmdb_id": 603}


def _attached(store) -> list[int]:
    return [c.args[0] for c in store.set_autofix_message.call_args_list]


async def test_a_newer_issue_leaves_the_older_submits_autofix_card(h, monkeypatch):
    store = _autofix_runs(h, monkeypatch, [41])
    h.app.bot_data["seerr"].create_issue = AsyncMock(
        return_value=CreatedIssue(id=5, url="u"))
    release = asyncio.Event()
    note, _ = _slow_first_call(release, None)
    h.app.bot_data["seerr"].add_issue_comment = (
        lambda *a, **kw: note(**kw))
    _park_autofix_at_confirm(h, 1)
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=6000, chat_id=CHAT_ID))
    a = asyncio.ensure_future(h.tap(f"{TICKET_AUTOFIX_CONFIRM}:1:yes", 6000))
    await _settle()
    store.add_pending_autofix.assert_awaited_once()  # A is at its Ticket note
    await h.say("/issue")  # a new report clears its draft
    release.set()
    await a
    assert _attached(store) == [41]


async def test_two_submits_each_attach_their_own_autofix_card(h, monkeypatch):
    store = _autofix_runs(h, monkeypatch, [41, 42])
    h.app.bot_data["seerr"].create_issue = AsyncMock(
        return_value=CreatedIssue(id=5, url="u"))
    release = asyncio.Event()
    note, _ = _slow_first_call(release, None)
    h.app.bot_data["seerr"].add_issue_comment = (
        lambda *a, **kw: note(**kw))
    _park_autofix_at_confirm(h, 1)
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=6000, chat_id=CHAT_ID))
    a = asyncio.ensure_future(h.tap(f"{TICKET_AUTOFIX_CONFIRM}:1:yes", 6000))
    await _settle()
    await h.say("/issue")
    _park_autofix_at_confirm(h, 7)
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=6001, chat_id=CHAT_ID))
    await h.tap(f"{TICKET_AUTOFIX_CONFIRM}:7:yes", 6001)  # B runs to the end
    assert _attached(store) == [42]
    release.set()
    await a
    assert sorted(_attached(store)) == [41, 42]
