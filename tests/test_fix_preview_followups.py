"""Both Fix paths keep every episode the file held, and the preview menus
(Admin Fix menu, Reporter confirm) show a checking state first, escape the
episode line for Markdown and say "couldn't check" when they can't tell."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import ticket_flow
from bot.callback_prefixes import TICKET_AUTOFIX_OFFER
from bot.ticket_flow import ticket_offer_autofix
from bot.tickets import _FixContext, describe_fix_target, enqueue_fix_completion, tk_fix
from fix_result import FixResult
from seerr import CreatedIssue, IssueListItem
from tests._handler_harness import make_ctx, make_link, make_update


USER = 42
ADMIN = 999
POLL_INFO = {"series_id": 8, "episode_id": 501, "episode_ids": [501, 502]}
TV = {"type": "tv", "tmdb_id": 7, "tvdb_id": 42, "title": "Show", "year": "2026",
      "seerr_media_id": 10}


# --- both enqueue paths keep every episode the file held -----------------------


async def test_enqueue_fix_completion_keeps_the_episode_ids():
    store = SimpleNamespace(add_pending_autofix=AsyncMock(return_value=3))
    fix = _FixContext(ticket_id=7, media={"type": "tv", "tmdb_id": 7},
                      season=1, episode=1, label="Show S01E01")
    result = FixResult.success("ok", steps_done=["delete", "search"],
                               poll_info=POLL_INFO)
    await enqueue_fix_completion(store, fix=fix, result=result, chat_id=1,
                                 user_id=2, ticket_url="http://x/issues/7")
    kwargs = store.add_pending_autofix.call_args.kwargs
    assert kwargs["sonarr_episode_id"] == 501
    assert kwargs["expected_episode_ids"] == [501, 502]


async def test_reporter_submit_keeps_the_episode_ids():
    ctx = make_ctx(admin_id=ADMIN, link=make_link(telegram_id=USER),
                   user_data={"media": dict(TV), "issue_type": 1, "season": 1,
                              "episode": 1, "description": "no sound"})
    ctx.bot_data["seerr"].create_issue = AsyncMock(
        return_value=CreatedIssue(id=77, url="http://seerr.example/issues/77"))
    ctx.bot_data["allowlist"] = {USER}
    ctx.bot_data["settings_store"].settings.daily_autofix_unlimited = True
    result = FixResult.success("ok", steps_done=["delete", "search"],
                               poll_info=POLL_INFO)
    upd = make_update(callback_data="x", user_id=USER)
    with patch.object(ticket_flow, "run_arr_action", AsyncMock(return_value=result)):
        await ticket_flow._submit_ticket_inner(upd, ctx, autofix=True)
    kwargs = ctx.bot_data["store"].add_pending_autofix.call_args.kwargs
    assert kwargs["sonarr_episode_id"] == 501
    assert kwargs["expected_episode_ids"] == [501, 502]


# --- the confirm preview is escaped for Markdown --------------------------------


async def test_confirm_preview_escapes_markdown_in_the_episode_title():
    ctx = make_ctx(admin_id=ADMIN, link=make_link(telegram_id=USER),
                   user_data={"search_version": 1, "media": dict(TV),
                              "season": 1, "episode": 1})
    ctx.bot_data["sonarr"].describe_episode = AsyncMock(
        return_value='Sonarr: S01E01 "Under_score *star*"')
    upd = make_update(callback_data=f"{TICKET_AUTOFIX_OFFER}:1:yes", user_id=USER)
    await ticket_offer_autofix(upd, ctx)
    edit = upd.callback_query.edits[-1]
    assert "Under\\_score \\*star\\*" in edit["text"]
    assert edit["parse_mode"] == "Markdown"


async def test_confirm_edits_before_the_sonarr_read():
    ctx = make_ctx(admin_id=ADMIN, link=make_link(telegram_id=USER),
                   user_data={"search_version": 1, "media": dict(TV),
                              "season": 1, "episode": 1})
    upd = make_update(callback_data=f"{TICKET_AUTOFIX_OFFER}:1:yes", user_id=USER)
    seen = []

    async def describe(*_a):
        seen.append(len(upd.callback_query.edits))
        return ""
    ctx.bot_data["sonarr"].describe_episode = describe
    await ticket_offer_autofix(upd, ctx)
    assert seen == [1]
    assert "Checking" in upd.callback_query.edits[0]["text"]


# --- the Admin Fix menu ------------------------------------------------------------


def _admin_ctx(**ticket):
    ctx = make_ctx(admin_id=ADMIN)
    fields = dict(id=5, issue_type=1, status=1, created_at="2026-05-29T12:00:00Z",
                  tmdb_id=7, media_type="tv", problem_season=1, problem_episode=3,
                  created_by="someone")
    fields.update(ticket)
    ctx.bot_data["seerr"].get_issue.return_value = IssueListItem(**fields)
    ctx.bot_data["seerr"].get_tv_seasons.return_value = ([], 42)
    return ctx


async def test_fix_menu_shows_a_checking_state_before_the_reads():
    ctx = _admin_ctx()
    upd = make_update(callback_data="tkf:5", user_id=ADMIN)
    seen = []

    async def describe(*_a):
        seen.append(len(upd.callback_query.edits))
        return 'Sonarr: S01E03 "Third"'
    ctx.bot_data["sonarr"].describe_episode = describe
    await tk_fix(upd, ctx)
    assert seen == [1]
    assert "checking" in upd.callback_query.edits[0]["text"]
    assert 'Sonarr: S01E03 "Third"' in upd.callback_query.edits[-1]["text"]


async def test_fix_menu_says_couldnt_check_when_the_ticket_read_fails():
    ctx = _admin_ctx()
    ctx.bot_data["seerr"].get_issue.side_effect = RuntimeError("down")
    upd = make_update(callback_data="tkf:5", user_id=ADMIN)
    await tk_fix(upd, ctx)
    assert "couldn't check which episode" in upd.callback_query.edits[-1]["text"]


async def test_fix_menu_says_couldnt_check_when_the_tvdb_id_is_missing():
    ctx = _admin_ctx()
    ctx.bot_data["seerr"].get_tv_seasons.return_value = ([], None)
    upd = make_update(callback_data="tkf:5", user_id=ADMIN)
    await tk_fix(upd, ctx)
    assert "couldn't check which episode" in upd.callback_query.edits[-1]["text"]
    assert "couldn't check" in await describe_fix_target(
        {"type": "tv"}, 1, 3, object())


async def test_fix_menu_for_a_tv_episode_skips_the_title_read():
    ctx = _admin_ctx()
    upd = make_update(callback_data="tkf:5", user_id=ADMIN)
    await tk_fix(upd, ctx)
    ctx.bot_data["seerr"].get_media_title.assert_not_called()


async def test_fix_menu_for_a_movie_names_the_movie():
    ctx = _admin_ctx(media_type="movie", problem_season=None, problem_episode=None)
    ctx.bot_data["seerr"].get_media_title.return_value = ("Alien", "1979")
    upd = make_update(callback_data="tkf:5", user_id=ADMIN)
    await tk_fix(upd, ctx)
    ctx.bot_data["seerr"].get_tv_seasons.assert_not_called()
    text = upd.callback_query.edits[-1]["text"]
    assert "Radarr: Alien (1979)" in text
    assert "couldn't check" not in text


async def test_fix_menu_for_a_movie_still_opens_when_the_title_read_fails():
    ctx = _admin_ctx(media_type="movie", problem_season=None, problem_episode=None)
    ctx.bot_data["seerr"].get_media_title.side_effect = RuntimeError("down")
    upd = make_update(callback_data="tkf:5", user_id=ADMIN)
    await tk_fix(upd, ctx)
    text = upd.callback_query.edits[-1]["text"]
    assert "how?" in text
    assert "Radarr: couldn't check which movie this is." in text


async def test_fix_menu_for_a_movie_names_it_when_sonarr_is_not_configured():
    ctx = _admin_ctx(media_type="movie", problem_season=None, problem_episode=None)
    ctx.bot_data["sonarr"] = None
    ctx.bot_data["seerr"].get_media_title.return_value = ("Alien", "1979")
    upd = make_update(callback_data="tkf:5", user_id=ADMIN)
    await tk_fix(upd, ctx)
    text = upd.callback_query.edits[-1]["text"]
    assert "Radarr: Alien (1979)" in text
    assert "how?" in text


async def test_fix_menu_without_sonarr_says_the_movie_could_not_be_checked_when_the_ticket_read_fails():
    ctx = _admin_ctx(media_type="movie", problem_season=None, problem_episode=None)
    ctx.bot_data["sonarr"] = None
    ctx.bot_data["seerr"].get_issue.side_effect = RuntimeError("down")
    upd = make_update(callback_data="tkf:5", user_id=ADMIN)
    await tk_fix(upd, ctx)
    text = upd.callback_query.edits[-1]["text"]
    assert "Radarr: couldn't check which movie this is." in text
    assert "episode" not in text


async def test_fix_menu_for_a_whole_season_ticket_shows_the_refusal_only():
    ctx = _admin_ctx(problem_episode=None)
    upd = make_update(callback_data="tkf:5", user_id=ADMIN)
    await tk_fix(upd, ctx)
    edit = upd.callback_query.edits[-1]
    assert "only works on individual episodes" in edit["text"]
    callbacks = [b.callback_data for row in edit["reply_markup"].inline_keyboard
                 for b in row]
    assert len(callbacks) == 1 and callbacks[0].endswith(":5")
    assert not any(c.startswith(("tkfd", "tkfm")) for c in callbacks)


async def test_fix_refusal_records_its_message_so_the_gated_back_works():
    import pytest
    from telegram.ext import ApplicationHandlerStop
    from bot.shared import global_btn_gate, record_btn
    ctx = _admin_ctx(problem_episode=None)
    # A newer button message exists, so the older Ticket DM (id 100) is not
    # the latest one the gate knows about.
    record_btn(ctx.application, ADMIN,
               SimpleNamespace(message_id=55, chat_id=100))
    upd = make_update(callback_data="tkf:5", user_id=ADMIN)
    await tk_fix(upd, ctx)
    back = [b.callback_data
            for row in upd.callback_query.edits[-1]["reply_markup"].inline_keyboard
            for b in row][0]
    tap = make_update(callback_data=back, user_id=ADMIN)
    tap.callback_query.message = upd.callback_query.message
    ctx.user_data = {}
    try:
        await global_btn_gate(tap, ctx)
    except ApplicationHandlerStop:
        pytest.fail("the gate refused the Back on the Fix refusal")
