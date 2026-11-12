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

from tests._handler_harness import make_ctx, make_link, make_update

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
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID))
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
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=ADMIN_ID))
    ctx.bot_data["seerr"].list_requests.return_value = (_items(), 2)
    upd = make_update(text="/requests", user_id=ADMIN_ID)
    await cmd_requests(upd, ctx)
    call = upd.effective_message.reply_calls[-1]
    assert "user1" in call["text"]
    assert ctx.bot_data["seerr"].list_requests.call_args.kwargs["as_plex_token"] is None


async def test_unlinked_user_is_gated():
    ctx = make_ctx(admin_id=ADMIN_ID, link=None)
    upd = make_update(text="/requests", user_id=USER_ID)
    await cmd_requests(upd, ctx)
    assert "link" in upd.effective_message.reply_calls[0]["text"]
    ctx.bot_data["seerr"].list_requests.assert_not_awaited()


async def test_empty_list_message():
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID))
    ctx.bot_data["seerr"].list_requests.return_value = ([], 0)
    upd = make_update(text="/requests", user_id=USER_ID)
    await cmd_requests(upd, ctx)
    assert "haven't requested anything" in upd.effective_message.reply_calls[0]["text"]


async def test_cancel_deletes_and_rerenders():
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID))
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
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID))
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
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID))
    ctx.bot_data["seerr"].delete_request.side_effect = NotFoundAPIError(
        "gone", status_code=404, service="Seerr")
    ctx.bot_data["seerr"].list_requests.return_value = ([], 0)
    upd = make_update(callback_data="rqlc:12", user_id=USER_ID)
    await rq_list_cancel(upd, ctx)
    assert "cancelled" in upd.callback_query.edits[0]["text"]


def _card(card_id, user_id, *, media_type="movie", tmdb_id=550, message_id=700):
    return {"id": card_id, "chat_id": user_id, "message_id": message_id,
            "user_id": user_id, "media_type": media_type, "tmdb_id": tmdb_id,
            "label": "Fight Club", "is4k": 0, "status": "waiting",
            "arr_id": None, "last_progress": None, "bumped": 0,
            "timeout_at": "2099-01-01 00:00:00"}


async def test_cancel_closes_requesters_progress_card():
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID))
    item = _items()[0]
    item.requested_by_id = 7
    seerr = ctx.bot_data["seerr"]
    seerr.get_request.side_effect = None
    seerr.get_request.return_value = item
    store = ctx.bot_data["store"]
    store.find_by_seerr_id.return_value = make_link(telegram_id=USER_ID)
    store.find_progress_cards.return_value = [
        _card(1, USER_ID, message_id=701), _card(2, 77, message_id=702)]
    upd = make_update(callback_data="rqlc:12", user_id=USER_ID)
    await rq_list_cancel(upd, ctx)
    store.find_progress_cards.assert_awaited_once_with("movie", 550)
    # Only the requester's card is closed; the other user's is untouched.
    store.delete_progress_card.assert_awaited_once_with(1)
    edit = ctx.bot.edit_message_text.await_args.kwargs
    assert edit["message_id"] == 701 and edit["chat_id"] == USER_ID
    assert "Request cancelled." in edit["text"]
    assert "/requests" not in edit["text"]


async def test_admin_cancel_closes_the_requesters_card_not_the_admins():
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=ADMIN_ID))
    item = _items()[0]
    item.requested_by_id = 7
    seerr = ctx.bot_data["seerr"]
    seerr.get_request.side_effect = None
    seerr.get_request.return_value = item
    store = ctx.bot_data["store"]
    store.find_by_seerr_id.return_value = make_link(telegram_id=USER_ID)
    store.find_progress_cards.return_value = [
        _card(1, USER_ID, message_id=701), _card(2, ADMIN_ID, message_id=702)]
    upd = make_update(callback_data="rqlc:12", user_id=ADMIN_ID)
    await rq_list_cancel(upd, ctx)
    store.delete_progress_card.assert_awaited_once_with(1)
    assert ctx.bot.edit_message_text.await_args.kwargs["message_id"] == 701


async def test_failed_cancel_leaves_progress_card_alone():
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID))
    item = _items()[0]
    item.requested_by_id = 7
    seerr = ctx.bot_data["seerr"]
    seerr.get_request.side_effect = None
    seerr.get_request.return_value = item
    seerr.delete_request.side_effect = RuntimeError("boom")
    store = ctx.bot_data["store"]
    store.find_by_seerr_id.return_value = make_link(telegram_id=USER_ID)
    store.find_progress_cards.return_value = [_card(1, USER_ID)]
    upd = make_update(callback_data="rqlc:12", user_id=USER_ID)
    await rq_list_cancel(upd, ctx)
    store.delete_progress_card.assert_not_awaited()
    ctx.bot.edit_message_text.assert_not_awaited()


