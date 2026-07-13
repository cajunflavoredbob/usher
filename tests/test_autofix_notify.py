"""Tests for the autofix poller notification helpers: button-gate recording
(the v0.11.18 bug class, missed in the poller), HTML escaping of media
titles (legacy Markdown died on titles like M*A*S*H and lost the DM),
legacy rows with an empty issue_url, and mark-before-notify ordering."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import autofix_poll
# Bound at import time (before any test runs) so the module-attribute
# patching in test_autofix_poll_inflight can't swap them out from under us.
from bot.autofix_poll import (
    _notify_complete,
    _notify_media_gone,
    _notify_timeout,
    poll_pending_autofixes,
)
from store import PendingAutofix
from tests._handler_harness import make_ctx, make_message


def _make_fix(*, fix_id: int = 1, label: str = "Test Movie",
              issue_url: str = "http://seerr.example/issues/1",
              timed_out: bool = False) -> PendingAutofix:
    now = datetime.now(timezone.utc)
    delta = timedelta(hours=-1 if timed_out else 1)
    return PendingAutofix(
        id=fix_id, chat_id=100, user_id=42,
        media_type="movie", radarr_movie_id=555,
        sonarr_series_id=None, sonarr_episode_id=None,
        sonarr_season=None, expected_episode_ids=[],
        label=label, issue_id=fix_id, issue_url=issue_url,
        started_at=now.strftime("%Y-%m-%d %H:%M:%S"),
        timeout_at=(now + delta).strftime("%Y-%m-%d %H:%M:%S"),
    )


@pytest.fixture(autouse=True)
def _clear_inflight():
    autofix_poll._inflight.clear()
    yield
    autofix_poll._inflight.clear()


# --- notify helpers: record_btn + HTML ---------------------------------------


async def test_notify_complete_records_keyboard_with_gate():
    """Regression for the v0.11.18 bug class: the RESOLVE keyboard must be
    recorded or the button gate rejects every tap for users with history."""
    fix = _make_fix()
    ctx = make_ctx()
    ctx.bot.send_message = AsyncMock(return_value=make_message(message_id=777))
    await _notify_complete(ctx, fix)
    entries = ctx.application.bot_data["btn_msgs"][fix.user_id]
    assert [e["message_id"] for e in entries] == [777]


async def test_notify_timeout_records_keyboard_with_gate():
    fix = _make_fix()
    ctx = make_ctx()
    ctx.bot.send_message = AsyncMock(return_value=make_message(message_id=778))
    await _notify_timeout(ctx, fix)
    entries = ctx.application.bot_data["btn_msgs"][fix.user_id]
    assert [e["message_id"] for e in entries] == [778]


async def test_timeout_copy_for_user_mentions_admin():
    fix = _make_fix(timed_out=True)  # user_id=42
    ctx = make_ctx(admin_id=999)
    ctx.bot.send_message = AsyncMock(return_value=make_message(message_id=790))
    await _notify_timeout(ctx, fix)
    assert "for the admin to follow up" in ctx.bot.send_message.call_args.kwargs["text"]


async def test_timeout_copy_for_admin_omits_admin_reference():
    """The admin follows up on their own issues; the timeout DM must not
    tell them to leave a comment 'for the admin'."""
    fix = _make_fix(timed_out=True)  # user_id=42
    ctx = make_ctx(admin_id=42)
    ctx.bot.send_message = AsyncMock(return_value=make_message(message_id=791))
    await _notify_timeout(ctx, fix)
    text = ctx.bot.send_message.call_args.kwargs["text"]
    assert "add a note to the ticket" in text
    assert "for the admin" not in text


async def test_notify_uses_html_and_escapes_title():
    """A title full of Markdown/HTML metacharacters must neither kill the
    send (legacy Markdown raised BadRequest) nor inject markup."""
    fix = _make_fix(label="M*A*S*H (1972) <Special_Cut>")
    ctx = make_ctx()
    ctx.bot.send_message = AsyncMock(return_value=make_message(message_id=779))
    await _notify_complete(ctx, fix)
    kwargs = ctx.bot.send_message.call_args.kwargs
    assert kwargs["parse_mode"] == "HTML"
    assert "M*A*S*H (1972) &lt;Special_Cut&gt;" in kwargs["text"]


async def test_media_gone_uses_html_and_escapes_title():
    fix = _make_fix(label="Weird <Title>")
    ctx = make_ctx()
    ctx.bot.send_message = AsyncMock(return_value=make_message(message_id=780))
    await _notify_media_gone(ctx, fix)
    kwargs = ctx.bot.send_message.call_args.kwargs
    assert kwargs["parse_mode"] == "HTML"
    assert "Weird &lt;Title&gt;" in kwargs["text"]


async def test_empty_issue_url_omits_dangling_line():
    """Legacy rows from the admin-fix path were enqueued with issue_url="";
    the DM must not render a dangling 'Original issue:' label."""
    fix = _make_fix(issue_url="")
    ctx = make_ctx()
    ctx.bot.send_message = AsyncMock(return_value=make_message(message_id=781))
    await _notify_complete(ctx, fix)
    assert "Original issue:" not in ctx.bot.send_message.call_args.kwargs["text"]


# --- poller: mark-before-notify ordering --------------------------------------


def _poll_ctx(fix, *, mark_side_effect=None):
    store = SimpleNamespace(
        list_pending_autofixes=AsyncMock(return_value=[fix]),
        mark_autofix_status=AsyncMock(side_effect=mark_side_effect),
    )
    return SimpleNamespace(bot_data={"store": store, "radarr": object(),
                                     "sonarr": None}), store


async def test_complete_marks_before_notifying(monkeypatch):
    events: list[str] = []
    fix = _make_fix()

    async def is_complete(radarr, sonarr):
        return True, ""
    fix.is_complete = is_complete  # type: ignore[method-assign]

    async def fake_notify(_ctx, fx, extra=""):
        events.append("notify")
    monkeypatch.setattr(autofix_poll, "_notify_complete", fake_notify)

    ctx, store = _poll_ctx(fix)
    store.mark_autofix_status.side_effect = (
        lambda *a, **k: events.append("mark"))
    await poll_pending_autofixes(ctx)
    assert events == ["mark", "notify"]


async def test_corrupt_timeout_at_is_treated_as_timed_out(monkeypatch):
    """A row with an unparseable timeout_at has no time bound at all; it must
    exit the poll set as a timeout instead of re-polling forever."""
    fix = _make_fix()
    fix.timeout_at = "not-a-timestamp"
    notified: list[int] = []

    async def fake_notify(_ctx, fx):
        notified.append(fx.id)
    monkeypatch.setattr(autofix_poll, "_notify_timeout", fake_notify)

    ctx, store = _poll_ctx(fix)
    await poll_pending_autofixes(ctx)
    store.mark_autofix_status.assert_called_once_with(fix.id, "timeout")
    assert notified == [fix.id]


async def test_failed_mark_suppresses_completion_dm(monkeypatch):
    """If the status write fails, the row stays pending and is retried next
    tick; the DM must NOT go out (lose-one-notification over per-minute
    spam)."""
    notified: list[int] = []
    fix = _make_fix()

    async def is_complete(radarr, sonarr):
        return True, ""
    fix.is_complete = is_complete  # type: ignore[method-assign]

    async def fake_notify(_ctx, fx, extra=""):
        notified.append(fx.id)
    monkeypatch.setattr(autofix_poll, "_notify_complete", fake_notify)

    ctx, _store = _poll_ctx(fix, mark_side_effect=RuntimeError("db locked"))
    await poll_pending_autofixes(ctx)
    assert notified == []


async def test_failed_mark_suppresses_timeout_dm(monkeypatch):
    notified: list[int] = []
    fix = _make_fix(timed_out=True)

    async def fake_notify(_ctx, fx):
        notified.append(fx.id)
    monkeypatch.setattr(autofix_poll, "_notify_timeout", fake_notify)

    ctx, _store = _poll_ctx(fix, mark_side_effect=RuntimeError("db locked"))
    await poll_pending_autofixes(ctx)
    assert notified == []
