"""Tests for automatic retry of failed Requests: the MEDIA_FAILED webhook
queues a row, the 10-minute job retries due rows on the schedule, and a row
ends quietly when the Request is settled."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from bot.auto_retry import (delay_before_try, enqueue_failed_request,
                            run_auto_retry, wait_words)
from bot.callback_prefixes import RQ_ADMIN_RETRY
from bot.webhook_handlers import handle_seerr_media
from http_util import NotFoundAPIError, PermanentAPIError
from seerr import (REQUEST_STATUS_APPROVED, REQUEST_STATUS_FAILED,
                   RequestListItem)
from settings import Settings
from store import UserStore

from tests._handler_harness import make_ctx

ADMIN_ID = 999
USER_ID = 42
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
HOUR, DAY = 3600, 24 * 3600


def _app(store, *, on=True, tries=3):
    ctx = make_ctx(admin_id=ADMIN_ID, bot_data_overrides={"store": store})
    cfg = ctx.bot_data["settings_store"].settings
    cfg.auto_retry_failed, cfg.auto_retry_tries = on, tries
    return ctx.application


def _item(status):
    return RequestListItem(id=12, status=status, media_type="movie", tmdb_id=550)


def _seerr(app, status=REQUEST_STATUS_FAILED, retry_error=None):
    seerr = app.bot_data["seerr"]
    seerr.get_request = AsyncMock(return_value=_item(status))
    seerr.retry_request = AsyncMock(side_effect=retry_error)
    return seerr


async def _queue(app, request_id=12, chat_id=USER_ID, now=NOW):
    return await enqueue_failed_request(app, request_id, 550, "movie",
                                        chat_id, 7, now=now)


def _failed_payload():
    return {
        "notification_type": "MEDIA_FAILED",
        "subject": "Some Movie (2026)",
        "media": {"media_type": "movie", "tmdbId": 550},
        "request": {"request_id": "12", "requestedBy_username": "user1plex"},
    }


async def _webhook_app(store, **kw):
    await store.link_with_plex(
        telegram_id=USER_ID, seerr_id=7, seerr_display="U", plex_token="t",
        plex_uuid="u", plex_username="user1plex")
    return _app(store, **kw)


def _texts(app):
    return {c.kwargs["chat_id"]: c.kwargs["text"]
            for c in app.bot.send_message.call_args_list}


# --- the webhook queues a row ------------------------------------------------

async def test_failed_event_adds_one_row_with_the_switch_on(fresh_store):
    app = await _webhook_app(fresh_store)
    await handle_seerr_media(app, _failed_payload())
    await handle_seerr_media(app, _failed_payload())  # a repeat refreshes
    rows = await fresh_store.list_due_failed_retries("2999-01-01 00:00:00")
    assert [(r.request_id, r.tmdb_id, r.media_type, r.requester_chat_id,
             r.tries) for r in rows] == [(12, 550, "movie", USER_ID, 0)]


async def test_failed_event_adds_nothing_with_the_switch_off(fresh_store):
    app = await _webhook_app(fresh_store, on=False)
    await handle_seerr_media(app, _failed_payload())
    assert await fresh_store.list_due_failed_retries("2999-01-01 00:00:00") == []
    assert "try again" not in _texts(app)[USER_ID]
    assert "Auto-retry" not in _texts(app)[ADMIN_ID]


async def test_a_refresh_keeps_tries_and_the_schedule(fresh_store):
    app = _app(fresh_store)
    await _queue(app)
    await fresh_store.schedule_failed_retry(12, 2, "2026-11-15 12:00:00")
    await enqueue_failed_request(app, 12, 550, "movie", USER_ID, 7, now=NOW)
    (row,) = await fresh_store.list_due_failed_retries("2999-01-01 00:00:00")
    assert (row.tries, row.next_try_at) == (2, "2026-11-15 12:00:00")


async def test_dms_say_when_the_retry_comes(fresh_store):
    app = await _webhook_app(fresh_store)
    await handle_seerr_media(app, _failed_payload())
    texts = _texts(app)
    assert "Usher will try again in about 1 hour." in texts[USER_ID]
    assert "Auto-retry is on" in texts[ADMIN_ID]


async def test_admin_alarm_keeps_its_retry_button(fresh_store):
    app = await _webhook_app(fresh_store)
    await handle_seerr_media(app, _failed_payload())
    call = [c for c in app.bot.send_message.call_args_list
            if c.kwargs["chat_id"] == ADMIN_ID][0]
    buttons = [b.callback_data for row in call.kwargs["reply_markup"].inline_keyboard
               for b in row]
    assert f"{RQ_ADMIN_RETRY}:12" in buttons


async def test_rows_survive_a_restart(tmp_db_path, fresh_token_crypto):
    first = UserStore(tmp_db_path, crypto=fresh_token_crypto)
    await _queue(_app(first))
    again = UserStore(tmp_db_path, crypto=fresh_token_crypto)
    rows = await again.list_due_failed_retries("2999-01-01 00:00:00")
    assert [r.request_id for r in rows] == [12]


# --- the job ------------------------------------------------------------------

async def test_a_row_not_yet_due_is_left_alone(fresh_store):
    app = _app(fresh_store)
    seerr = _seerr(app)
    await _queue(app)
    await run_auto_retry(app, NOW + timedelta(minutes=59))
    seerr.get_request.assert_not_called()


async def test_a_due_failed_row_retries_and_follows_the_schedule(fresh_store):
    app = _app(fresh_store, tries=5)
    seerr = _seerr(app)
    await _queue(app)
    when = NOW
    waits = []
    for expected_tries in (1, 2, 3, 4):
        when = when + timedelta(seconds=delay_before_try(expected_tries - 1))
        await run_auto_retry(app, when)
        (row,) = await fresh_store.list_due_failed_retries("2999-01-01 00:00:00")
        assert row.tries == expected_tries
        waits.append((datetime.strptime(row.next_try_at, "%Y-%m-%d %H:%M:%S")
                      .replace(tzinfo=timezone.utc) - when).total_seconds())
    assert seerr.retry_request.await_count == 4
    assert waits == [6 * HOUR, DAY, DAY, DAY]


async def test_a_request_no_longer_failed_ends_the_row_quietly(fresh_store):
    app = _app(fresh_store)
    seerr = _seerr(app, status=REQUEST_STATUS_APPROVED)
    await _queue(app)
    await run_auto_retry(app, NOW + timedelta(hours=2))
    seerr.retry_request.assert_not_called()
    assert await fresh_store.list_due_failed_retries("2999-01-01 00:00:00") == []
    app.bot.send_message.assert_not_called()


async def test_a_deleted_request_ends_the_row_quietly(fresh_store):
    app = _app(fresh_store)
    seerr = _seerr(app)
    seerr.get_request.side_effect = NotFoundAPIError("gone", status_code=404)
    await _queue(app)
    await run_auto_retry(app, NOW + timedelta(hours=2))
    assert await fresh_store.list_due_failed_retries("2999-01-01 00:00:00") == []
    app.bot.send_message.assert_not_called()


async def test_a_409_ends_the_row_quietly(fresh_store):
    app = _app(fresh_store)
    _seerr(app, retry_error=PermanentAPIError("conflict", status_code=409))
    await _queue(app)
    await run_auto_retry(app, NOW + timedelta(hours=2))
    assert await fresh_store.list_due_failed_retries("2999-01-01 00:00:00") == []
    app.bot.send_message.assert_not_called()


async def test_after_the_last_try_the_requester_is_told_once(fresh_store):
    app = _app(fresh_store, tries=2)
    seerr = _seerr(app)
    await _queue(app)
    when = NOW
    for tries_made in range(3):  # two retries, then the final check
        when = when + timedelta(seconds=delay_before_try(tries_made))
        await run_auto_retry(app, when)
    assert seerr.retry_request.await_count == 2
    (call,) = app.bot.send_message.call_args_list
    assert call.kwargs["chat_id"] == USER_ID
    assert "needs the Admin" in call.kwargs["text"]
    assert await fresh_store.list_due_failed_retries("2999-01-01 00:00:00") == []
    await run_auto_retry(app, when + timedelta(days=3))  # nothing left to say
    assert app.bot.send_message.await_count == 1


async def test_a_row_with_nobody_to_tell_ends_without_a_dm(fresh_store):
    app = _app(fresh_store, tries=1)
    _seerr(app)
    await _queue(app, chat_id=None)
    await run_auto_retry(app, NOW + timedelta(hours=1))
    await run_auto_retry(app, NOW + timedelta(hours=1) + timedelta(days=1))
    app.bot.send_message.assert_not_called()
    assert await fresh_store.list_due_failed_retries("2999-01-01 00:00:00") == []


async def test_the_job_does_nothing_with_the_switch_off(fresh_store):
    app = _app(fresh_store)
    seerr = _seerr(app)
    await _queue(app)
    app.bot_data["settings_store"].settings.auto_retry_failed = False
    await run_auto_retry(app, NOW + timedelta(hours=2))
    seerr.get_request.assert_not_called()


async def test_one_broken_row_does_not_stop_the_others(fresh_store):
    app = _app(fresh_store)
    seerr = _seerr(app)

    async def get(request_id):
        if request_id == 12:
            raise RuntimeError("boom")
        return _item(REQUEST_STATUS_FAILED)
    seerr.get_request = AsyncMock(side_effect=get)
    await _queue(app, request_id=12)
    await _queue(app, request_id=13)
    await run_auto_retry(app, NOW + timedelta(hours=2))
    seerr.retry_request.assert_awaited_once_with(13)


# --- settings and wording -------------------------------------------------------

def test_settings_defaults_and_limits():
    s = Settings.from_dict({})
    assert (s.auto_retry_failed, s.auto_retry_tries) == (True, 3)
    s = Settings.from_dict({"auto_retry_failed": False, "auto_retry_tries": 5})
    assert (s.auto_retry_failed, s.auto_retry_tries) == (False, 5)
    assert Settings.from_dict({"auto_retry_tries": 6}).auto_retry_tries == 3
    assert Settings.from_dict({"auto_retry_tries": 0}).auto_retry_tries == 3


def test_wait_words():
    assert wait_words(HOUR) == "in about 1 hour"
    assert wait_words(6 * HOUR) == "in about 6 hours"
    assert wait_words(20 * 60) == "in about 20 minutes"


def test_panel_shows_both_settings():
    from settings import Settings as S
    import webui
    page = webui._settings_page(S(auto_retry_failed=True, auto_retry_tries=4),
                                webhook_url="http://x/webhook/seerr",
                                csrf_token="t")
    assert 'name="auto_retry_failed" checked' in page
    assert 'name="auto_retry_tries" value="4"' in page


async def test_a_refused_retry_still_counts_as_a_try(fresh_store):
    app = _app(fresh_store)
    _seerr(app, retry_error=PermanentAPIError("boom", status_code=500))
    await _queue(app)
    await run_auto_retry(app, NOW + timedelta(hours=2))
    (row,) = await fresh_store.list_due_failed_retries("2999-01-01 00:00:00")
    assert row.tries == 1
