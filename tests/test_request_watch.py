"""Tests for the morphing request cards: queue-progress parsing, the watch
poller's edit/dedupe/bump behavior, webhook-driven state changes, and the
watch row created at submit time."""
from __future__ import annotations

from unittest.mock import AsyncMock

from fix_result import QueueProgress, parse_queue_records
from bot.request_watch import (
    apply_webhook_event,
    maybe_bump_priority,
    poll_request_watches,
)

from tests._handler_harness import make_ctx, make_mapping, make_update

USER_ID = 42


def test_parse_queue_records_aggregates():
    p = parse_queue_records([
        {"size": 1000, "sizeleft": 500, "timeleft": "00:10:00",
         "downloadId": "SABnzbd_nzo_a"},
        {"size": 3000, "sizeleft": 0, "timeleft": "00:00:00",
         "downloadId": "SABnzbd_nzo_b"},
    ])
    assert p.percent == 88  # (4000-500)/4000
    assert p.timeleft == "00:00:00"  # largest record's
    assert p.download_ids == ["SABnzbd_nzo_a", "SABnzbd_nzo_b"]
    assert p.count == 2


def test_parse_queue_records_empty_and_garbage():
    assert parse_queue_records([]) is None
    assert parse_queue_records(None) is None
    p = parse_queue_records(["junk", {"size": "x", "sizeleft": "y"}])
    assert p.count == 1 and p.percent == 0


def _watch(status="grabbing", **over):
    w = {"id": 1, "chat_id": 100, "message_id": 55, "user_id": USER_ID,
         "media_type": "movie", "tmdb_id": 550, "label": "Movie (2026)",
         "is4k": 0, "status": status, "arr_id": 7, "last_progress": "",
         "bumped": 0, "timeout_at": "2099-01-01 00:00:00"}
    w.update(over)
    return w


async def test_poll_edits_card_with_progress_and_dedupes():
    ctx = make_ctx()
    ctx.bot_data["store"].list_request_watches.return_value = [_watch()]
    ctx.bot_data["radarr"].get_queue_progress.return_value = QueueProgress(
        percent=42, timeleft="00:14:00", download_ids=["nzo_a"], count=1)
    await poll_request_watches(ctx)
    kwargs = ctx.bot.edit_message_text.call_args.kwargs
    assert kwargs["chat_id"] == 100 and kwargs["message_id"] == 55
    assert "42%" in kwargs["text"] and "00:14:00" in kwargs["text"]
    ctx.bot_data["store"].update_request_watch.assert_awaited()

    # Same progress next tick: no second edit.
    ctx.bot.edit_message_text.reset_mock()
    ctx.bot_data["store"].list_request_watches.return_value = [
        _watch(last_progress=kwargs["text"].split("\n")[1])]
    await poll_request_watches(ctx)
    ctx.bot.edit_message_text.assert_not_awaited()


async def test_poll_waiting_watch_is_left_alone():
    ctx = make_ctx()
    ctx.bot_data["store"].list_request_watches.return_value = [
        _watch(status="waiting")]
    await poll_request_watches(ctx)
    ctx.bot.edit_message_text.assert_not_awaited()
    ctx.bot_data["radarr"].get_queue_progress.assert_not_awaited()


async def test_poll_timeout_finalizes_and_deletes():
    ctx = make_ctx()
    ctx.bot_data["store"].list_request_watches.return_value = [
        _watch(timeout_at="2020-01-01 00:00:00")]
    await poll_request_watches(ctx)
    assert "Still processing" in ctx.bot.edit_message_text.call_args.kwargs["text"]
    ctx.bot_data["store"].delete_request_watch.assert_awaited_with(1)


