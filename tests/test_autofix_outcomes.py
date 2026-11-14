"""Auto-fix events carry a kind, an outcome and a refund flag, tied to their
pending fix row; a Reporter's repeat gives the daily use back."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from bot import autofix_poll
from bot.autofix_poll import (_notify_complete, _notify_timeout,
                              poll_pending_autofixes)
from fix_result import ReleaseFingerprint as FP
from store import PendingAutofix, UserStore
from tests._handler_harness import make_ctx, make_message


def _events(path: Path) -> list[tuple]:
    with sqlite3.connect(path) as c:
        return c.execute(
            "SELECT telegram_id, tmdb_id, kind, outcome, refunded, pending_id "
            "FROM autofix_events ORDER BY id").fetchall()


async def _enqueue(store: UserStore, user: int, *, kind="auto-fix", tmdb=5):
    event = await store.log_autofix(user, "movie", tmdb, kind=kind)
    pid = await store.add_pending_autofix(
        chat_id=1, user_id=user, media_type="movie", label="M", ticket_id=3,
        ticket_url="u", radarr_movie_id=9, event_id=event)
    return event, pid


# --- store -------------------------------------------------------------------


async def test_refunded_event_is_not_counted_but_an_unrefunded_one_is(
        fresh_store: UserStore):
    _, first = await _enqueue(fresh_store, 42)
    await _enqueue(fresh_store, 42, tmdb=6)
    assert await fresh_store.count_autofix_24h(42) == 2
    await fresh_store.mark_autofix_repeat(first, refunded=True)
    assert await fresh_store.count_autofix_24h(42) == 1


async def test_an_unrefunded_repeat_still_counts(fresh_store: UserStore):
    _, pid = await _enqueue(fresh_store, 1)
    await fresh_store.mark_autofix_repeat(pid, refunded=False)
    assert await fresh_store.count_autofix_24h(1) == 1


async def test_event_is_tied_to_its_pending_row_and_keeps_its_kind(
        fresh_store: UserStore, tmp_db_path: Path):
    _, pid = await _enqueue(fresh_store, 1, kind="mark failed")
    await fresh_store.log_autofix(2, "tv", 7, season=1, episode=2)
    rows = _events(tmp_db_path)
    assert rows[0] == (1, 5, "mark failed", "pending", 0, pid)
    assert rows[1][2:] == ("auto-fix", "pending", 0, None)


@pytest.mark.parametrize("status", ["complete", "timeout", "failed"])
async def test_status_marks_write_the_matching_outcome(
        fresh_store: UserStore, tmp_db_path: Path, status):
    _, pid = await _enqueue(fresh_store, 1)
    await fresh_store.mark_autofix_status(pid, status)
    assert _events(tmp_db_path)[0][3] == status


async def test_repeat_writes_the_repeat_outcome(
        fresh_store: UserStore, tmp_db_path: Path):
    _, pid = await _enqueue(fresh_store, 1)
    await fresh_store.mark_autofix_status(pid, "complete")
    await fresh_store.mark_autofix_repeat(pid, refunded=True)
    assert _events(tmp_db_path)[0][3:5] == ("repeat", 1)


async def test_prune_deletes_events_older_than_30_days_and_keeps_a_3_day_old_one(
        fresh_store: UserStore, tmp_db_path: Path):
    for tmdb in (1, 2, 3):
        await fresh_store.log_autofix(1, "movie", tmdb)
    with sqlite3.connect(tmp_db_path) as c:
        c.execute("UPDATE autofix_events SET occurred_at = datetime('now', '-31 days') "
                  "WHERE tmdb_id = 1")
        c.execute("UPDATE autofix_events SET occurred_at = datetime('now', '-3 days') "
                  "WHERE tmdb_id = 2")
        c.execute("UPDATE autofix_events SET occurred_at = datetime('now', '-29 days') "
                  "WHERE tmdb_id = 3")
    _, events = await fresh_store.prune_autofix_history()
    assert events == 1
    assert sorted(r[1] for r in _events(tmp_db_path)) == [2, 3]


async def test_database_already_at_schema_4_without_the_event_columns_gets_them(
        tmp_db_path: Path, fresh_token_crypto):
    with sqlite3.connect(tmp_db_path) as c:
        c.execute(
            """CREATE TABLE autofix_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL,
                occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
                media_type TEXT NOT NULL, tmdb_id INTEGER NOT NULL,
                season INTEGER, episode INTEGER)""")
        c.execute("INSERT INTO autofix_events (telegram_id, media_type, tmdb_id) "
                  "VALUES (4, 'movie', 8)")
        c.execute("PRAGMA user_version = 4")
    store = UserStore(tmp_db_path, crypto=fresh_token_crypto)
    assert _events(tmp_db_path) == [(4, 8, "auto-fix", "pending", 0, None)]
    assert await store.count_autofix_24h(4) == 1


# --- poller ------------------------------------------------------------------


def _fix(user_id: int = 42) -> PendingAutofix:
    now = datetime.now(timezone.utc)
    return PendingAutofix(
        id=1, chat_id=100, user_id=user_id, media_type="movie",
        radarr_movie_id=555, sonarr_series_id=None, sonarr_episode_id=None,
        sonarr_season=None, expected_episode_ids=[], label="Test Movie",
        ticket_id=7, ticket_url="http://seerr.example/issues/7",
        started_at=now.strftime("%Y-%m-%d %H:%M:%S"),
        timeout_at=(now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
        message_id=300, before_fingerprint=FP(release_name="Movie.2020-GRP"))


@pytest.fixture(autouse=True)
def _clear_inflight():
    autofix_poll._inflight.clear()
    yield
    autofix_poll._inflight.clear()


async def _tick(monkeypatch, *, same: bool, user_id=42, unlimited=False,
                timed_out=False, is_complete=None):
    monkeypatch.setattr(autofix_poll, "_notify_complete", _notify_complete)
    monkeypatch.setattr(autofix_poll, "_notify_timeout", _notify_timeout)
    fix = _fix(user_id)
    if timed_out:
        fix.timeout_at = "2000-01-01 00:00:00"
    fix.is_complete = is_complete or AsyncMock(return_value=(True, ""))  # type: ignore[method-assign]
    ctx = make_ctx()
    ctx.bot_data["settings_store"].settings.daily_autofix_unlimited = unlimited
    after = FP(release_name="Movie.2020-GRP" if same else "Other.2020-X")
    ctx.bot_data["radarr"].read_fingerprint = AsyncMock(return_value=after)
    ctx.bot_data["radarr"].get_queue_progress = AsyncMock(return_value=None)
    store = ctx.bot_data["store"]
    store.list_pending_autofixes = AsyncMock(return_value=[fix])
    store.mark_autofix_status = AsyncMock()
    store.set_autofix_progress = AsyncMock()
    ctx.bot.send_message = AsyncMock(return_value=make_message(message_id=900))
    ctx.bot.edit_message_text = AsyncMock()
    await poll_pending_autofixes(ctx)
    return ctx, store


def _dm(ctx) -> str:
    return ctx.bot.send_message.call_args.kwargs["text"]


async def test_a_reporters_repeat_is_refunded_and_the_dm_says_so(monkeypatch):
    ctx, store = await _tick(monkeypatch, same=True)
    store.mark_autofix_repeat.assert_awaited_once_with(1, refunded=True)
    assert "That use was given back" in _dm(ctx)


async def test_no_refund_line_when_the_daily_limit_is_unlimited(monkeypatch):
    ctx, _ = await _tick(monkeypatch, same=True, unlimited=True)
    assert "given back" not in _dm(ctx)


async def test_the_admins_repeat_is_never_refunded(monkeypatch):
    ctx, store = await _tick(monkeypatch, same=True, user_id=999)  # the harness admin
    store.mark_autofix_repeat.assert_awaited_once_with(1, refunded=False)
    assert "given back" not in _dm(ctx)


async def test_a_complete_that_is_not_a_repeat_is_not_refunded(monkeypatch):
    ctx, store = await _tick(monkeypatch, same=False)
    store.mark_autofix_status.assert_awaited_once_with(1, "complete")
    store.mark_autofix_repeat.assert_not_awaited()
    assert "given back" not in _dm(ctx)


async def test_a_timeout_records_the_timeout_outcome(monkeypatch):
    _, store = await _tick(monkeypatch, same=False, timed_out=True)
    store.mark_autofix_status.assert_awaited_once_with(1, "timeout")


async def test_removed_media_records_the_failed_outcome(monkeypatch):
    from http_util import NotFoundAPIError
    _, store = await _tick(monkeypatch, same=False,
                           is_complete=AsyncMock(side_effect=NotFoundAPIError("gone")))
    store.mark_autofix_status.assert_awaited_once_with(1, "failed")


async def test_a_failed_repeat_write_still_sends_the_dm_without_the_refund_line(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db")
    monkeypatch.setattr(autofix_poll, "_notify_complete", _notify_complete)
    fix = _fix()
    fix.is_complete = AsyncMock(return_value=(True, ""))  # type: ignore[method-assign]
    ctx = make_ctx()
    ctx.bot_data["radarr"].read_fingerprint = AsyncMock(
        return_value=FP(release_name="Movie.2020-GRP"))
    ctx.bot_data["radarr"].get_queue_progress = AsyncMock(return_value=None)
    store = ctx.bot_data["store"]
    store.list_pending_autofixes = AsyncMock(return_value=[fix])
    store.mark_autofix_status = AsyncMock()
    store.mark_autofix_repeat = boom
    store.set_autofix_progress = AsyncMock()
    ctx.bot.send_message = AsyncMock(return_value=make_message(message_id=900))
    ctx.bot.edit_message_text = AsyncMock()
    await poll_pending_autofixes(ctx)
    ctx.bot.send_message.assert_awaited_once()
    assert "given back" not in _dm(ctx)
