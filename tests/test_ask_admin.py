"""Ask the Admin to try a different release: the offer on a repeat DM
and after a comment, the one-per-Ticket Admin DM with Mark Failed first, and
the escalation surviving a restart."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from bot import autofix_poll
from bot.ask_admin import ALREADY_SENT, ask_admin
from bot.autofix_poll import _notify_complete
from bot.resolve_flow import _resume_resolve_comment, resolve_comment
from bot.tickets import apply_fix
from fix_result import FixResult, ReleaseFingerprint as FP
from seerr import IssueListItem
from store import PendingAutofix, UserStore
from tests._handler_harness import make_ctx, make_link, make_message, make_update

ADMIN = 999
REPORTER = 42


def _ticket() -> IssueListItem:
    return IssueListItem(
        id=7, issue_type=1, status=1, created_at="2026-11-14T00:00:00Z",
        tmdb_id=1, media_type="tv", problem_season=2, problem_episode=3,
        created_by="Reporter <b>", description="x")


def _fix(user_id: int = REPORTER) -> PendingAutofix:
    now = datetime.now(timezone.utc)
    return PendingAutofix(
        id=1, chat_id=100, user_id=user_id, media_type="movie",
        radarr_movie_id=555, sonarr_series_id=None, sonarr_episode_id=None,
        sonarr_season=None, expected_episode_ids=[], label="Test Movie",
        ticket_id=7, ticket_url="http://seerr.example/issues/7",
        started_at=now.strftime("%Y-%m-%d %H:%M:%S"),
        timeout_at=(now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))


def _rows(markup):
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


def _callbacks(markup):
    return [cb for row in _rows(markup) for _t, cb in row]


@pytest.fixture
def ctx(fresh_store):
    c = make_ctx(admin_id=ADMIN)
    c.bot_data["store"] = fresh_store
    c.bot_data["seerr"].get_issue.return_value = _ticket()
    c.bot_data["seerr"].get_media_title.return_value = ("Some Show", "2020")
    c.bot.send_message = AsyncMock(return_value=make_message(message_id=900))
    c.bot_data["radarr"].read_fingerprint = AsyncMock(return_value=FP())
    return c


def _offer_markup(ticket_id=7):
    from bot.ask_admin import ask_admin_button
    from telegram import InlineKeyboardMarkup
    return InlineKeyboardMarkup([[ask_admin_button(ticket_id)]])


async def _tap(ctx, user_id=REPORTER, markup=None):
    upd = make_update(callback_data="askadm:7", user_id=user_id)
    upd.callback_query.message.reply_markup = (
        _offer_markup() if markup is None else markup)
    await ask_admin(upd, ctx)
    return upd


async def test_a_tap_from_a_message_without_the_button_does_nothing(ctx):
    from telegram import InlineKeyboardMarkup
    upd = await _tap(ctx, markup=InlineKeyboardMarkup([]))
    ctx.bot.send_message.assert_not_awaited()
    # The claim was left free for the real Reporter.
    assert await ctx.bot_data["store"].claim_ticket_escalation(7)


# --- the offer ---------------------------------------------------------------


async def test_repeat_dm_to_a_reporter_carries_ask_the_admin():
    ctx = make_ctx(admin_id=ADMIN)
    ctx.bot.send_message = AsyncMock(return_value=make_message(message_id=1))
    await _notify_complete(ctx, _fix(), repeat=FP(release_name="A"))
    markup = ctx.bot.send_message.call_args.kwargs["reply_markup"]
    assert _callbacks(markup)[-1] == "askadm:7"
    assert "tkfm:7" not in _callbacks(markup)


async def test_the_admins_own_repeat_dm_carries_mark_failed_instead():
    ctx = make_ctx(admin_id=ADMIN)
    ctx.bot.send_message = AsyncMock(return_value=make_message(message_id=1))
    await _notify_complete(ctx, _fix(user_id=ADMIN), repeat=FP(release_name="A"))
    callbacks = _callbacks(ctx.bot.send_message.call_args.kwargs["reply_markup"])
    assert callbacks[-1] == "tkfm:7"
    assert "askadm:7" not in callbacks


async def test_a_dm_without_a_repeat_has_neither_button():
    ctx = make_ctx(admin_id=ADMIN)
    ctx.bot.send_message = AsyncMock(return_value=make_message(message_id=1))
    await _notify_complete(ctx, _fix())
    callbacks = _callbacks(ctx.bot.send_message.call_args.kwargs["reply_markup"])
    assert "askadm:7" not in callbacks and "tkfm:7" not in callbacks


async def test_comment_confirmation_carries_the_button_for_a_reporter():
    ctx = make_ctx(admin_id=ADMIN, link=make_link(), user_data={"awaiting_comment_for": 7})
    upd = make_update(text="still broken", user_id=REPORTER)
    await resolve_comment(upd, ctx)
    call = upd.effective_message.reply_calls[0]
    assert "Added your comment" in call["text"]
    assert _callbacks(call["reply_markup"]) == ["askadm:7"]
    assert ctx.application.bot_data["btn_msgs"][REPORTER]


async def test_comment_confirmation_has_no_button_for_the_admin():
    ctx = make_ctx(admin_id=ADMIN, user_data={"awaiting_comment_for": 7})
    upd = make_update(text="still broken", user_id=ADMIN)
    await resolve_comment(upd, ctx)
    assert upd.effective_message.reply_calls[0]["reply_markup"] is None


# --- the ask -----------------------------------------------------------------


async def test_one_tap_sends_one_admin_dm_with_mark_failed_first(ctx):
    upd = await _tap(ctx)
    ctx.bot.send_message.assert_awaited_once()
    kwargs = ctx.bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == ADMIN
    rows = _rows(kwargs["reply_markup"])
    assert rows[0] == [("🚫 Mark Failed", "tkfm:7")]
    # the new-Ticket DM's buttons sit under it
    assert [cb for _t, cb in rows[1]] == ["tkr:7", "tkf:7", "tkc:7"]
    assert rows[2] == [("📜 History", "tkopen:7")]
    text = kwargs["text"]
    assert "Some Show (2020)" in text and "Season 2, Episode 3" in text
    assert "Reporter &lt;b&gt;" in text
    assert "http://seerr.example/issues/7" in text
    # the Reporter is told it was sent
    assert "Sent to the Admin" in upd.callback_query.message.reply_calls[0]["text"]


async def test_dm_names_the_release_that_came_back(ctx, fresh_store):
    await fresh_store.add_pending_autofix(
        chat_id=1, user_id=REPORTER, media_type="movie", label="L", ticket_id=7,
        ticket_url="u", radarr_movie_id=555)
    ctx.bot_data["radarr"].read_fingerprint = AsyncMock(return_value=FP(
        release_name="Show.S02E03.1080p-GRP", group="GRP", quality="WEBDL-1080p",
        size=2 * 1024 ** 3))
    await _tap(ctx)
    text = ctx.bot.send_message.call_args.kwargs["text"]
    assert "Show.S02E03.1080p-GRP (GRP, WEBDL-1080p, 2.00 GB)" in text


async def test_a_second_tap_sends_nothing_and_says_already_sent(ctx):
    await _tap(ctx)
    upd = await _tap(ctx)
    ctx.bot.send_message.assert_awaited_once()
    assert upd.callback_query.answers == [(ALREADY_SENT, False)]


async def test_the_ask_ignores_the_ticket_dm_class_switch(ctx):
    ctx.bot_data["settings_store"].settings.tg_notify_issues = False
    await _tap(ctx)
    ctx.bot.send_message.assert_awaited_once()


async def test_the_escalation_survives_a_restart(ctx, tmp_db_path, fresh_token_crypto):
    await _tap(ctx)
    restarted = make_ctx(admin_id=ADMIN)
    restarted.bot_data["store"] = UserStore(tmp_db_path, crypto=fresh_token_crypto)
    restarted.bot.send_message = AsyncMock()
    upd = await _tap(restarted)
    restarted.bot.send_message.assert_not_awaited()
    assert upd.callback_query.answers == [(ALREADY_SENT, False)]


async def test_a_failed_send_lets_the_reporter_try_again(ctx):
    ctx.bot.send_message = AsyncMock(side_effect=[RuntimeError("down"),
                                                  make_message(message_id=901)])
    first = await _tap(ctx)
    # Answered before the send; the failure arrives as a reply instead.
    assert first.callback_query.answers[0][1] is False
    reply = first.callback_query.message.reply_calls[-1]["text"]
    assert "Couldn't reach the Admin" in reply
    await _tap(ctx)
    assert ctx.bot.send_message.await_count == 2


async def test_mark_failed_on_that_dm_runs_with_its_own_progress_card(ctx):
    await _tap(ctx)
    kb = ctx.bot.send_message.call_args.kwargs["reply_markup"]
    ctx.bot_data["seerr"].get_issue.return_value = IssueListItem(
        id=7, issue_type=1, status=1, created_at="2026-11-14T00:00:00Z",
        tmdb_id=1, media_type="movie", problem_season=None,
        problem_episode=None, created_by="x")
    ctx.bot_data["radarr"].mark_failed.return_value = FixResult.success(
        "ok", steps_done=["blocklist", "delete", "search"],
        poll_info={"movie_id": 555})
    ctx.bot_data["store"].add_pending_autofix = AsyncMock(return_value=5)
    ctx.bot_data["store"].log_autofix = AsyncMock()
    ctx.bot_data["store"].set_autofix_message = AsyncMock()
    upd = make_update(callback_data="tkfm:7", user_id=ADMIN)
    upd.callback_query.message.reply_markup = kb
    ctx.bot.send_message = AsyncMock(return_value=make_message(message_id=950))
    await apply_fix(upd, ctx, strategy="mark_failed")
    ctx.bot_data["radarr"].mark_failed.assert_awaited_once()
    texts = [c.kwargs["text"] for c in ctx.bot.send_message.call_args_list]
    assert any("Mark Failed for #7" in t for t in texts)
    ctx.bot_data["store"].set_autofix_message.assert_awaited_once()


async def test_relink_resume_comment_confirmation_carries_the_button():
    ctx = make_ctx(admin_id=ADMIN, link=make_link())
    upd = make_update(text="", user_id=REPORTER)
    await _resume_resolve_comment(upd, ctx, {"ticket_id": 7, "comment": "c"})
    call = upd.effective_message.reply_calls[0]
    assert _callbacks(call["reply_markup"]) == ["askadm:7"]
