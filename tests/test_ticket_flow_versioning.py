"""Every flow-scoped button of the Report a Ticket flow carries the search
version and is refused when it differs; the counter survives a flow end."""
from __future__ import annotations

import pytest

from bot.ticket_flow import (
    _clear_issue_state,
    _show_search_results,
    _show_season_picker,
    _show_type_picker,
    _ticket_timeout,
    ticket_cancel,
    ticket_confirm_autofix,
    ticket_episode_page,
    ticket_offer_autofix,
    ticket_pick_episode,
    ticket_pick_season,
    ticket_pick_type,
    ticket_research_parent,
    ticket_start,
)
from seerr import MediaResult, TvSeason
from tests._handler_harness import make_ctx, make_link, make_update

USER_ID = 42
STALE_TEXT = "Search context changed"


def _ctx(**user_data):
    return make_ctx(admin_id=999, link=make_link(telegram_id=USER_ID),
                    user_data=dict(user_data))


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _assert_refused(upd, ctx, state):
    assert state is None
    assert STALE_TEXT in upd.callback_query.edits[0]["text"]
    # The newer report's draft is untouched.
    assert "issue_type" not in ctx.user_data


async def test_stale_cancel_after_restarted_issue_is_refused():
    ctx = _ctx(search_version=4)
    # The old menu was built at version 4; /issue restarts and bumps to 5.
    await ticket_start(make_update(text="/issue", user_id=USER_ID), ctx)
    assert ctx.user_data["search_version"] == 5
    ctx.user_data["media"] = {"title": "Newer"}
    upd = make_update(callback_data="cancel:4", user_id=USER_ID)
    state = await ticket_cancel(upd, ctx)
    assert state is None
    assert STALE_TEXT in upd.callback_query.edits[0]["text"]
    assert ctx.user_data["media"] == {"title": "Newer"}


async def test_current_cancel_still_cancels():
    ctx = _ctx(search_version=5, media={"title": "X"})
    upd = make_update(callback_data="cancel:5", user_id=USER_ID)
    state = await ticket_cancel(upd, ctx)
    assert state == -1  # ConversationHandler.END
    assert upd.callback_query.edits[0]["text"].startswith("Cancelled.")
    assert "media" not in ctx.user_data


async def test_typed_cancel_without_button_still_cancels():
    ctx = _ctx(search_version=5, media={"title": "X"})
    upd = make_update(text="/cancel", user_id=USER_ID)
    state = await ticket_cancel(upd, ctx)
    assert state == -1
    assert "media" not in ctx.user_data


async def test_stale_season_tap_is_refused():
    ctx = _ctx(search_version=6,
               seasons={1: TvSeason(season_number=1, episode_count=5)})
    upd = make_update(callback_data="season:5:1", user_id=USER_ID)
    state = await ticket_pick_season(upd, ctx)
    _assert_refused(upd, ctx, state)
    assert "season" not in ctx.user_data


async def test_current_season_tap_advances_to_episode_page():
    ctx = _ctx(search_version=6,
               seasons={1: TvSeason(season_number=1, episode_count=5)})
    upd = make_update(callback_data="season:6:1", user_id=USER_ID)
    state = await ticket_pick_season(upd, ctx)
    assert state is not None and state != -1
    assert ctx.user_data["season"] == 1
    markup = upd.callback_query.edits[0]["reply_markup"]
    cbs = _callbacks(markup)
    assert "ep:6:1" in cbs and "ep:6:0" in cbs and "cancel:6" in cbs


async def test_stale_issue_type_tap_is_refused():
    ctx = _ctx(search_version=3)
    upd = make_update(callback_data="type:2:1", user_id=USER_ID)
    state = await ticket_pick_type(upd, ctx)
    _assert_refused(upd, ctx, state)


async def test_current_issue_type_tap_asks_for_description():
    ctx = _ctx(search_version=3)
    upd = make_update(callback_data="type:3:1", user_id=USER_ID)
    state = await ticket_pick_type(upd, ctx)
    assert state is not None and state != -1
    assert ctx.user_data["issue_type"] == 1