async def test_poll_bumps_priority_once_when_enabled():
    ctx = make_ctx()
    ctx.bot_data["settings_store"].settings.sabnzbd_boost = "high"
    sab = AsyncMock()
    ctx.bot_data["sabnzbd"] = sab
    ctx.bot_data["store"].list_request_watches.return_value = [_watch()]
    ctx.bot_data["radarr"].get_queue_progress.return_value = QueueProgress(
        percent=10, timeleft="", download_ids=["nzo_a"], count=1)
    await poll_request_watches(ctx)
    sab.set_priority.assert_awaited_with("nzo_a", "high")
    assert {"bumped": 1} in [c.kwargs for c in
                             ctx.bot_data["store"].update_request_watch.call_args_list]

    # Already-bumped rows never re-bump.
    sab.set_priority.reset_mock()
    ctx.bot_data["store"].list_request_watches.return_value = [_watch(bumped=1)]
    await poll_request_watches(ctx)
    sab.set_priority.assert_not_awaited()


async def test_maybe_bump_respects_off():
    ctx = make_ctx()
    ctx.bot_data["sabnzbd"] = AsyncMock()
    ctx.bot_data["settings_store"].settings.sabnzbd_boost = "off"
    assert await maybe_bump_priority(ctx, ["nzo"]) is False
    ctx.bot_data["sabnzbd"].set_priority.assert_not_awaited()


async def test_webhook_approved_flips_waiting_to_grabbing():
    ctx = make_ctx()
    app = ctx.application
    app.bot_data["store"].find_request_watches = AsyncMock(
        return_value=[_watch(status="waiting")])
    await apply_webhook_event(app, "MEDIA_APPROVED", "movie", 550)
    app.bot_data["store"].update_request_watch.assert_awaited_with(
        1, status="grabbing")
    assert "Approved" in app.bot.edit_message_text.call_args.kwargs["text"]


async def test_webhook_available_finalizes_and_deletes():
    ctx = make_ctx()
    app = ctx.application
    app.bot_data["store"].find_request_watches = AsyncMock(
        return_value=[_watch()])
    await apply_webhook_event(app, "MEDIA_AVAILABLE", "movie", 550)
    text = app.bot.edit_message_text.call_args.kwargs["text"]
    assert "Available in Plex" in text
    assert "/requests" not in text  # terminal card drops the footer
    app.bot_data["store"].delete_request_watch.assert_awaited_with(1)


async def test_submit_enqueues_watch():
    from seerr import CreatedRequest, REQUEST_STATUS_APPROVED
    from bot.request_flow import request_submit
    ctx = make_ctx(admin_id=999, mapping=make_mapping(telegram_id=USER_ID),
                   user_data={"rq_media": {"type": "movie", "tmdb_id": 550,
                                           "title": "Movie", "year": "2026"},
                              "rq_search_version": 1})
    ctx.args = []
    ctx.bot_data["seerr"].create_request.return_value = CreatedRequest(
        id=5, status=REQUEST_STATUS_APPROVED)
    upd = make_update(callback_data="rqgo:1", user_id=USER_ID)
    await request_submit(upd, ctx)
    kwargs = ctx.bot_data["store"].add_request_watch.call_args.kwargs
    assert kwargs["media_type"] == "movie" and kwargs["tmdb_id"] == 550
    assert kwargs["status"] == "grabbing"
    assert kwargs["is4k"] is False


async def test_store_watch_round_trip(fresh_store):
    wid = await fresh_store.add_request_watch(
        chat_id=1, message_id=2, user_id=3, media_type="movie", tmdb_id=550,
        label="Movie", is4k=False, status="waiting", timeout_hours=24)
    rows = await fresh_store.list_request_watches()
    assert rows[0]["id"] == wid and rows[0]["status"] == "waiting"
    await fresh_store.update_request_watch(wid, status="grabbing", arr_id=9,
                                           last_progress="x", bumped=1)
    row = (await fresh_store.find_request_watches("movie", 550))[0]
    assert (row["status"], row["arr_id"], row["bumped"]) == ("grabbing", 9, 1)
    await fresh_store.delete_request_watch(wid)
    assert await fresh_store.list_request_watches() == []
