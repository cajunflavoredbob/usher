"""A conversation timeout never fires for a conversation that has ended.

A typed /cancel while an /issue, /request or /invite submit is running is
answered "Already sending"; that answer used to return None, so PTB armed a
fresh conversation timeout the submit's own END never removed, and ten minutes
later "The /issue flow timed out" appeared under the filed report and the
handler cleared a newer flow's draft. Driven through the real Application with
a JobQueue whose scheduler is running (paused, so no job fires by itself: the
tests fire the jobs PTB scheduled). Every timeout handler also does nothing
when its conversation no longer holds the update's key."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.callback_prefixes import INV_DONE, INV_GO, RQ_GO
from seerr import CreatedIssue, CreatedRequest, REQUEST_STATUS_APPROVED
from tests._handler_harness import make_ctx, make_update
from tests.test_flows_across_chats import (DM_ID, GROUP_ID, USER_ID, _Rig,
                                           _request_to_confirm)


@pytest.fixture
async def jq_rig(monkeypatch):
    rig = _Rig(monkeypatch)
    scheduler = rig.app.job_queue.scheduler
    scheduler.start(paused=True)
    try:
        yield rig
    finally:
        scheduler.shutdown(wait=False)


def _blocking(result):
    started, release = asyncio.Event(), asyncio.Event()

    async def call(*a, **kw):
        started.set()
        await release.wait()
        return result

    return call, started, release


async def _fire_dm_timeouts(rig) -> None:
    """Fire every timeout PTB left armed for the DM's conversations."""
    for name in ("issue", "request", "invite"):
        conv = rig.app.bot_data["conversations"][name]
        job = conv.timeout_jobs.get((DM_ID, USER_ID))
        if job is not None:
            await job.run(rig.app)


async def _cancel_while(rig, send_coro, started) -> None:
    task = asyncio.create_task(send_coro)
    await asyncio.wait_for(started.wait(), 5)
    before = len(rig.sent)
    await rig.say(DM_ID, "/cancel")
    assert any("Already sending" in t for _c, t in rig.sent[before:])
    return task


async def _assert_no_timeout_after(rig, name: str) -> None:
    conv = rig.app.bot_data["conversations"][name]
    assert (DM_ID, USER_ID) not in conv.timeout_jobs
    # A newer flow in the group has a draft the old timeout must not touch.
    await rig.issue_to_description(GROUP_ID, "alien", 4)
    before = len(rig.sent)
    await _fire_dm_timeouts(rig)
    assert not any("timed out" in t for c, t in rig.sent[before:] if c == DM_ID)
    assert rig.user_data["media"]["title"] == "Alien"


async def test_an_issue_cancelled_while_sending_arms_no_timeout(jq_rig):
    rig = jq_rig
    call, started, release = _blocking(
        CreatedIssue(id=5, url="https://seerr.example/5"))
    rig.seerr.create_issue = AsyncMock(side_effect=call)
    await rig.issue_to_description(DM_ID, "movie x", 4)
    task = await _cancel_while(rig, rig.say(DM_ID, "the picture is green"), started)
    release.set()
    await asyncio.wait_for(task, 5)
    rig.seerr.create_issue.assert_awaited_once()
    await _assert_no_timeout_after(rig, "issue")


async def test_a_request_cancelled_while_sending_arms_no_timeout(jq_rig):
    rig = jq_rig
    call, started, release = _blocking(
        CreatedRequest(id=9, status=REQUEST_STATUS_APPROVED))
    rig.seerr.create_request = AsyncMock(side_effect=call)
    version = await _request_to_confirm(rig, DM_ID, "movie x")
    task = await _cancel_while(
        rig, rig.tap(DM_ID, f"{RQ_GO}:{version}", rig.last_menu(DM_ID)), started)
    release.set()
    await asyncio.wait_for(task, 5)
    rig.seerr.create_request.assert_awaited_once()
    await _assert_no_timeout_after(rig, "request")


async def test_an_invite_cancelled_while_sending_arms_no_timeout(jq_rig):
    rig = jq_rig
    plex = rig.app.bot_data["plex"]
    plex.get_owned_server = AsyncMock(return_value=("m1", "Server"))
    plex.get_library_sections = AsyncMock(return_value=[
        SimpleNamespace(id=1, title="Movies", type="movie")])
    call, started, release = _blocking(None)
    plex.invite_to_server = AsyncMock(side_effect=call)
    await rig.say(DM_ID, "/invite")
    await rig.say(DM_ID, "friend@example.com")
    version = rig.user_data["inv_version"]
    await rig.tap(DM_ID, f"{INV_DONE}:{version}", rig.last_menu(DM_ID))
    task = await _cancel_while(
        rig, rig.tap(DM_ID, f"{INV_GO}:{version}", rig.last_menu(DM_ID)), started)
    release.set()
    await asyncio.wait_for(task, 5)
    plex.invite_to_server.assert_awaited_once()
    await _assert_no_timeout_after(rig, "invite")


async def test_a_live_conversation_still_times_out(jq_rig):
    rig = jq_rig
    await rig.issue_to_description(DM_ID, "movie x", 4)
    conv = rig.app.bot_data["conversations"]["issue"]
    job = conv.timeout_jobs[(DM_ID, USER_ID)]
    await job.run(rig.app)
    assert any("timed out" in t for t in rig.texts(DM_ID))


# --- every timeout handler: a conversation that has ended does nothing -------


def _handlers():
    from bot.broadcast import _timeout
    from bot.invite_flow import _invite_timeout
    from bot.link_flow import _link_timeout
    from bot.request_flow import _request_timeout
    from bot.resolve_flow import _resolve_timeout
    from bot.ticket_flow import _ticket_timeout
    from bot.tickets import tk_reply_timeout
    return [
        (_ticket_timeout, "issue", {"media": {"title": "Alien"}}),
        (_request_timeout, "request", {"rq_media": {"title": "Alien"}}),
        (_invite_timeout, "invite", {"inv_email": "friend@example.com"}),
        (_link_timeout, "link", {"link_active_loop": "loop"}),
        (_resolve_timeout, "resolve", {"awaiting_comment_for": 2,
                                       "awaiting_comment_chat": DM_ID}),
        (tk_reply_timeout, "ticket_reply", {"tk_reply_id": 2,
                                            "tk_reply_chat": DM_ID}),
        (_timeout, "broadcast", {"bc_text": "hello"}),
    ]


def _ctx(name: str, live: bool, user_data: dict):
    ctx = make_ctx(admin_id=USER_ID, user_data=dict(user_data))
    key = (DM_ID, USER_ID)
    ctx.bot_data["conversations"] = {name: SimpleNamespace(
        _conversations={key: 1} if live else {},
        _get_key=lambda u: (u.effective_chat.id, u.effective_user.id))}
    return ctx


@pytest.mark.parametrize("index", range(7))
async def test_a_timeout_for_an_ended_conversation_does_nothing(index):
    handler, name, data = _handlers()[index]
    ctx = _ctx(name, live=False, user_data=data)
    await handler(make_update(text="x", user_id=USER_ID, chat_id=DM_ID), ctx)
    assert ctx.user_data == data
    ctx.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("index", range(7))
async def test_a_timeout_for_a_live_conversation_still_clears(index):
    handler, name, data = _handlers()[index]
    ctx = _ctx(name, live=True, user_data=data)
    await handler(make_update(text="x", user_id=USER_ID, chat_id=DM_ID), ctx)
    assert ctx.user_data != data
