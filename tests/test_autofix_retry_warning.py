"""The Auto-fix offer warns when a Fix already ran on the same item in the
last 30 days, and puts Report to Admin first."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from bot.ticket_flow import ticket_description
from store import RecentFixEvent, UserStore
from tests._handler_harness import make_ctx, make_update


def _backdate_event(path: Path, days: float, *, event_id: int = 1) -> None:
    with sqlite3.connect(path) as c:
        c.execute(
            "UPDATE autofix_events SET occurred_at = "
            "datetime('now', ?) WHERE id = ?", (f"-{days} days", event_id))


# --- store lookup ------------------------------------------------------------


async def test_an_episode_event_matches_only_that_episode(
        fresh_store: UserStore, tmp_db_path: Path):
    await fresh_store.log_autofix(1, "tv", 10, season=2, episode=3, kind="redownload")
    _backdate_event(tmp_db_path, 3)
    found = await fresh_store.recent_fix_event("tv", 10, 2, 3)
    assert found == RecentFixEvent("redownload", "pending", found.age_seconds)
    assert 3 * 86400 - 5 <= found.age_seconds <= 3 * 86400 + 5
    assert await fresh_store.recent_fix_event("tv", 10, 2, 4) is None
    assert await fresh_store.recent_fix_event("tv", 10, 3, 3) is None


async def test_an_event_older_than_30_days_is_not_found(
        fresh_store: UserStore, tmp_db_path: Path):
    await fresh_store.log_autofix(1, "movie", 5)
    _backdate_event(tmp_db_path, 31)
    assert await fresh_store.recent_fix_event("movie", 5) is None


async def test_the_lookup_takes_no_user_and_reports_a_repeat(
        fresh_store: UserStore):
    event = await fresh_store.log_autofix(7, "movie", 5)  # logged by user 7; the lookup takes no user
    pid = await fresh_store.add_pending_autofix(
        chat_id=1, user_id=7, media_type="movie", label="M", ticket_id=1,
        ticket_url="u", radarr_movie_id=9, event_id=event)
    await fresh_store.mark_autofix_repeat(pid, refunded=True)
    found = await fresh_store.recent_fix_event("movie", 5)
    assert (found.kind, found.outcome) == ("auto-fix", "repeat")


async def test_the_newest_event_wins(fresh_store: UserStore):
    await fresh_store.log_autofix(1, "movie", 5, kind="auto-fix")
    await fresh_store.log_autofix(2, "movie", 5, kind="mark failed")
    assert (await fresh_store.recent_fix_event("movie", 5)).kind == "mark failed"


# --- the offer -----------------------------------------------------------------


def _ctx(event, *, media=None, episode=None, season=None):
    ctx = make_ctx(user_data={
        "issue_type": 1,
        "media": media or {"type": "tv", "tmdb_id": 10, "tvdb_id": 4},
        "season": season, "episode": episode,
    }, bot_data_overrides={"autofix_allow_all": True})
    ctx.bot_data["settings_store"].settings.daily_autofix_unlimited = True
    ctx.bot_data["store"].recent_fix_event.return_value = event
    return ctx


def _buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


async def _run(event, **kw):
    ctx = _ctx(event, **kw)
    update = make_update(text="audio drifts", user_id=42)
    await ticket_description(update, ctx)
    return ctx, update.effective_message.reply_calls[-1]


async def test_a_recent_event_warns_names_kind_and_age_and_reorders_buttons():
    ctx, reply = await _run(RecentFixEvent("redownload", "complete", 3 * 86400),
                            season=1, episode=2)
    text = reply["text"]
    assert "same file" in text and "ran on this title" in text
    assert "Redownload" in text and "3 days ago" in text
    assert "repeat" not in text
    labels = [b[0] for b in _buttons(reply["reply_markup"])]
    assert labels == ["📨 Report to Admin", "Try auto-fix anyway"]
    data = [b[1] for b in _buttons(reply["reply_markup"])]
    assert data[0].endswith(":no") and data[1].endswith(":yes")
    ctx.bot_data["store"].recent_fix_event.assert_awaited_once_with("tv", 10, 1, 2)


async def test_a_previous_repeat_is_mentioned():
    _, reply = await _run(RecentFixEvent("auto-fix", "repeat", 2 * 3600),
                          media={"type": "movie", "tmdb_id": 5})
    assert "came back as a repeat" in reply["text"]
    assert "2 hours ago" in reply["text"]


async def test_no_recent_event_keeps_todays_order_with_the_new_name():
    _, reply = await _run(None, media={"type": "movie", "tmdb_id": 5})
    assert "same file" not in reply["text"]
    labels = [b[0] for b in _buttons(reply["reply_markup"])]
    assert labels == ["✅ Try auto-fix", "📨 Report to Admin"]


async def test_offer_stays_plain_for_another_episode_or_an_old_event(
        fresh_store: UserStore, tmp_db_path: Path):
    await fresh_store.log_autofix(1, "tv", 10, season=1, episode=2)
    await fresh_store.log_autofix(1, "tv", 10, season=1, episode=3)
    _backdate_event(tmp_db_path, 31, event_id=2)
    for episode in (4, 3):
        ctx = _ctx(None, season=1, episode=episode)
        ctx.bot_data["store"].recent_fix_event = fresh_store.recent_fix_event
        update = make_update(text="audio drifts", user_id=42)
        await ticket_description(update, ctx)
        reply = update.effective_message.reply_calls[-1]
        assert "same file" not in reply["text"]
        assert [b[0] for b in _buttons(reply["reply_markup"])] == [
            "✅ Try auto-fix", "📨 Report to Admin"]
