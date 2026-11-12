"""Admin Request DM buttons (#26): which DMs get Approve / Decline / Retry /
Open in Seerr, and what a tap does (status check first, edit in place,
error line on a Seerr failure, Admin only)."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from http_util import NotFoundAPIError, TransientAPIError
from seerr import (
    REQUEST_STATUS_APPROVED,
    REQUEST_STATUS_FAILED,
    REQUEST_STATUS_PENDING,
    RequestListItem,
)

from bot.request_admin import admin_request_kb, rq_admin_action, seerr_link
from bot.webhook_handlers import handle_seerr_media

from tests._handler_harness import make_ctx, make_link, make_update

USER_ID = 42
ADMIN_ID = 999
PUBLIC_URL = "https://seerr.example.com"
DM_TEXT = "📥 New request, approve it in Seerr\nSome Movie (2026)\n<b>Requested by:</b> user1plex"


# --- DM buttons from the webhook ---------------------------------------------

def _app(*, public_url=PUBLIC_URL):
    ctx = make_ctx(admin_id=ADMIN_ID)
    app = ctx.application
    app.bot_data["store"].find_by_plex_username = AsyncMock(
        return_value=make_link(telegram_id=USER_ID))
    app.bot_data["settings_store"].settings.seerr_public_url = public_url
    return app


def _payload(nt, *, request_id="12"):
    request = {"requestedBy_username": "user1plex"}
    if request_id is not None:
        request["request_id"] = request_id
    return {
        "notification_type": nt,
        "subject": "Some Movie (2026)",
        "media": {"media_type": "movie", "tmdbId": 550},
        "request": request,
    }


def _admin_markup(app):
    calls = [c for c in app.bot.send_message.call_args_list
             if c.kwargs["chat_id"] == ADMIN_ID]
    assert len(calls) == 1
    return calls[0].kwargs.get("reply_markup"), calls[0].kwargs["text"]


def _buttons(markup):
    if markup is None:
        return []
    return [(b.text, b.callback_data, b.url)
            for row in markup.inline_keyboard for b in row]


async def test_pending_dm_has_approve_decline_and_open_in_seerr():
    app = _app()
    await handle_seerr_media(app, _payload("MEDIA_PENDING"))
    markup, _ = _admin_markup(app)
    assert _buttons(markup) == [
        ("✅ Approve", "rqap:12", None),
        ("🚫 Decline", "rqdc:12", None),
        ("Open in Seerr", None, f"{PUBLIC_URL}/movie/550"),
    ]
    # Recorded for the button gate, or the gate could refuse the taps.
    assert len(app.bot_data["btn_msgs"][ADMIN_ID]) == 1


async def test_failed_dm_has_retry_and_open_in_seerr():
    app = _app()
    await handle_seerr_media(app, _payload("MEDIA_FAILED"))
    markup, _ = _admin_markup(app)
    assert _buttons(markup) == [
        ("🔁 Retry", "rqrt:12", None),
        ("Open in Seerr", None, f"{PUBLIC_URL}/movie/550"),
    ]


async def test_auto_approved_dm_has_no_buttons():
    app = _app()
    await handle_seerr_media(app, _payload("MEDIA_AUTO_APPROVED"))
    markup, _ = _admin_markup(app)
    assert markup is None


async def test_no_request_id_means_no_action_buttons_and_same_text():
    app = _app()
    await handle_seerr_media(app, _payload("MEDIA_PENDING"))
    _, text_with_id = _admin_markup(app)
    app2 = _app()
    await handle_seerr_media(app2, _payload("MEDIA_PENDING", request_id=None))
    markup, text = _admin_markup(app2)
    assert _buttons(markup) == [("Open in Seerr", None, f"{PUBLIC_URL}/movie/550")]
    assert text == text_with_id


async def test_no_public_url_means_no_open_in_seerr():
    app = _app(public_url="")
    await handle_seerr_media(app, _payload("MEDIA_PENDING"))
    markup, _ = _admin_markup(app)
    assert [b[0] for b in _buttons(markup)] == ["✅ Approve", "🚫 Decline"]


async def test_no_request_id_and_no_public_url_sends_plain_dm():
    app = _app(public_url="")
    await handle_seerr_media(app, _payload("MEDIA_FAILED", request_id=None))
    markup, _ = _admin_markup(app)
    assert markup is None


def test_seerr_link_falls_back_to_requests_list_without_tmdb_id():
    assert seerr_link(PUBLIC_URL + "/", {}) == f"{PUBLIC_URL}/requests"
    assert seerr_link("", {"media_type": "tv", "tmdbId": 1}) is None


def test_requester_dm_events_get_no_buttons():
    for nt in ("MEDIA_APPROVED", "MEDIA_DECLINED", "MEDIA_AVAILABLE",
               "MEDIA_AUTO_APPROVED"):
        assert admin_request_kb(nt, 12, PUBLIC_URL) is None


# --- Tapping the buttons -------------------------------------------------------

def _request(status):
    return RequestListItem(id=12, status=status, media_type="movie",
                           tmdb_id=550, seasons=[], created_at="",
                           requested_by="user1plex", requested_by_id=7)


def _tap(data, *, status=REQUEST_STATUS_PENDING, user_id=ADMIN_ID):
    upd = make_update(callback_data=data, user_id=user_id)
    ctx = make_ctx(admin_id=ADMIN_ID)
    seerr = ctx.bot_data["seerr"]
    seerr.get_request = AsyncMock(return_value=_request(status))
    seerr.approve_request = AsyncMock()
    seerr.decline_request = AsyncMock()
    seerr.retry_request = AsyncMock()
    nt = "MEDIA_FAILED" if data.startswith("rqrt") else "MEDIA_PENDING"
    upd.callback_query.message.text_html = DM_TEXT
    upd.callback_query.message.reply_markup = admin_request_kb(
        nt, 12, f"{PUBLIC_URL}/movie/550")
    return upd, ctx, seerr


def _last_edit(upd):
    return upd.callback_query.edits[-1]


@pytest.mark.parametrize("data,method,outcome", [
    ("rqap:12", "approve_request", "✅ Approved"),
    ("rqdc:12", "decline_request", "🚫 Declined"),
])
async def test_pending_action_calls_seerr_once_and_leaves_only_link(
        data, method, outcome):
    upd, ctx, seerr = _tap(data)
    await rq_admin_action(upd, ctx)
    getattr(seerr, method).assert_awaited_once_with(12)
    edit = _last_edit(upd)
    assert edit["text"] == f"{DM_TEXT}\n\n{outcome}"
    assert _buttons(edit["reply_markup"]) == [
        ("Open in Seerr", None, f"{PUBLIC_URL}/movie/550")]


async def test_decline_is_one_tap_with_no_confirm():
    upd, ctx, seerr = _tap("rqdc:12")
    await rq_admin_action(upd, ctx)
    seerr.decline_request.assert_awaited_once_with(12)
    assert len(upd.callback_query.edits) == 1


async def test_retry_calls_seerr_once_and_reads_retrying():
    upd, ctx, seerr = _tap("rqrt:12", status=REQUEST_STATUS_FAILED)
    await rq_admin_action(upd, ctx)
    seerr.retry_request.assert_awaited_once_with(12)
    edit = _last_edit(upd)
    assert edit["text"].endswith("\n\n🔁 Retrying")
    assert [b[0] for b in _buttons(edit["reply_markup"])] == ["Open in Seerr"]


async def test_already_approved_changes_nothing_and_says_so():
    upd, ctx, seerr = _tap("rqap:12", status=REQUEST_STATUS_APPROVED)
    await rq_admin_action(upd, ctx)
    seerr.approve_request.assert_not_awaited()
    seerr.decline_request.assert_not_awaited()
    edit = _last_edit(upd)
    assert edit["text"].endswith("Already approved in Seerr")
    assert [b[0] for b in _buttons(edit["reply_markup"])] == ["Open in Seerr"]


async def test_retry_on_request_no_longer_failed_changes_nothing():
    upd, ctx, seerr = _tap("rqrt:12", status=REQUEST_STATUS_APPROVED)
    await rq_admin_action(upd, ctx)
    seerr.retry_request.assert_not_awaited()
    assert _last_edit(upd)["text"].endswith("Already approved in Seerr")


async def test_request_gone_from_seerr_removes_action_buttons():
    upd, ctx, seerr = _tap("rqap:12")
    seerr.get_request.side_effect = NotFoundAPIError("gone", status_code=404,
                                                     service="Seerr")
    await rq_admin_action(upd, ctx)
    seerr.approve_request.assert_not_awaited()
    edit = _last_edit(upd)
    assert "no longer exists in Seerr" in edit["text"]
    assert [b[0] for b in _buttons(edit["reply_markup"])] == ["Open in Seerr"]


async def test_seerr_error_keeps_buttons_and_adds_error_line():
    upd, ctx, seerr = _tap("rqap:12")
    seerr.approve_request.side_effect = TransientAPIError("down", service="Seerr")
    await rq_admin_action(upd, ctx)
    edit = _last_edit(upd)
    base, note = edit["text"].split("\n\n")
    assert base == DM_TEXT
    assert note.startswith("⚠️ Couldn't approve.")
    assert edit["reply_markup"] is upd.callback_query.message.reply_markup


async def test_error_line_is_replaced_not_stacked_on_a_second_failure():
    upd, ctx, seerr = _tap("rqap:12")
    upd.callback_query.message.text_html = f"{DM_TEXT}\n\n⚠️ Couldn't approve. Old."
    seerr.approve_request.side_effect = TransientAPIError("down", service="Seerr")
    await rq_admin_action(upd, ctx)
    assert _last_edit(upd)["text"].count("⚠️ Couldn't approve.") == 1


async def test_status_read_error_keeps_buttons_and_changes_nothing():
    upd, ctx, seerr = _tap("rqap:12")
    seerr.get_request.side_effect = TransientAPIError("down", service="Seerr")
    await rq_admin_action(upd, ctx)
    seerr.approve_request.assert_not_awaited()
    edit = _last_edit(upd)
    assert "⚠️ Couldn't approve." in edit["text"]
    assert edit["reply_markup"] is upd.callback_query.message.reply_markup


async def test_non_admin_tap_is_refused_with_toast():
    upd, ctx, seerr = _tap("rqap:12", user_id=USER_ID)
    await rq_admin_action(upd, ctx)
    seerr.get_request.assert_not_awaited()
    seerr.approve_request.assert_not_awaited()
    assert upd.callback_query.answers == [("Admin only.", False)]
    assert upd.callback_query.edits == []


async def test_dm_text_with_blank_line_is_kept_whole():
    upd, ctx, _ = _tap("rqap:12")
    odd = "📥 New request\nTitle\n\nSeasons: 1\n<b>Requested by:</b> u"
    upd.callback_query.message.text_html = odd
    await rq_admin_action(upd, ctx)
    assert _last_edit(upd)["text"] == f"{odd}\n\n✅ Approved"


async def test_tap_while_same_request_in_flight_is_ignored():
    upd, ctx, seerr = _tap("rqap:12")
    ctx.bot_data["inflight_actions"] = {"rqadmin:12"}
    await rq_admin_action(upd, ctx)
    seerr.get_request.assert_not_awaited()
    seerr.approve_request.assert_not_awaited()
    assert upd.callback_query.edits == []


async def test_in_flight_slot_released_after_tap():
    upd, ctx, _ = _tap("rqap:12")
    await rq_admin_action(upd, ctx)
    assert "rqadmin:12" not in ctx.bot_data.get("inflight_actions", set())


# --- The Admin's own failed Request (#43) ------------------------------------

def _admin_requested_app():
    app = _app()
    app.bot_data["store"].find_by_plex_username = AsyncMock(
        return_value=make_link(telegram_id=ADMIN_ID))
    return app


async def test_admin_own_failed_request_dm_has_retry_and_open_in_seerr():
    app = _admin_requested_app()
    await handle_seerr_media(app, _payload("MEDIA_FAILED"))
    markup, text = _admin_markup(app)
    assert "failed to download" in text
    assert _buttons(markup) == [
        ("🔁 Retry", "rqrt:12", None),
        ("Open in Seerr", None, f"{PUBLIC_URL}/movie/550"),
    ]
    assert len(app.bot_data["btn_msgs"][ADMIN_ID]) == 1


async def test_admin_own_failed_request_without_request_id_has_link_only():
    app = _admin_requested_app()
    await handle_seerr_media(app, _payload("MEDIA_FAILED", request_id=None))
    markup, _ = _admin_markup(app)
    assert _buttons(markup) == [
        ("Open in Seerr", None, f"{PUBLIC_URL}/movie/550")]


async def test_admin_own_approved_request_dm_stays_bare():
    app = _admin_requested_app()
    await handle_seerr_media(app, _payload("MEDIA_APPROVED"))
    markup, _ = _admin_markup(app)
    assert markup is None


async def test_user_failed_request_dm_to_requester_stays_bare():
    app = _app()
    await handle_seerr_media(app, _payload("MEDIA_FAILED"))
    user_calls = [c for c in app.bot.send_message.call_args_list
                  if c.kwargs["chat_id"] == USER_ID]
    assert len(user_calls) == 1
    assert user_calls[0].kwargs.get("reply_markup") is None


async def test_retry_tap_on_the_requester_line_dm_reads_retrying():
    upd, ctx, seerr = _tap("rqrt:12", status=REQUEST_STATUS_FAILED)
    upd.callback_query.message.text_html = (
        "⚠️ Your request failed to download.\nSome Movie (2026)")
    await rq_admin_action(upd, ctx)
    seerr.retry_request.assert_awaited_once_with(12)
    edit = _last_edit(upd)
    assert edit["text"] == ("⚠️ Your request failed to download.\n"
                            "Some Movie (2026)\n\n🔁 Retrying")
    assert [b[0] for b in _buttons(edit["reply_markup"])] == ["Open in Seerr"]


async def test_admin_own_failed_request_keeps_retry_with_admin_alarm_off():
    app = _admin_requested_app()
    app.bot_data["settings_store"].settings.tg_notify_admin_failed = False
    await handle_seerr_media(app, _payload("MEDIA_FAILED"))
    markup, text = _admin_markup(app)
    assert "admin has been notified" not in text
    assert ("🔁 Retry", "rqrt:12", None) in _buttons(markup)
