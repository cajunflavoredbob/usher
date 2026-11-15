"""A confirm's double-tap guard is keyed on the draft's own identity (owner
chat plus a per-draft id), not on the user's flow generation: a command in
another chat bumps that between the two taps, and the second tap must still
find the first one's submit running. Driven through the real Application like
tests/test_flows_across_chats.py. Also: a Cancel button tapped during a submit
answers "Already sending" and leaves the send alone."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.callback_prefixes import (INV_DONE, INV_GO, INV_CANCEL, RQ_CANCEL,
                                   RQ_GO, TICKET_AUTOFIX_OFFER)
from seerr import CreatedIssue
from tests.test_flows_across_chats import (  # noqa: F401
    DM_ID, GROUP_ID, USER_ID, _request_to_confirm, _requests, rig, rq_rig)


def _slow(release: asyncio.Event, calls: list, result=None):
    async def call(*args, **kwargs):
        calls.append(kwargs or args)
        # Bounded: a guard that lets a second submit in must fail, not hang.
        await asyncio.wait_for(release.wait(), 3)
        return result
    return call


async def _until(calls: list) -> None:
    for _ in range(200):
        if calls:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the submit never reached its network call")


async def _issue_at_offer(rig) -> tuple:
    await rig.issue_to_description(DM_ID, "movie x", 1)
    await rig.say(DM_ID, "the picture is green")
    version = rig.user_data["search_version"]
    return version, rig.last_menu(DM_ID)


async def test_an_issue_double_tap_with_a_group_command_between_files_once(rig):
    release, calls = asyncio.Event(), []
    rig.seerr.create_issue = _slow(release, calls, CreatedIssue(id=5, url="u"))
    version, menu = await _issue_at_offer(rig)
    data = f"{TICKET_AUTOFIX_OFFER}:{version}:no"
    first = asyncio.ensure_future(rig.tap(DM_ID, data, menu))
    await _until(calls)
    await rig.say(GROUP_ID, "/weather")  # bumps the user's flow generation
    await rig.tap(DM_ID, data, menu)
    release.set()
    await first
    assert len(calls) == 1


async def test_a_request_double_tap_with_a_group_command_between_files_once(rq_rig):
    rig = rq_rig
    release, calls = asyncio.Event(), []
    rig.seerr.create_request = _slow(release, calls, rig.seerr.create_request.return_value)
    version = await _request_to_confirm(rig, DM_ID, "movie x")
    menu = rig.last_menu(DM_ID)
    first = asyncio.ensure_future(rig.tap(DM_ID, f"{RQ_GO}:{version}", menu))
    await _until(calls)
    await rig.say(GROUP_ID, "/weather")
    await rig.tap(DM_ID, f"{RQ_GO}:{version}", menu)
    release.set()
    await first
    assert len(calls) == 1


async def _invite_at_confirm(rig):
    plex = rig.app.bot_data["plex"]
    plex.get_owned_server = AsyncMock(return_value=("m1", "Server"))
    plex.get_library_sections = AsyncMock(return_value=[
        SimpleNamespace(id=1, title="Movies", type="movie")])
    await rig.say(DM_ID, "/invite")
    await rig.say(DM_ID, "friend@example.com")
    version = rig.user_data["inv_version"]
    await rig.tap(DM_ID, f"{INV_DONE}:{version}", rig.last_menu(DM_ID))
    return plex, version, rig.last_menu(DM_ID)


async def test_an_invite_double_tap_with_a_group_command_between_sends_once(rig):
    release, calls = asyncio.Event(), []
    plex, version, menu = await _invite_at_confirm(rig)
    plex.invite_to_server = _slow(release, calls)
    first = asyncio.ensure_future(rig.tap(DM_ID, f"{INV_GO}:{version}", menu))
    await _until(calls)
    await rig.say(GROUP_ID, "/weather")
    await rig.tap(DM_ID, f"{INV_GO}:{version}", menu)
    release.set()
    await first
    assert len(calls) == 1


async def test_a_newer_draft_in_the_group_is_not_blocked_by_the_dm_submit(rq_rig):
    """The guard is per draft: a request started in the group while the DM's
    submit runs has an identity of its own and files."""
    rig = rq_rig
    release, calls = asyncio.Event(), []
    rig.seerr.create_request = _slow(release, calls, rig.seerr.create_request.return_value)
    dm_version = await _request_to_confirm(rig, DM_ID, "movie x")
    first = asyncio.ensure_future(
        rig.tap(DM_ID, f"{RQ_GO}:{dm_version}", rig.last_menu(DM_ID)))
    await _until(calls)
    group_version = await _request_to_confirm(rig, GROUP_ID, "alien")
    second = asyncio.ensure_future(
        rig.tap(GROUP_ID, f"{RQ_GO}:{group_version}", rig.last_menu(GROUP_ID)))
    for _ in range(200):
        if len(calls) == 2:
            break
        await asyncio.sleep(0.01)
    assert len(calls) == 2
    release.set()
    await first
    await second


# --- a Cancel button tapped while the submit runs ------------------------------


async def test_a_request_cancel_button_during_the_submit_says_already_sending(rq_rig):
    rig = rq_rig
    release, calls = asyncio.Event(), []
    rig.seerr.create_request = _slow(release, calls, rig.seerr.create_request.return_value)
    version = await _request_to_confirm(rig, DM_ID, "movie x")
    menu = rig.last_menu(DM_ID)
    answers = []

    async def answer(self_, callback_query_id=None, text=None, *a, **kw):
        answers.append(text)
        return True
    from telegram.ext import ExtBot
    from unittest.mock import patch
    first = asyncio.ensure_future(rig.tap(DM_ID, f"{RQ_GO}:{version}", menu))
    await _until(calls)
    with patch.object(ExtBot, "answer_callback_query", answer):
        await rig.tap(DM_ID, f"{RQ_CANCEL}:{version}", menu)
    assert any(t and "Already sending" in t for t in answers), answers
    assert not any(t == "Cancelled" for t in answers)
    release.set()
    await first
    assert len(calls) == 1


async def test_an_invite_cancel_button_during_the_send_says_already_sending(rig):
    release, calls = asyncio.Event(), []
    plex, version, menu = await _invite_at_confirm(rig)
    plex.invite_to_server = _slow(release, calls)
    answers = []

    async def answer(self_, callback_query_id=None, text=None, *a, **kw):
        answers.append(text)
        return True
    from telegram.ext import ExtBot
    from unittest.mock import patch
    first = asyncio.ensure_future(rig.tap(DM_ID, f"{INV_GO}:{version}", menu))
    await _until(calls)
    with patch.object(ExtBot, "answer_callback_query", answer):
        await rig.tap(DM_ID, f"{INV_CANCEL}:{version}", menu)
    assert any(t and "Already sending" in t for t in answers), answers
    release.set()
    await first
    assert len(calls) == 1


async def test_an_issue_cancel_button_during_the_submit_says_already_sending():
    from bot.callback_prefixes import TICKET_CANCEL
    from bot.shared import begin_submit, claim_draft
    from bot.ticket_flow import ticket_cancel
    from tests._handler_harness import make_ctx, make_link, make_update
    ctx = make_ctx(admin_id=999, link=make_link(telegram_id=USER_ID),
                   user_data={"search_version": 1, "media": {"title": "M"}})
    upd = make_update(callback_data=f"{TICKET_CANCEL}:1", user_id=USER_ID)
    claim_draft(ctx, upd, "issue_chat")
    assert begin_submit(ctx, "submitting_ticket", "issue_chat") is not None
    answers = []

    async def answer(text=None, *a, **kw):
        answers.append(text)
    upd.callback_query.answer = answer
    state = await ticket_cancel(upd, ctx)
    assert state == -1  # ConversationHandler.END
    assert answers and "Already sending" in answers[0]
    assert ctx.user_data["media"] == {"title": "M"}  # nothing cleared
    assert "submitting_ticket" in ctx.user_data