async def test_stale_episode_and_page_taps_are_refused():
    ctx = _ctx(search_version=3, season=1,
               seasons={1: TvSeason(season_number=1, episode_count=5)})
    for fn, data in ((ticket_pick_episode, "ep:2:3"),
                     (ticket_episode_page, "ep_page:2:1")):
        upd = make_update(callback_data=data, user_id=USER_ID)
        state = await fn(upd, ctx)
        _assert_refused(upd, ctx, state)
        assert "episode" not in ctx.user_data


async def test_stale_research_parent_is_refused():
    ctx = _ctx(search_version=3, research_parent="Show")
    upd = make_update(callback_data="research_parent:2", user_id=USER_ID)
    state = await ticket_research_parent(upd, ctx)
    assert state is None
    assert STALE_TEXT in upd.callback_query.edits[0]["text"]
    ctx.bot_data["seerr"].search.assert_not_called()


@pytest.mark.parametrize("fn,data", [
    (ticket_offer_autofix, "autofix:2:yes"),
    (ticket_confirm_autofix, "confirm:2:yes"),
    (ticket_confirm_autofix, "confirm:2:no"),
])
async def test_stale_autofix_taps_are_refused(fn, data):
    ctx = _ctx(search_version=3)
    upd = make_update(callback_data=data, user_id=USER_ID)
    state = await fn(upd, ctx)
    _assert_refused(upd, ctx, state)
    assert "submitting_ticket" not in ctx.user_data


async def test_versionless_old_format_tap_is_refused():
    ctx = _ctx(search_version=3)
    upd = make_update(callback_data="type:1", user_id=USER_ID)
    state = await ticket_pick_type(upd, ctx)
    _assert_refused(upd, ctx, state)


async def test_search_keyboard_carries_version_on_cancel_and_parent():
    ctx = _ctx(search_version=7)
    ctx.bot_data["seerr"].search.return_value = [
        MediaResult("movie", 1, "Show - Special", "2020", None)]
    upd = make_update(text="Show - Special", user_id=USER_ID)
    await _show_search_results(upd.effective_message.reply_text, ctx,
                               "Show - Special", user_id=USER_ID)
    markup = upd.effective_message.reply_calls[0]["reply_markup"]
    cbs = _callbacks(markup)
    assert "media:8:movie:1" in cbs
    assert "research_parent:8" in cbs
    assert "cancel:8" in cbs


async def test_season_and_type_keyboards_carry_version():
    ctx = _ctx(search_version=9,
               media={"type": "tv", "tmdb_id": 1, "title": "T", "year": ""})
    ctx.bot_data["seerr"].get_tv_seasons.return_value = (
        [TvSeason(season_number=1, episode_count=3)], 5)
    upd = make_update(callback_data="media:9:tv:1", user_id=USER_ID)
    await _show_season_picker(upd, ctx)
    cbs = _callbacks(upd.callback_query.edits[0]["reply_markup"])
    assert "season:9:1" in cbs and "cancel:9" in cbs

    upd2 = make_update(callback_data="season:9:1", user_id=USER_ID)
    ctx.user_data["season"] = 1
    ctx.user_data["episode"] = None
    await _show_type_picker(upd2, ctx)
    cbs2 = _callbacks(upd2.callback_query.edits[0]["reply_markup"])
    assert "type:9:1" in cbs2 and "cancel:9" in cbs2


async def test_version_counter_survives_clear_and_timeout():
    ctx = _ctx(search_version=11, media={"title": "X"}, description="d")
    _clear_issue_state(ctx)
    assert ctx.user_data["search_version"] == 11
    assert "media" not in ctx.user_data
    ctx.user_data["media"] = {"title": "Y"}
    await _ticket_timeout(make_update(text="x", user_id=USER_ID), ctx)
    assert ctx.user_data["search_version"] == 11
    assert "media" not in ctx.user_data


def test_error_cleanup_keeps_version_counter():
    from bot.app import _CONVERSATION_USER_DATA_KEYS
    assert "search_version" not in _CONVERSATION_USER_DATA_KEYS


# --- A new report never inherits an old draft ------------------------------

_DRAFT = dict(media={"title": "Old"}, search_results={"version": 1},
              seasons={}, season=3, episode=5, issue_type=1,
              description="old", research_parent="Old Show",
              rq_jump_media={"tmdb_id": 9}, _pending_autofix_id=7,
              hub_origin="tickets")


