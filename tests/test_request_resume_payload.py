"""A relink resume files the request that was interrupted, never a newer
/request the user left unconfirmed. Driven through a real PTB Application with
the real link and request conversations, reset_stale_flows and global_btn_gate
wired as in bot/app.py; only the Telegram network calls and the Plex/Seerr
clients are faked. Reuses the rig of test_issue_resume_superseded."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update, User
from telegram.ext import Application, CallbackContext, TypeHandler

import bot.link_flow as link_flow
from bot.callback_prefixes import RELINK
from bot.link_flow import _link_conversation
from bot.request_flow import _request_conversation
from bot.shared import (global_btn_gate, guard_flow_steps, prompt_plex_relink,
                        record_btn, reset_stale_flows)
from seerr import CreatedRequest, MediaResult, REQUEST_STATUS_APPROVED
from tests._handler_harness import make_ctx, make_link
from tests.test_issue_resume_superseded import USER_ID, _Harness

A_PAYLOAD = {"media": {"type": "movie", "tmdb_id": 111, "title": "Movie A",
                       "year": "2020"},
             "seasons": None, "is4k": False}


@pytest.fixture
async def h(monkeypatch):
    monkeypatch.setattr(link_flow, "PLEX_POLL_INTERVAL_S", 0)
    app = Application.builder().token("1:x").build()
    app.bot._bot_user = User(id=1, first_name="Usher", is_bot=True,
                             username="UsherBot")
    app._initialized = True
    harness = _Harness(app, monkeypatch)

    ctx = make_ctx(admin_id=999, link=make_link(telegram_id=USER_ID))
    app.bot_data.update(ctx.bot_data)
    app.bot_data["store"].link_with_plex = AsyncMock()
    seerr = app.bot_data["seerr"]
    seerr.login_with_plex = AsyncMock(return_value=(7, "User1"))
    seerr.create_request = AsyncMock(return_value=CreatedRequest(
        id=9, status=REQUEST_STATUS_APPROVED))
    seerr.get_my_permissions = AsyncMock(return_value=0)
    seerr.get_media_title = AsyncMock(return_value=("Movie B", "2021"))
    plex = app.bot_data["plex"]
    plex.request_pin = AsyncMock(return_value=SimpleNamespace(
        id=1, code="ABCD", auth_url="https://plex.example/auth"))
    plex.poll_pin = AsyncMock(return_value="new-token")
    plex.get_user = AsyncMock(return_value=SimpleNamespace(
        uuid="u", username="user1plex"))

    link_conv = _link_conversation()
    request_conv = _request_conversation()
    app.bot_data["conversations"] = {"link": link_conv, "request": request_conv}
    app.bot_data["flow_convs"] = [link_conv, request_conv]
    for conv in app.bot_data["flow_convs"]:
        guard_flow_steps(conv)
    app.add_handler(TypeHandler(Update, reset_stale_flows), group=-2)
    app.add_handler(TypeHandler(Update, global_btn_gate), group=-1)
    app.add_handler(link_conv)
    app.add_handler(request_conv)
    yield harness


async def _interrupted_request(h, payload=A_PAYLOAD) -> int:
    """A submit that hit a revoked token: the relink prompt on screen. Returns
    the prompt's message id."""
    h._ids += 1
    upd = h._update({"update_id": h._ids,
                     "message": h._message_dict(h._ids, "ok")})
    ctx = CallbackContext.from_update(upd, h.app)
    await prompt_plex_relink(upd, ctx, resume_kind="submit_request",
                             resume_payload=payload, reply=True)
    return h._ids


async def _start_b_unconfirmed(h) -> None:
    """Movie B picked from a jump button and left at the confirm screen."""
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=5000, chat_id=USER_ID))
    await h.tap("rqfi:movie:222", 5000)
    assert h.user_data["rq_media"]["tmdb_id"] == 222


def _filed(h) -> list[int]:
    return [c.kwargs["tmdb_id"]
            for c in h.app.bot_data["seerr"].create_request.call_args_list]


async def test_b_ended_by_a_command_then_relink_files_a_not_b(h):
    prompt = await _interrupted_request(h)
    await _start_b_unconfirmed(h)
    await h.say("/tickets")  # force-ends B
    assert "rq_media" not in h.user_data
    texts = await h.relink(prompt)
    assert _filed(h) == [111], texts
    assert any("Movie A" in t and "Approved" in t for t in texts), texts
    assert not any("Movie B" in t and "Approved" in t for t in texts), texts


async def test_b_still_open_then_relink_files_nothing(h):
    prompt = await _interrupted_request(h)
    await _start_b_unconfirmed(h)
    texts = await h.relink(prompt)
    assert _filed(h) == [], texts
    assert any("started a new /request" in t for t in texts), texts


