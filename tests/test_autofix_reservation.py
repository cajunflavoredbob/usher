"""The Auto-fix daily quota is reserved atomically before the action runs,
and the allowlist is re-checked when the confirm executes."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from bot import ticket_flow
from fix_result import FixResult
from seerr import CreatedIssue
from tests._handler_harness import make_ctx, make_link, make_update

USER = 42
ADMIN = 999


def _media(n: int) -> dict:
    return {"type": "movie", "tmdb_id": n, "seerr_media_id": 10 + n,
            "title": f"Movie {n}", "year": "2026"}


def _ctx(store, n=1, *, limit=1, allowlist=(USER,)):
    ctx = make_ctx(admin_id=ADMIN, link=make_link(telegram_id=USER),
                   user_data={"media": _media(n), "issue_type": 1,
                              "description": "no sound"})
    # The harness store stays for everything but the Auto-fix quota rows,
    # which go through the real store.
    for name in ("log_autofix", "release_autofix", "count_autofix_24h"):
        setattr(ctx.bot_data["store"], name, getattr(store, name))
    ctx.bot_data["seerr"].create_issue = AsyncMock(
        return_value=CreatedIssue(id=70 + n, url="http://seerr.example/issues/7"))
    s = ctx.bot_data["settings_store"].settings
    s.daily_autofix_unlimited = False
    s.daily_autofix_limit = limit
    ctx.bot_data["allowlist"] = set(allowlist)
    return ctx


async def _submit(ctx, run):
    upd = make_update(callback_data="x", user_id=USER)
    with patch.object(ticket_flow, "run_arr_action", run):
        await ticket_flow._submit_ticket_inner(upd, ctx, autofix=True)
    return upd


def _text(upd) -> str:
    return "\n".join(c.get("text", "") for c in upd.effective_message.reply_calls)


async def test_the_store_reserves_at_most_the_limit_under_parallel_calls(fresh_store):
    ids = await asyncio.gather(*[
        fresh_store.log_autofix(USER, "movie", n, limit=3) for n in range(8)])
    assert len([i for i in ids if i is not None]) == 3
    assert await fresh_store.count_autofix_24h(USER) == 3


async def test_a_released_reservation_frees_the_use(fresh_store):
    first = await fresh_store.log_autofix(USER, "movie", 1, limit=1)
    assert await fresh_store.log_autofix(USER, "movie", 2, limit=1) is None
    await fresh_store.release_autofix(first)
    assert await fresh_store.log_autofix(USER, "movie", 2, limit=1) is not None


async def test_overlapping_submits_cannot_exceed_the_daily_limit(fresh_store):
    started = asyncio.Event()

    async def slow_ok(*a, **k):
        started.set()
        await asyncio.sleep(0.05)
        return FixResult.success("ok", steps_done=["delete"])

    run = AsyncMock(side_effect=slow_ok)
    ctx1, ctx2 = _ctx(fresh_store, 1), _ctx(fresh_store, 2)
    u1, u2 = await asyncio.gather(_submit(ctx1, run), _submit(ctx2, run))
    assert run.await_count == 1
    assert await fresh_store.count_autofix_24h(USER) == 1
    texts = _text(u1) + _text(u2)
    assert texts.count("Auto-fix skipped: you've used your 1 auto-fixes today.") == 1


async def test_a_fix_that_fails_gives_the_use_back(fresh_store):
    run = AsyncMock(return_value=FixResult.failed("radarr is down"))
    upd = await _submit(_ctx(fresh_store), run)
    assert "radarr is down" in _text(upd)
    assert await fresh_store.count_autofix_24h(USER) == 0


async def test_a_fix_that_raises_gives_the_use_back(fresh_store):
    run = AsyncMock(side_effect=RuntimeError("boom"))
    await _submit(_ctx(fresh_store), run)
    assert await fresh_store.count_autofix_24h(USER) == 0


async def test_a_fix_that_ran_keeps_its_use(fresh_store):
    run = AsyncMock(return_value=FixResult.success("ok", steps_done=["delete"]))
    await _submit(_ctx(fresh_store), run)
    assert await fresh_store.count_autofix_24h(USER) == 1


async def test_a_user_removed_from_the_allowlist_before_the_confirm_gets_no_fix(fresh_store):
    run = AsyncMock(return_value=FixResult.success("ok", steps_done=["delete"]))
    ctx = _ctx(fresh_store, allowlist=())
    upd = await _submit(ctx, run)
    run.assert_not_awaited()
    assert "no longer turned on for you" in _text(upd)
    notes = [c.args[1] for c in ctx.bot_data["seerr"].add_issue_comment.call_args_list]
    assert "no longer allowed" in notes[0]
    assert await fresh_store.count_autofix_24h(USER) == 0
    ctx.bot_data["seerr"].create_issue.assert_awaited_once()


async def test_allow_all_still_runs_for_a_user_not_on_the_list(fresh_store):
    run = AsyncMock(return_value=FixResult.success("ok", steps_done=["delete"]))
    ctx = _ctx(fresh_store, allowlist=())
    ctx.bot_data["autofix_allow_all"] = True
    await _submit(ctx, run)
    run.assert_awaited_once()



async def test_release_leaves_a_use_a_pending_fix_has_claimed(fresh_store):
    event = await fresh_store.log_autofix(USER, "movie", 1, limit=5)
    with fresh_store._conn() as c:
        c.execute("UPDATE autofix_events SET pending_id = 9 WHERE id = ?", (event,))
    await fresh_store.release_autofix(event)
    assert await fresh_store.count_autofix_24h(USER) == 1
