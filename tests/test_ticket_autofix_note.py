"""The Seerr Ticket only says a fresh download started when the Auto-fix ran."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from bot import ticket_flow
from bot.shared import media_action_key
from fix_result import FixResult
from seerr import CreatedIssue
from tests._handler_harness import make_ctx, make_link, make_update

USER = 42
MEDIA = {"type": "movie", "tmdb_id": 1, "seerr_media_id": 10,
         "title": "Some Movie", "year": "2026"}


def _ctx(*, used=0):
    ctx = make_ctx(admin_id=999, link=make_link(telegram_id=USER),
                   user_data={"media": dict(MEDIA), "issue_type": 1,
                              "description": "no sound"})
    ctx.bot_data["seerr"].create_issue = AsyncMock(
        return_value=CreatedIssue(id=77, url="http://seerr.example/issues/77"))
    s = ctx.bot_data["settings_store"].settings
    s.daily_autofix_unlimited = False
    ctx.bot_data["store"].count_autofix_24h.return_value = used
    return ctx


async def _submit(ctx, result=None, *, raises=None):
    upd = make_update(callback_data="x", user_id=USER)
    run = AsyncMock(return_value=result, side_effect=raises)
    with patch.object(ticket_flow, "run_arr_action", run):
        await ticket_flow._submit_ticket_inner(upd, ctx, autofix=True)
    return upd


def _notes(ctx):
    return [c.args[1] for c in ctx.bot_data["seerr"].add_issue_comment.call_args_list]


def _created_message(ctx):
    return ctx.bot_data["seerr"].create_issue.call_args.kwargs["message"]


async def test_a_ran_fix_adds_a_fresh_download_note_after_the_fix():
    ctx = _ctx()
    await _submit(ctx, FixResult(status="ok", message="done", steps_done=["delete", "search"]))
    assert "fresh download was started" in _notes(ctx)[0]
    assert _created_message(ctx) == "no sound"


async def test_a_partial_fix_also_says_a_fresh_download_started():
    ctx = _ctx()
    await _submit(ctx, FixResult(status="partial", message="half", steps_done=["delete", "search"]))
    assert "fresh download was started" in _notes(ctx)[0]


async def test_a_failed_fix_does_not_claim_the_file_was_removed():
    ctx = _ctx()
    await _submit(ctx, FixResult(status="failed", message="Radarr down"))
    (note,) = _notes(ctx)
    assert "failed" in note and "Radarr down" in note
    assert "removed" not in note and "fresh download" not in note


async def test_a_raising_fix_says_it_failed_and_the_reporter_still_gets_a_reply():
    ctx = _ctx()
    upd = await _submit(ctx, raises=RuntimeError("boom"))
    (note,) = _notes(ctx)
    assert "failed" in note and "fresh download" not in note
    assert "ticket #77" in upd.effective_message.reply_calls[-1]["text"]


async def test_a_busy_title_is_noted_as_skipped():
    ctx = _ctx()
    ctx.bot_data["inflight_actions"] = {media_action_key(MEDIA)}
    await _submit(ctx, FixResult(status="ok", message="x", steps_done=[]))
    (note,) = _notes(ctx)
    assert "skipped" in note and "fresh download" not in note


async def test_the_daily_limit_is_noted_as_skipped():
    ctx = _ctx(used=3)
    await _submit(ctx, FixResult(status="ok", message="x", steps_done=[]))
    (note,) = _notes(ctx)
    assert "skipped" in note and "fresh download" not in note


async def test_a_failed_note_keeps_the_ticket_and_the_reply():
    ctx = _ctx()
    ctx.bot_data["seerr"].add_issue_comment.side_effect = RuntimeError("seerr down")
    upd = await _submit(ctx, FixResult(status="ok", message="done", steps_done=["delete", "search"]))
    assert "ticket #77" in upd.effective_message.reply_calls[-1]["text"]


async def test_a_fix_that_never_searched_does_not_claim_a_download_started():
    ctx = _ctx()
    await _submit(ctx, FixResult(status="partial", message="search failed",
                                 steps_done=["delete"]))
    (note,) = _notes(ctx)
    assert "no fresh download was started" in note
    assert "search failed" in note
    ctx.bot_data["seerr"].add_issue_comment.assert_awaited_once_with(
        77, note, as_plex_token="plex-abc")
