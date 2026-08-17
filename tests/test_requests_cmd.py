"""Tests for /requests: list rendering with statuses, cancel buttons for
pending entries only, per-user attribution of the fetch, and the cancel
callback's delete + re-render."""
from __future__ import annotations

from seerr import (
    REQUEST_STATUS_APPROVED,
    REQUEST_STATUS_PENDING,
    RequestListItem,
)
from bot.requests_cmd import cmd_requests, rq_list_cancel

from tests._handler_harness import make_ctx, make_mapping, make_update

USER_ID = 42
ADMIN_ID = 999


def _items():
    return [
        RequestListItem(id=12, status=REQUEST_STATUS_PENDING,
                        media_type="movie", tmdb_id=550,
                        created_at="2026-08-16T10:00:00.000Z",
                        requested_by="user1"),
        RequestListItem(id=13, status=REQUEST_STATUS_APPROVED,
                        media_type="tv", tmdb_id=1399, seasons=[1, 2],
                        created_at="2026-08-16T09:00:00.000Z",
                        requested_by="user2"),
    ]


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def test_list_renders_statuses_and_cancel_buttons():
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=make_mapping(telegram_id=USER_ID))
    ctx.bot_data["seerr"].list_requests.return_value = (_items(), 2)
    upd = make_update(text="/requests", user_id=USER_ID)
    await cmd_requests(upd, ctx)
    call = upd.effective_message.reply_calls[-1]
    text = call["text"]
    assert "#12" in text and "pending approval" in text
    assert "#13" in text and "approved, grabbing" in text
    assert "S1-S2" in text
    # Only the pending request gets a cancel button.
    assert _callbacks(call["reply_markup"]) == ["rqlc:12"]
    # Fetch rode the user's own session, not the admin key.
    kwargs = ctx.bot_data["seerr"].list_requests.call_args.kwargs
    assert kwargs["as_plex_token"] == "plex-abc"


async def test_admin_list_shows_requesters_and_uses_admin_key():
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=make_mapping(telegram_id=ADMIN_ID))
    ctx.bot_data["seerr"].list_requests.return_value = (_items(), 2)
    upd = make_update(text="/requests", user_id=ADMIN_ID)
    await cmd_requests(upd, ctx)
    call = upd.effective_message.reply_calls[-1]
    assert "user1" in call["text"]
    assert ctx.bot_data["seerr"].list_requests.call_args.kwargs["as_plex_token"] is None


async def test_unlinked_user_is_gated():
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=None)
    upd = make_update(text="/requests", user_id=USER_ID)
    await cmd_requests(upd, ctx)
    assert "link" in upd.effective_message.reply_calls[0]["text"]
    ctx.bot_data["seerr"].list_requests.assert_not_awaited()


async def test_empty_list_message():
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=make_mapping(telegram_id=USER_ID))
    ctx.bot_data["seerr"].list_requests.return_value = ([], 0)
    upd = make_update(text="/requests", user_id=USER_ID)
    await cmd_requests(upd, ctx)
    assert "haven't requested anything" in upd.effective_message.reply_calls[0]["text"]


async def test_cancel_deletes_and_rerenders():
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=make_mapping(telegram_id=USER_ID))
    ctx.bot_data["seerr"].list_requests.return_value = ([_items()[1]], 1)
    upd = make_update(callback_data="rqlc:12", user_id=USER_ID)
    await rq_list_cancel(upd, ctx)
    kwargs = ctx.bot_data["seerr"].delete_request.call_args
    assert kwargs.args[0] == 12
    assert kwargs.kwargs["as_plex_token"] == "plex-abc"
    # List re-rendered in place after the delete, success notice up top,
    # and the callback was answered BEFORE the network work.
    text = upd.callback_query.edits[0]["text"]
    assert "#12 cancelled" in text and "#13" in text
    assert upd.callback_query.answers


async def test_cancel_failure_surfaces_reason():
    """The callback is answered up front (a slow DELETE would outlive the
    query's validity), so failure feedback rides the re-rendered list."""
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=make_mapping(telegram_id=USER_ID))
    ctx.bot_data["seerr"].delete_request.side_effect = RuntimeError("boom")
    ctx.bot_data["seerr"].list_requests.return_value = ([_items()[0]], 1)
    upd = make_update(callback_data="rqlc:12", user_id=USER_ID)
    await rq_list_cancel(upd, ctx)
    text = upd.callback_query.edits[0]["text"]
    assert "Couldn't cancel #12" in text
    assert "#12" in text  # list still rendered below the warning


async def test_cancel_not_found_treated_as_success():
    """A 404 on DELETE (already cancelled, or our own retried DELETE) reads
    as success, not a scary failure alert."""
    from http_util import NotFoundAPIError
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=make_mapping(telegram_id=USER_ID))
    ctx.bot_data["seerr"].delete_request.side_effect = NotFoundAPIError(
        "gone", status_code=404, service="Seerr")
    ctx.bot_data["seerr"].list_requests.return_value = ([], 0)
    upd = make_update(callback_data="rqlc:12", user_id=USER_ID)
    await rq_list_cancel(upd, ctx)
    assert "cancelled" in upd.callback_query.edits[0]["text"]
