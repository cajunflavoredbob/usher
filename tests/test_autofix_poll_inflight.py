"""Regression for audit CONC #8: the poller skips fix IDs already being
processed by a prior tick, so a slow tick that overlaps the next 60s mark
doesn't double-notify on the same fix."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import autofix_poll
from store import PendingAutofix


def _make_fix(fix_id: int = 1) -> PendingAutofix:
    return PendingAutofix(
        id=fix_id, chat_id=100, user_id=42,
        media_type="movie", radarr_movie_id=555,
        sonarr_series_id=None, sonarr_episode_id=None,
        sonarr_season=None, expected_episode_ids=[],
        label="Test Movie", issue_id=fix_id, issue_url="http://x/issues/1",
        started_at="2026-05-30 00:00:00",
        timeout_at="2026-05-30 23:59:59",
    )


def _make_ctx(fix: PendingAutofix, *,
              is_complete_event: asyncio.Event,
              is_complete_return: tuple[bool, str] = (True, "")):
    """Build a ctx whose store returns `fix` and whose radarr.movie_has_file
    waits on `is_complete_event` before returning. Used to simulate a slow
    is_complete during which the next tick can fire."""
    notified: list[int] = []
    marked: list[tuple[int, str]] = []

    async def list_pending():
        return [fix]

    async def mark_status(fix_id, status):
        marked.append((fix_id, status))

    async def is_complete_slow(radarr, sonarr):
        await is_complete_event.wait()
        return is_complete_return

    # Monkey-patch the bound method on the dataclass instance.
    fix.is_complete = is_complete_slow  # type: ignore[method-assign]

    async def notify_complete(_ctx, fx, extra=""):
        notified.append(fx.id)

    async def notify_timeout(_ctx, fx):
        notified.append(("timeout", fx.id))

    # Patch the module-level notify helpers for the test.
    autofix_poll._notify_complete = notify_complete  # type: ignore[assignment]
    autofix_poll._notify_timeout = notify_timeout  # type: ignore[assignment]

    store = SimpleNamespace(
        list_pending_autofixes=list_pending,
        mark_autofix_status=mark_status,
    )
    ctx = SimpleNamespace(bot_data={"store": store, "radarr": object(), "sonarr": None})
    return ctx, notified, marked


@pytest.fixture(autouse=True)
def _clear_inflight():
    """Ensure each test starts with an empty in-flight set."""
    autofix_poll._inflight.clear()
    yield
    autofix_poll._inflight.clear()


async def test_overlapping_ticks_dedupe_on_same_fix():
    """While a slow tick is mid-await on is_complete, the next tick sees the
    fix in _inflight and skips it. Only the first tick's _notify_complete
    fires."""
    fix = _make_fix(fix_id=42)
    gate = asyncio.Event()
    ctx, notified, marked = _make_ctx(fix, is_complete_event=gate,
                                       is_complete_return=(True, ""))

    # Tick 1: kick it off but don't let it finish yet.
    tick1 = asyncio.create_task(autofix_poll.poll_pending_autofixes(ctx))
    # Yield to let tick1 add fix.id to _inflight + park in is_complete.
    await asyncio.sleep(0.01)
    assert 42 in autofix_poll._inflight
    # Tick 2 starts while tick1 is still parked: should see _inflight + skip.
    await autofix_poll.poll_pending_autofixes(ctx)
    # Tick 2 didn't notify -- the only notification will be from tick1
    # once we release the gate.
    assert notified == []
    # Release the gate so tick1 completes.
    gate.set()
    await tick1
    assert notified == [42]
    assert marked == [(42, "complete")]
    assert 42 not in autofix_poll._inflight  # released in finally


async def test_non_overlapping_ticks_both_process():
    """Sanity: when the prior tick has finished, the next tick processes
    normally."""
    fix = _make_fix(fix_id=7)
    gate = asyncio.Event()
    gate.set()  # is_complete returns immediately
    ctx, notified, marked = _make_ctx(fix, is_complete_event=gate,
                                       is_complete_return=(False, ""))

    await autofix_poll.poll_pending_autofixes(ctx)
    assert 7 not in autofix_poll._inflight
    # Second tick can run cleanly (notified stays empty since done=False).
    await autofix_poll.poll_pending_autofixes(ctx)
    assert notified == []
    assert marked == []
