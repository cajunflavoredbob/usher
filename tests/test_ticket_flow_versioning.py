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
    ctx = _ctx(search_version=4, media={"title": "Newer"})
    # The old menu was built at version 4; /issue restarts and bumps to 5.
    await ticket_start(make_update(text="/issue", user_id=USER_ID), ctx)
    assert ctx.user_data["search_version"] == 5
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
