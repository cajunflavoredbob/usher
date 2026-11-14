"""Tests for the Admin's "Request for someone": the Hub button (Admin only),
the paged Linked-user picker, the normal Request flow carrying the picked
user's Seerr id, and the quota and permission wording that names them."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram.ext import ConversationHandler

import seerr as seerr_mod
from bot import hub
from bot.callback_prefixes import REQUESTS_BEHALF
from bot.request_flow import (BEHALF_PER_PAGE, request_behalf_page,
                              request_behalf_pick, request_hub_behalf,
                              request_start, request_submit)
from bot.requests_hub import render_requests_hub
from bot.shared import RQ_CONFIRM, RQ_PICK_USER, RQ_TITLE
from seerr import (REQUEST_STATUS_APPROVED, CreatedRequest,
                   QuotaExceededError, RequestPermissionError, SeerrClient)

from tests._handler_harness import make_ctx, make_link, make_update

ADMIN_ID = 999
USER_ID = 42


@pytest.fixture(autouse=True)
def _restore_hubs(monkeypatch):
    monkeypatch.setattr(hub, "HUBS", dict(hub.HUBS))


def _users(n=3):
    # (telegram_id, seerr_id, display name)
    return [(1000 + i, 20 + i, f"Person {i}") for i in range(n)]


def _admin_ctx(users=None, **kwargs):
    ctx = make_ctx(admin_id=ADMIN_ID, **kwargs)
    ctx.args = []
    ctx.bot_data["store"].list_linked_users = AsyncMock(
        return_value=_users() if users is None else users)
    return ctx


def _cbs(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# --- the Hub button ----------------------------------------------------------

async def test_admin_sees_request_for_someone_button():
    ctx = make_ctx(admin_id=ADMIN_ID)
    upd = make_update(text="/request", user_id=ADMIN_ID)
    view = await render_requests_hub(upd, ctx)
    assert REQUESTS_BEHALF in [b.callback_data for b in view.buttons]


async def test_user_does_not_see_request_for_someone_button():
    ctx = make_ctx(admin_id=ADMIN_ID)
    upd = make_update(text="/request", user_id=USER_ID)
    view = await render_requests_hub(upd, ctx)
    assert REQUESTS_BEHALF not in [b.callback_data for b in view.buttons]


async def test_user_tapping_forged_button_gets_nothing():
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID))
    ctx.bot_data["store"].list_linked_users = AsyncMock(return_value=_users())
    upd = make_update(callback_data=REQUESTS_BEHALF, user_id=USER_ID)
    state = await request_hub_behalf(upd, ctx)
    assert state == ConversationHandler.END
    assert upd.callback_query.edits == []
    ctx.bot_data["store"].list_linked_users.assert_not_awaited()


# --- the picker --------------------------------------------------------------

async def test_admin_tap_lists_linked_users_by_seerr_id():
    ctx = _admin_ctx()
    upd = make_update(callback_data=REQUESTS_BEHALF, user_id=ADMIN_ID)
    state = await request_hub_behalf(upd, ctx)
    assert state == RQ_PICK_USER
    markup = upd.callback_query.edits[-1]["reply_markup"]
    assert _cbs(markup)[:3] == ["rqbu:20", "rqbu:21", "rqbu:22"]


async def test_picker_pages_past_one_screen():
    ctx = _admin_ctx(users=_users(BEHALF_PER_PAGE + 2))
    upd = make_update(callback_data=REQUESTS_BEHALF, user_id=ADMIN_ID)
    await request_hub_behalf(upd, ctx)
    first = _cbs(upd.callback_query.edits[-1]["reply_markup"])
    assert "rqbp:1" in first and "rqbu:20" in first
    assert f"rqbu:{20 + BEHALF_PER_PAGE}" not in first

    upd2 = make_update(callback_data="rqbp:1", user_id=ADMIN_ID)
    state = await request_behalf_page(upd2, ctx)
    assert state == RQ_PICK_USER
    second = _cbs(upd2.callback_query.edits[-1]["reply_markup"])
    assert f"rqbu:{20 + BEHALF_PER_PAGE}" in second
    assert "rqbp:0" in second


async def test_no_linked_users_ends_with_message():
    ctx = _admin_ctx(users=[])
    upd = make_update(callback_data=REQUESTS_BEHALF, user_id=ADMIN_ID)
    state = await request_hub_behalf(upd, ctx)
    assert state == ConversationHandler.END
    assert "No one has linked" in upd.callback_query.edits[-1]["text"]


async def test_pick_names_user_and_asks_for_title():
    ctx = _admin_ctx()
    upd = make_update(callback_data="rqbu:21", user_id=ADMIN_ID)
    state = await request_behalf_pick(upd, ctx)
    assert state == RQ_TITLE
    assert ctx.user_data["rq_for"]["seerr_id"] == 21
    assert "Person 1" in upd.callback_query.edits[-1]["text"]


async def test_pick_of_unlinked_user_ends_flow():
    ctx = _admin_ctx()
    upd = make_update(callback_data="rqbu:555", user_id=ADMIN_ID)
    state = await request_behalf_pick(upd, ctx)
    assert state == ConversationHandler.END
    assert "rq_for" not in ctx.user_data


async def test_typed_request_forgets_an_earlier_pick():
    ctx = _admin_ctx(user_data={"rq_for": {"seerr_id": 21, "name": "X",
                                           "telegram_id": 1}})
    ctx.args = []
    upd = make_update(text="/request", user_id=ADMIN_ID)
    await request_start(upd, ctx)
    assert "rq_for" not in ctx.user_data


# --- submit ------------------------------------------------------------------

def _confirm_ctx(**kwargs):
    ctx = _admin_ctx(user_data={
        "rq_media": {"type": "movie", "tmdb_id": 1, "title": "New Movie",
                     "year": "2026"},
        "rq_for": {"seerr_id": 21, "name": "Person 1", "telegram_id": 1001},
        "rq_search_version": 1,
    }, **kwargs)
    return ctx


async def test_submit_carries_picked_users_seerr_id():
    ctx = _confirm_ctx()
    ctx.bot_data["seerr"].create_request.return_value = CreatedRequest(
        id=5, status=REQUEST_STATUS_APPROVED)
    upd = make_update(callback_data="rqgo:1", user_id=ADMIN_ID)
    state = await request_submit(upd, ctx)
    assert state == ConversationHandler.END
    kwargs = ctx.bot_data["seerr"].create_request.call_args.kwargs
    assert kwargs["user_id"] == 21
    assert kwargs["as_plex_token"] is None
    assert "Person 1" in upd.callback_query.edits[-1]["text"]


async def test_submit_without_pick_sends_no_user_id():
    ctx = _admin_ctx(user_data={
        "rq_media": {"type": "movie", "tmdb_id": 1, "title": "M", "year": ""},
        "rq_search_version": 1})
    ctx.bot_data["seerr"].create_request.return_value = CreatedRequest(
        id=5, status=REQUEST_STATUS_APPROVED)
    upd = make_update(callback_data="rqgo:1", user_id=ADMIN_ID)
    await request_submit(upd, ctx)
    assert ctx.bot_data["seerr"].create_request.call_args.kwargs["user_id"] is None


async def test_quota_error_names_the_user():
    ctx = _confirm_ctx()
    ctx.bot_data["seerr"].create_request.side_effect = QuotaExceededError(
        "Movie Quota exceeded.")
    upd = make_update(callback_data="rqgo:1", user_id=ADMIN_ID)
    await request_submit(upd, ctx)
    text = upd.callback_query.edits[-1]["text"]
    assert "Person 1" in text and "Movie Quota exceeded." in text
    assert "Your quota" not in text


async def test_permission_error_names_the_user():
    ctx = _confirm_ctx()
    ctx.bot_data["seerr"].create_request.side_effect = RequestPermissionError(
        "You do not have permission to make requests.")
    upd = make_update(callback_data="rqgo:1", user_id=ADMIN_ID)
    await request_submit(upd, ctx)
    text = upd.callback_query.edits[-1]["text"]
    assert "Person 1" in text and "permission" in text


# --- wire and store ----------------------------------------------------------

async def test_create_request_sends_user_id_in_body(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 7, "status": 1})

    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(seerr_mod.httpx, "AsyncClient", patched)
    client = SeerrClient("http://seerr.test", "key")
    await client.create_request(media_type="movie", tmdb_id=550, user_id=21)
    assert captured["body"] == {"mediaType": "movie", "mediaId": 550,
                                "userId": 21}
    await client.close()


async def test_list_linked_users_sorted_by_name(fresh_store):
    for tg, sid, name in ((3, 30, "zed"), (1, 10, "Amy"), (2, 20, "bob")):
        await fresh_store.link_with_plex(
            telegram_id=tg, seerr_id=sid, seerr_display=name,
            plex_token="tok", plex_uuid=f"u{tg}", plex_username=f"p{tg}")
    assert await fresh_store.list_linked_users() == [
        (1, 10, "Amy"), (2, 20, "bob"), (3, 30, "zed")]


async def test_stale_pick_does_not_change_remaining_seasons_quota():
    from bot.request_flow import remaining_seasons_ask
    from seerr import SeasonAvailability
    ctx = _admin_ctx(user_data={"rq_for": {"seerr_id": 21, "name": "Person 1",
                                           "telegram_id": 1001}})
    seerr = ctx.bot_data["seerr"]
    seerr.get_tv_season_availability.return_value = [
        SeasonAvailability(1, True)]
    upd = make_update(callback_data="rqrem:1396", user_id=ADMIN_ID)
    await remaining_seasons_ask(upd, ctx)
    # The quota lookup is for the Admin's own link, never the stale pick.
    assert all(c.args[0] != 21 for c in seerr.get_quota.call_args_list)