async def test_control_no_detour_files_a(h):
    prompt = await _interrupted_request(h)
    texts = await h.relink(prompt)
    assert _filed(h) == [111], texts
    assert any("Movie A" in t and "Approved" in t for t in texts), texts


async def test_a_four_k_tv_request_resumes_with_its_seasons_and_tier(h):
    prompt = await _interrupted_request(h, {
        "media": {"type": "tv", "tmdb_id": 333, "title": "Show A",
                  "year": "2019"},
        "seasons": [1, 3], "is4k": True})
    await _start_b_unconfirmed(h)
    await h.say("/tickets")
    await h.relink(prompt)
    kwargs = h.app.bot_data["seerr"].create_request.call_args.kwargs
    assert kwargs["tmdb_id"] == 333
    assert kwargs["seasons"] == [1, 3]
    assert kwargs["is4k"] is True


async def test_an_old_marker_with_an_empty_payload_says_the_draft_is_gone(h):
    prompt = await _interrupted_request(h, {})
    await _start_b_unconfirmed(h)
    await h.say("/tickets")
    texts = await h.relink(prompt)
    assert _filed(h) == [], texts
    assert any("draft is gone" in t for t in texts), texts


async def test_a_jump_over_an_open_request_keeps_the_four_k_permission(h):
    from seerr import PERMISSION_REQUEST_4K
    h.app.bot_data["seerr"].get_my_permissions = AsyncMock(
        return_value=PERMISSION_REQUEST_4K)
    await _start_b_unconfirmed(h)  # an open /request
    record_btn(h.app, USER_ID, SimpleNamespace(message_id=5001, chat_id=USER_ID))
    from seerr import MediaResult
    h.user_data["dv_results"] = [MediaResult("movie", 333, "Movie C", "2022", 3)]
    await h.tap("rqfi:movie:333", 5001)  # a /trending tap while it is open
    assert h.user_data["rq_media"]["tmdb_id"] == 333
    assert h.user_data["rq_perms"] == PERMISSION_REQUEST_4K


# --- a resume still waiting on Seerr leaves a newer /request alone ------------

async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


def _slow_create(h) -> asyncio.Event:
    """create_request waits for the returned event on its first call."""
    release = asyncio.Event()
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            await release.wait()
        return CreatedRequest(id=9, status=REQUEST_STATUS_APPROVED)
    h.app.bot_data["seerr"].create_request = create
    h.app.bot_data["seerr"].search = AsyncMock(return_value=[
        MediaResult("movie", 444, "Movie B", "2021", None)])
    return release


async def _start_b_at_the_list(h) -> None:
    await h.say("/request movie b")
    assert h.user_data["rq_results"]


async def test_a_resume_waiting_on_seerr_leaves_a_newer_request_alone(h):
    release = _slow_create(h)
    prompt = await _interrupted_request(h)
    resume = asyncio.ensure_future(h.relink(prompt))  # strong PIN
    await _settle()
    await _start_b_at_the_list(h)
    version = h.user_data["rq_search_version"]
    release.set()
    await resume
    assert any("Movie A" in t and "Approved" in t for t in h.sent), h.sent
    assert h.user_data.get("rq_results")
    assert h.user_data["rq_search_version"] == version


async def test_a_weak_pin_resume_waiting_on_seerr_leaves_a_newer_request_alone(h):
    from telegram.ext import CallbackQueryHandler
    from bot.callback_prefixes import LINK_HELP, LINK_PLATFORM
    from bot.link_flow import cmd_link_didnt_work
    h.app.add_handler(CallbackQueryHandler(cmd_link_didnt_work,
                                           pattern=f"^{LINK_HELP}$"))
    plex = h.app.bot_data["plex"]

    async def request_pin(strong=True):
        return SimpleNamespace(id=1 if strong else 2, code="ABCD",
                               auth_url="https://plex.example/auth")

    async def poll_pin(pin_id):
        return "new-token" if pin_id == 2 else None  # only the weak PIN signs in
    plex.request_pin = request_pin
    plex.poll_pin = poll_pin
    release = _slow_create(h)
    prompt = await _interrupted_request(h)
    await h.tap(RELINK, prompt)
    strong = asyncio.ensure_future(h.tap(f"{LINK_PLATFORM}:desktop", prompt))
    await _settle()
    weak = asyncio.ensure_future(h.tap(LINK_HELP, prompt))  # "Having trouble?"
    await _settle()
    await _start_b_at_the_list(h)
    version = h.user_data["rq_search_version"]
    release.set()
    await weak
    await strong
    assert any("Movie A" in t and "Approved" in t for t in h.sent), h.sent
    assert h.user_data.get("rq_results")
    assert h.user_data["rq_search_version"] == version
