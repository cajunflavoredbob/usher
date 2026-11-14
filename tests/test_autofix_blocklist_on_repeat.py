"""The "Auto-fix blocklists on a repeat" switch: a Reporter's repeat makes the
poller run Mark Failed once, enqueue one marked Fix row, tell the Reporter and
the Admin; a marked row's own repeat and an Admin Fix's repeat run nothing."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from bot import autofix_poll
from bot.autofix_poll import poll_pending_autofixes
from fix_result import FixResult
from fix_result import ReleaseFingerprint as FP
from seerr import IssueListItem
from settings import Settings
from store import PendingAutofix, UserStore
from tests._handler_harness import make_ctx, make_message

ADMIN = 999


def _fix(user_id: int = 42, *, marked: int = 0) -> PendingAutofix:
    now = datetime.now(timezone.utc)
    return PendingAutofix(
        id=1, chat_id=100, user_id=user_id, media_type="movie",
        radarr_movie_id=555, sonarr_series_id=None, sonarr_episode_id=None,
        sonarr_season=None, expected_episode_ids=[], label="Test Movie",
        ticket_id=7, ticket_url="http://seerr.example/issues/7",
        started_at=now.strftime("%Y-%m-%d %H:%M:%S"),
        timeout_at=(now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
        message_id=300, before_fingerprint=FP(release_name="Movie.2020-GRP"),
        auto_blocklisted=marked)


@pytest.fixture(autouse=True)
def _clear_inflight():
    autofix_poll._inflight.clear()
    yield
    autofix_poll._inflight.clear()


async def _tick(*, on=True, same=True, user_id=42, marked=0,
                mark_failed=None, busy=False, resolved=False):
    fix = _fix(user_id, marked=marked)
    fix.is_complete = AsyncMock(return_value=(True, ""))  # type: ignore[method-assign]
    ctx = make_ctx(admin_id=ADMIN)
    ctx.bot_data["settings_store"].settings.autofix_blocklist_on_repeat = on
    ctx.bot_data["seerr"].get_issue.return_value = IssueListItem(
        id=7, issue_type=1, status=2 if resolved else 1,
        created_at="2026-05-29T12:00:00Z", tmdb_id=12345, media_type="movie",
        problem_season=None, problem_episode=None, created_by="someone")
    radarr = ctx.bot_data["radarr"]
    radarr.read_fingerprint = AsyncMock(return_value=FP(
        release_name="Movie.2020-GRP" if same else "Other.2020-X",
        group="GRP", quality="WEBDL-1080p"))
    radarr.get_queue_progress = AsyncMock(return_value=None)
    radarr.mark_failed.return_value = mark_failed or FixResult.success(
        "Blocklisted and searching.", ["blocklist", "delete", "search"],
        poll_info={"movie_id": 555})
    store = ctx.bot_data["store"]
    store.list_pending_autofixes = AsyncMock(return_value=[fix])
    store.mark_autofix_status = AsyncMock()
    store.log_autofix.return_value = 77
    ctx.bot.send_message = AsyncMock(return_value=make_message(message_id=900))
    ctx.bot.edit_message_text = AsyncMock()
    if busy:
        ctx.bot_data["inflight_actions"] = {"movie:12345"}
    await poll_pending_autofixes(ctx)
    return ctx


def _sent(ctx, chat_id):
    return [c.kwargs for c in ctx.bot.send_message.call_args_list
            if c.kwargs["chat_id"] == chat_id]


def _callbacks(kwargs) -> list[str]:
    markup = kwargs.get("reply_markup")
    return [b.callback_data for row in markup.inline_keyboard
            for b in row] if markup else []


# --- the setting ---------------------------------------------------------------


def test_the_setting_defaults_to_off_and_round_trips():
    assert Settings.from_dict({}).autofix_blocklist_on_repeat is False
    on = Settings.from_dict({"autofix_blocklist_on_repeat": True})
    assert Settings.from_dict(on.to_dict()).autofix_blocklist_on_repeat is True


# --- off -------------------------------------------------------------------------


async def test_off_a_repeat_runs_no_mark_failed_and_keeps_the_ask_button():
    ctx = await _tick(on=False)
    ctx.bot_data["radarr"].mark_failed.assert_not_awaited()
    ctx.bot_data["store"].add_pending_autofix.assert_not_awaited()
    assert _sent(ctx, ADMIN) == []
    dm = _sent(ctx, 100)[-1]
    assert any(c.startswith("askadm:") for c in _callbacks(dm))
    assert "different release now" not in dm["text"]


# --- on --------------------------------------------------------------------------


async def test_on_a_reporters_repeat_runs_mark_failed_once_and_enqueues_one_marked_row():
    ctx = await _tick()
    ctx.bot_data["radarr"].mark_failed.assert_awaited_once()
    store = ctx.bot_data["store"]
    store.add_pending_autofix.assert_awaited_once()
    kwargs = store.add_pending_autofix.call_args.kwargs
    assert kwargs["auto_blocklisted"] is True
    assert (kwargs["user_id"], kwargs["chat_id"], kwargs["ticket_id"]) == (42, 100, 7)
    assert kwargs["radarr_movie_id"] == 555
    assert kwargs["event_id"] == 77
    # The event is the Admin's, so the Reporter's limit is untouched.
    assert store.log_autofix.call_args.args[0] == ADMIN
    assert store.log_autofix.call_args.kwargs["kind"] == "mark failed"


async def test_on_the_reporter_is_told_without_the_ask_button_and_still_refunded():
    ctx = await _tick()
    dm = _sent(ctx, 100)[-1]
    assert "trying a different release now" in dm["text"]
    assert "That use was given back" in dm["text"]
    assert not any(c.startswith("askadm:") for c in _callbacks(dm))
    ctx.bot_data["store"].mark_autofix_repeat.assert_awaited_once_with(1, refunded=True)


async def test_on_the_admin_is_told_what_was_blocklisted():
    ctx = await _tick()
    (dm,) = _sent(ctx, ADMIN)
    assert "blocklisted" in dm["text"]
    assert "Movie.2020-GRP" in dm["text"]
    assert "Test Movie" in dm["text"]
    assert "issues/7" in dm["text"]


async def test_on_not_a_repeat_runs_nothing():
    ctx = await _tick(same=False)
    ctx.bot_data["radarr"].mark_failed.assert_not_awaited()
    assert _sent(ctx, ADMIN) == []


async def test_on_a_repeat_on_the_marked_row_runs_nothing_more_and_is_not_refunded():
    ctx = await _tick(marked=1)
    ctx.bot_data["radarr"].mark_failed.assert_not_awaited()
    ctx.bot_data["store"].add_pending_autofix.assert_not_awaited()
    assert _sent(ctx, ADMIN) == []
    ctx.bot_data["store"].mark_autofix_repeat.assert_awaited_once_with(1, refunded=False)
    dm = _sent(ctx, 100)[-1]
    assert "given back" not in dm["text"]
    assert any(c.startswith("askadm:") for c in _callbacks(dm))


async def test_on_a_repeat_on_an_admin_fix_runs_nothing():
    ctx = await _tick(user_id=ADMIN)
    ctx.bot_data["radarr"].mark_failed.assert_not_awaited()
    ctx.bot_data["store"].add_pending_autofix.assert_not_awaited()
    assert _sent(ctx, ADMIN) == []


async def test_on_a_busy_title_skips_the_blocklist_and_keeps_the_ask_button():
    ctx = await _tick(busy=True)
    ctx.bot_data["radarr"].mark_failed.assert_not_awaited()
    dm = _sent(ctx, 100)[-1]
    assert "different release now" not in dm["text"]
    assert any(c.startswith("askadm:") for c in _callbacks(dm))
    (admin_dm,) = _sent(ctx, ADMIN)
    assert "another fix is running" in admin_dm["text"]
    ctx.bot_data["store"].add_pending_autofix.assert_not_awaited()


async def test_on_the_title_slot_is_released_after_the_run():
    ctx = await _tick()
    assert "movie:12345" not in ctx.bot_data["inflight_actions"]


async def test_on_a_failed_mark_failed_still_sends_the_completion_dm_and_tells_the_admin():
    ctx = await _tick(mark_failed=FixResult.failed("Radarr is down."))
    ctx.bot_data["store"].add_pending_autofix.assert_not_awaited()
    (admin_dm,) = _sent(ctx, ADMIN)
    assert "Radarr is down." in admin_dm["text"]
    assert "Test Movie" in admin_dm["text"]
    assert "Movie.2020-GRP" in admin_dm["text"]
    assert "issues/7" in admin_dm["text"]
    assert "Mark Failed" in admin_dm["text"]
    dm = _sent(ctx, 100)[-1]
    assert "different release now" not in dm["text"]
    assert any(c.startswith("askadm:") for c in _callbacks(dm))


async def test_on_a_resolved_ticket_runs_nothing_and_tells_the_admin():
    ctx = await _tick(resolved=True)
    ctx.bot_data["radarr"].mark_failed.assert_not_awaited()
    (admin_dm,) = _sent(ctx, ADMIN)
    assert "resolved" in admin_dm["text"]


async def test_on_a_failed_admin_dm_never_stops_the_reporters_dm():
    fix = _fix()
    fix.is_complete = AsyncMock(return_value=(True, ""))  # type: ignore[method-assign]
    ctx = make_ctx(admin_id=ADMIN)
    ctx.bot_data["settings_store"].settings.autofix_blocklist_on_repeat = True
    ctx.bot_data["seerr"].get_issue.return_value = IssueListItem(
        id=7, issue_type=1, status=2, created_at="2026-05-29T12:00:00Z",
        tmdb_id=12345, media_type="movie", problem_season=None,
        problem_episode=None, created_by="someone")
    radarr = ctx.bot_data["radarr"]
    radarr.read_fingerprint = AsyncMock(return_value=FP(
        release_name="Movie.2020-GRP", group="GRP", quality="WEBDL-1080p"))
    radarr.get_queue_progress = AsyncMock(return_value=None)
    store = ctx.bot_data["store"]
    store.list_pending_autofixes = AsyncMock(return_value=[fix])
    store.mark_autofix_status = AsyncMock()
    reporter_sent = []

    async def send(**kw):
        if kw["chat_id"] == ADMIN:
            raise RuntimeError("blocked")
        reporter_sent.append(kw)
        return make_message(message_id=900)

    ctx.bot.send_message = AsyncMock(side_effect=send)
    ctx.bot.edit_message_text = AsyncMock()
    await poll_pending_autofixes(ctx)
    assert reporter_sent
    assert any(c.startswith("askadm:") for c in _callbacks(reporter_sent[-1]))


# --- store -------------------------------------------------------------------------


async def test_a_marked_row_reads_back_marked_and_an_ordinary_one_does_not(
        fresh_store: UserStore):
    common = dict(chat_id=1, user_id=2, media_type="movie", label="M",
                  ticket_id=3, ticket_url="u", radarr_movie_id=9)
    await fresh_store.add_pending_autofix(**common)
    await fresh_store.add_pending_autofix(**common, auto_blocklisted=True)
    plain, marked = await fresh_store.list_pending_autofixes()
    assert (plain.auto_blocklisted, marked.auto_blocklisted) == (0, 1)


async def test_a_schema_4_database_without_the_column_gets_it(
        tmp_db_path: Path, fresh_token_crypto):
    with sqlite3.connect(tmp_db_path) as c:
        c.execute(
            """CREATE TABLE pending_autofixes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL, media_type TEXT NOT NULL,
                label TEXT NOT NULL, issue_id INTEGER NOT NULL,
                issue_url TEXT NOT NULL, timeout_at TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT (datetime('now')),
                radarr_movie_id INTEGER, sonarr_series_id INTEGER,
                sonarr_episode_id INTEGER, sonarr_season INTEGER,
                expected_episode_ids TEXT NOT NULL DEFAULT '[]')""")
        c.execute("PRAGMA user_version = 4")
    store = UserStore(tmp_db_path, crypto=fresh_token_crypto)
    await store.add_pending_autofix(chat_id=1, user_id=2, media_type="movie",
                                    label="M", ticket_id=3, ticket_url="u",
                                    auto_blocklisted=True)
    (row,) = await store.list_pending_autofixes()
    assert row.auto_blocklisted == 1


async def test_on_an_error_before_mark_failed_tells_the_admin():
    fix = _fix()
    fix.is_complete = AsyncMock(return_value=(True, ""))  # type: ignore[method-assign]
    ctx = make_ctx(admin_id=ADMIN)
    ctx.bot_data["settings_store"].settings.autofix_blocklist_on_repeat = True
    ctx.bot_data["seerr"].get_issue.side_effect = RuntimeError("boom")
    radarr = ctx.bot_data["radarr"]
    radarr.read_fingerprint = AsyncMock(return_value=FP(
        release_name="Movie.2020-GRP", group="GRP", quality="WEBDL-1080p"))
    radarr.get_queue_progress = AsyncMock(return_value=None)
    store = ctx.bot_data["store"]
    store.list_pending_autofixes = AsyncMock(return_value=[fix])
    store.mark_autofix_status = AsyncMock()
    ctx.bot.send_message = AsyncMock(return_value=make_message(message_id=900))
    ctx.bot.edit_message_text = AsyncMock()
    await poll_pending_autofixes(ctx)
    radarr.mark_failed.assert_not_awaited()
    assert len(_sent(ctx, ADMIN)) == 1
    assert len(_sent(ctx, 100)) == 1


async def test_on_an_error_after_mark_failed_ran_sends_no_skip_dm():
    fix = _fix()
    fix.is_complete = AsyncMock(return_value=(True, ""))  # type: ignore[method-assign]
    ctx = make_ctx(admin_id=ADMIN)
    ctx.bot_data["settings_store"].settings.autofix_blocklist_on_repeat = True
    ctx.bot_data["seerr"].get_issue.return_value = IssueListItem(
        id=7, issue_type=1, status=1, created_at="2026-05-29T12:00:00Z",
        tmdb_id=12345, media_type="movie", problem_season=None,
        problem_episode=None, created_by="someone")
    radarr = ctx.bot_data["radarr"]
    radarr.read_fingerprint = AsyncMock(return_value=FP(
        release_name="Movie.2020-GRP", group="GRP", quality="WEBDL-1080p"))
    radarr.get_queue_progress = AsyncMock(return_value=None)
    radarr.mark_failed.return_value = FixResult.success(
        "Blocklisted and searching.", ["blocklist"], poll_info={"movie_id": 555})
    store = ctx.bot_data["store"]
    store.list_pending_autofixes = AsyncMock(return_value=[fix])
    store.mark_autofix_status = AsyncMock()
    store.log_autofix.side_effect = RuntimeError("db down")
    ctx.bot.send_message = AsyncMock(return_value=make_message(message_id=900))
    ctx.bot.edit_message_text = AsyncMock()
    await poll_pending_autofixes(ctx)
    radarr.mark_failed.assert_awaited_once()
    assert _sent(ctx, ADMIN) == []
    assert len(_sent(ctx, 100)) == 1