def _assert_draft_gone(ctx):
    for key in _DRAFT:
        assert key not in ctx.user_data, key


async def test_ticket_start_clears_an_old_draft():
    ctx = _ctx(search_version=2, **_DRAFT)
    await ticket_start(make_update(text="/issue", user_id=USER_ID), ctx)
    _assert_draft_gone(ctx)
    assert ctx.user_data["search_version"] == 3


async def test_hub_report_clears_an_old_draft():
    from bot.callback_prefixes import TICKETS_REPORT
    from bot.ticket_flow import ticket_hub_report
    ctx = _ctx(search_version=2, **_DRAFT)
    ctx.application.bot_data["flow_convs"] = []
    await ticket_hub_report(
        make_update(callback_data=TICKETS_REPORT, user_id=USER_ID), ctx)
    for key in _DRAFT:
        if key != "hub_origin":
            assert key not in ctx.user_data, key
    assert ctx.user_data["search_version"] == 3


async def test_timeout_clears_every_draft_key():
    ctx = _ctx(search_version=2, **_DRAFT, submitting_ticket=True)
    await _ticket_timeout(None, ctx)
    _assert_draft_gone(ctx)
    assert "submitting_ticket" not in ctx.user_data
    assert ctx.user_data["search_version"] == 2


async def test_force_end_clears_the_ended_issue_draft():
    from types import SimpleNamespace
    from telegram.ext import CommandHandler, ConversationHandler
    from bot.shared import force_end_flow_conversations

    async def _noop(update, ctx):
        return None

    conv = ConversationHandler(
        entry_points=[CommandHandler("x", _noop)],
        states={1: [CommandHandler("y", _noop)]}, fallbacks=[],
        per_user=True, per_chat=True, name="issue")
    key = (100, USER_ID)
    conv._conversations[key] = 1
    ctx = _ctx(search_version=2, **_DRAFT)
    ctx.application.bot_data["flow_convs"] = [conv]
    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=100),
                          effective_user=SimpleNamespace(id=USER_ID),
                          effective_message=None, callback_query=None)
    ended = force_end_flow_conversations(ctx, upd)
    assert ended == [conv]
    for gone in ("media", "season", "episode", "research_parent",
                 "rq_jump_media", "description"):
        assert gone not in ctx.user_data, gone
    # The Hub origin goes to the next flow; the in-flight submit pops its own
    # Auto-fix Progress card id when it finishes.
    assert ctx.user_data["_pending_autofix_id"] == 7
    assert ctx.user_data["hub_origin"] == "tickets"
    assert ctx.user_data["search_version"] == 2


async def test_force_end_leaves_the_draft_when_no_issue_flow_is_live():
    from bot.shared import force_end_flow_conversations
    ctx = _ctx(**_DRAFT)
    ctx.application.bot_data["flow_convs"] = []
    force_end_flow_conversations(ctx, make_update(text="/x", user_id=USER_ID))
    assert ctx.user_data["media"] == {"title": "Old"}


async def test_new_issue_keeps_the_interrupted_submit_resume_marker():
    """A new /issue must not silently drop the earlier report's relink
    resume: the marker stays so the resume can say it was not submitted."""
    from bot.shared import run_relink_resume
    import time
    marker = {"kind": "submit_ticket", "payload": {"autofix": False},
              "saved_at": time.time()}
    ctx = _ctx(search_version=2, relink_resume=dict(marker), **_DRAFT)
    await ticket_start(make_update(text="/issue", user_id=USER_ID), ctx)
    assert ctx.user_data["relink_resume"]["kind"] == "submit_ticket"
    # The re-link finishes while the new /issue is open: the user is told.
    class _Conv:
        _conversations = {(100, USER_ID): 1}
    ctx.bot_data["conversations"] = {"issue": _Conv()}
    upd = make_update(text="x", user_id=USER_ID)
    assert await run_relink_resume(upd, ctx) is True
    texts = [c["text"] for c in upd.effective_message.reply_calls]
    assert any("didn't send the earlier report" in t for t in texts)
    assert not any("Picking up" in t for t in texts)