async def test_card_failure_does_not_fail_the_cancel():
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=USER_ID))
    item = _items()[0]
    seerr = ctx.bot_data["seerr"]
    seerr.get_request.side_effect = None
    seerr.get_request.return_value = item
    ctx.bot_data["store"].find_progress_cards.side_effect = RuntimeError("db")
    upd = make_update(callback_data="rqlc:12", user_id=USER_ID)
    await rq_list_cancel(upd, ctx)
    assert "#12 cancelled" in upd.callback_query.edits[0]["text"]


def _setup(user=USER_ID, requested_by_id=7, cards=(), owner=None):
    ctx = make_ctx(admin_id=ADMIN_ID, link=make_link(telegram_id=user))
    item = _items()[0]
    item.requested_by_id = requested_by_id
    seerr = ctx.bot_data["seerr"]
    seerr.get_request.side_effect = None
    seerr.get_request.return_value = item
    store = ctx.bot_data["store"]
    store.find_by_seerr_id.return_value = owner
    store.find_progress_cards.return_value = list(cards)
    return ctx, store


async def test_ambiguous_waiting_cards_are_left_alone():
    """Standard and 4K (or two season picks) both waiting: no Request id on
    the rows, so none is closed."""
    ctx, store = _setup(cards=[_card(1, USER_ID, message_id=701),
                               _card(2, USER_ID, message_id=702)],
                        owner=make_link(telegram_id=USER_ID))
    await rq_list_cancel(make_update(callback_data="rqlc:12", user_id=USER_ID), ctx)
    store.delete_progress_card.assert_not_awaited()


async def test_grabbing_card_of_another_request_is_kept():
    live = _card(1, USER_ID, message_id=701)
    live["status"] = "grabbing"
    ctx, store = _setup(cards=[live, _card(2, USER_ID, message_id=702)],
                        owner=make_link(telegram_id=USER_ID))
    await rq_list_cancel(make_update(callback_data="rqlc:12", user_id=USER_ID), ctx)
    store.delete_progress_card.assert_awaited_once_with(2)


async def test_unlinked_requester_closes_nobody_elses_card():
    ctx, store = _setup(requested_by_id=20, owner=None,
                        cards=[_card(5, USER_ID)])
    # Tapper's Seerr id differs from the requester's.
    await rq_list_cancel(make_update(callback_data="rqlc:12", user_id=USER_ID), ctx)
    store.delete_progress_card.assert_not_awaited()


async def test_tapper_sharing_the_seerr_account_keeps_own_card():
    ctx, store = _setup(requested_by_id=ctx_seerr_id(),
                        owner=make_link(telegram_id=43),
                        cards=[_card(1, USER_ID, message_id=701)])
    await rq_list_cancel(make_update(callback_data="rqlc:12", user_id=USER_ID), ctx)
    store.delete_progress_card.assert_awaited_once_with(1)


def ctx_seerr_id():
    return make_link(telegram_id=USER_ID).seerr_id


async def test_pre_read_happens_before_delete_and_row_goes_before_edit():
    ctx, store = _setup(cards=[_card(1, USER_ID)],
                        owner=make_link(telegram_id=USER_ID))
    order = []
    ctx.bot_data["seerr"].get_request.side_effect = (
        lambda *a, **k: order.append("get") or _items()[0])
    ctx.bot_data["seerr"].delete_request.side_effect = (
        lambda *a, **k: order.append("delete"))
    store.delete_progress_card.side_effect = lambda *a: order.append("row")
    ctx.bot.edit_message_text.side_effect = (
        lambda **k: order.append("edit"))
    await rq_list_cancel(make_update(callback_data="rqlc:12", user_id=USER_ID), ctx)
    assert order == ["get", "delete", "row", "edit"]


async def test_delete_404_after_landed_delete_still_closes_card():
    from http_util import NotFoundAPIError
    ctx, store = _setup(cards=[_card(1, USER_ID)],
                        owner=make_link(telegram_id=USER_ID))
    ctx.bot_data["seerr"].delete_request.side_effect = NotFoundAPIError(
        "gone", status_code=404, service="Seerr")
    await rq_list_cancel(make_update(callback_data="rqlc:12", user_id=USER_ID), ctx)
    store.delete_progress_card.assert_awaited_once_with(1)
