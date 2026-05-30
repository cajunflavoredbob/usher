"""Tests for bot.tickets._apply_fix: admin gate, get_issue resolution,
strategy dispatch, FixResult handling (ok/partial/failed), pending-autofix
enqueue, message rendering."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from bot.tickets import _apply_fix
from fix_result import FixResult
from seerr import IssueListItem
from tests._handler_harness import make_ctx, make_update


def _issue(*, media_type: str = "movie", tmdb_id: int = 12345,
           season=None, episode=None) -> IssueListItem:
    return IssueListItem(
        id=42, issue_type=1, status=1, created_at="2026-05-29T12:00:00Z",
        tmdb_id=tmdb_id, media_type=media_type,
        problem_season=season, problem_episode=episode,
        created_by="someone",
    )


# --- admin gate ---


async def test_non_admin_blocked_with_toast_and_audit(caplog):
    upd = make_update(callback_data="tkfd:42", user_id=42)  # not admin
    ctx = make_ctx(admin_id=999)
    caplog.set_level(logging.WARNING, logger="hermes.audit")
    await _apply_fix(upd, ctx, strategy="redownload")
    # Two answer() calls happen: first the empty ack at handler entry,
    # second the "Admin only." toast from _require_admin.
    toast_texts = [text for text, _alert in upd.callback_query.answers]
    assert "Admin only." in toast_texts
    # Audit event
    audit_msgs = [r.getMessage() for r in caplog.records if r.name == "hermes.audit"]
    assert any("admin_callback_blocked" in m for m in audit_msgs)
    # get_issue NEVER called
    ctx.bot_data["seerr"].get_issue.assert_not_called()


# --- happy path: movie redownload ---


async def test_movie_redownload_happy_path_enqueues_poller():
    upd = make_update(callback_data="tkfd:42", user_id=999)
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["seerr"].get_issue.return_value = _issue(media_type="movie")
    ctx.bot_data["seerr"].get_media_title.return_value = ("Inception", "2010")
    ctx.bot_data["radarr"].auto_fix.return_value = FixResult.success(
        "Deleted file and triggered re-search.",
        steps_done=["delete", "search"],
        poll_info={"movie_id": 555},
    )

    await _apply_fix(upd, ctx, strategy="redownload")

    # Pending autofix enqueued
    ctx.bot_data["store"].add_pending_autofix.assert_called_once()
    kwargs = ctx.bot_data["store"].add_pending_autofix.call_args.kwargs
    assert kwargs["media_type"] == "movie"
    assert kwargs["radarr_movie_id"] == 555
    assert kwargs["issue_id"] == 42

    # log_autofix called
    ctx.bot_data["store"].log_autofix.assert_called_once()

    # Success message edited in
    assert upd.callback_query.edits
    text = upd.callback_query.edits[0]["text"]
    assert "🔧 Redownload for #42" in text
    assert "Inception (2010)" in text
    assert "I'll DM when the new file finishes" in text


# --- happy path: mark_failed ---


async def test_movie_mark_failed_happy_path():
    upd = make_update(callback_data="tkfm:42", user_id=999)
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["seerr"].get_issue.return_value = _issue(media_type="movie")
    ctx.bot_data["radarr"].mark_failed.return_value = FixResult.success(
        "Blocklisted current release, deleted 'M' file, and triggered re-search.",
        steps_done=["blocklist", "delete", "search"],
        poll_info={"movie_id": 555},
    )
    await _apply_fix(upd, ctx, strategy="mark_failed")
    ctx.bot_data["radarr"].mark_failed.assert_called_once()
    ctx.bot_data["store"].add_pending_autofix.assert_called_once()
    assert "🔧 Mark Failed for #42" in upd.callback_query.edits[0]["text"]


# --- failed path: get_issue fails ---


async def test_get_issue_failure_surfaces_friendly_message():
    upd = make_update(callback_data="tkfd:42", user_id=999)
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["seerr"].get_issue.side_effect = RuntimeError("seerr-down-noise")
    await _apply_fix(upd, ctx, strategy="redownload")
    assert upd.callback_query.edits
    text = upd.callback_query.edits[0]["text"]
    assert "Couldn't fetch ticket #42" in text
    # Raw exception NOT echoed
    assert "seerr-down-noise" not in text
    # No enqueue
    ctx.bot_data["store"].add_pending_autofix.assert_not_called()


# --- whole-season TV rejected ---


async def test_whole_season_tv_rejected():
    upd = make_update(callback_data="tkfd:42", user_id=999)
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["seerr"].get_issue.return_value = _issue(
        media_type="tv", season=1, episode=None,
    )
    await _apply_fix(upd, ctx, strategy="redownload")
    text = upd.callback_query.edits[0]["text"]
    assert "only works on individual episodes" in text
    # Neither radarr.auto_fix nor sonarr.auto_fix_episode hit
    ctx.bot_data["sonarr"].auto_fix_episode.assert_not_called()
    ctx.bot_data["radarr"].auto_fix.assert_not_called()
    ctx.bot_data["store"].add_pending_autofix.assert_not_called()


# --- partial success ---


async def test_partial_success_still_enqueues_poller():
    """Search ran but delete failed before it -> partial. Poller should still
    be enqueued (the new file is incoming) but message reflects the partial."""
    upd = make_update(callback_data="tkfm:42", user_id=999)
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["seerr"].get_issue.return_value = _issue(media_type="movie")
    ctx.bot_data["radarr"].mark_failed.return_value = FixResult.partial(
        "Blocklisted release but couldn't delete file: <permanent>",
        steps_done=["blocklist", "search"],
        poll_info={"movie_id": 555},
    )

    await _apply_fix(upd, ctx, strategy="mark_failed")

    # Poller IS enqueued (should_poll is True because 'search' is in steps_done)
    ctx.bot_data["store"].add_pending_autofix.assert_called_once()
    # Message uses the partial prefix
    text = upd.callback_query.edits[0]["text"]
    assert "⚠️ Mark Failed for #42" in text


# --- failed: arr returns failure ---


async def test_arr_failure_no_enqueue():
    upd = make_update(callback_data="tkfd:42", user_id=999)
    ctx = make_ctx(admin_id=999)
    ctx.bot_data["seerr"].get_issue.return_value = _issue(media_type="movie")
    ctx.bot_data["radarr"].auto_fix.return_value = FixResult.failed(
        "Movie isn't in Radarr (not monitored)."
    )
    await _apply_fix(upd, ctx, strategy="redownload")
    text = upd.callback_query.edits[0]["text"]
    assert "⚠️ Redownload for #42 didn't run" in text
    ctx.bot_data["store"].add_pending_autofix.assert_not_called()
