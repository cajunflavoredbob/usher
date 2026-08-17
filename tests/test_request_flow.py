"""Handler tests for the /request conversation: entry gates, availability
annotations in search, movie dead-end preemption, the season multi-select,
submit outcome wording, and the double-tap guard."""
from __future__ import annotations

from telegram.ext import ConversationHandler

from seerr import (
    MEDIA_STATUS_AVAILABLE,
    MEDIA_STATUS_PENDING,
    REQUEST_STATUS_APPROVED,
    REQUEST_STATUS_PENDING,
    CreatedRequest,
    DuplicateRequestError,
    MediaResult,
    NothingToRequestError,
    Quota,
    QuotaBucket,
    QuotaExceededError,
    SeasonAvailability,
)
from bot.request_flow import (
    request_pick_media,
    request_seasons_done,
    request_select_all,
    request_start,
    request_submit,
    request_title,
    request_toggle_season,
    _summarize_seasons,
)
from bot.shared import RQ_CONFIRM, RQ_PICK_MEDIA, RQ_PICK_SEASONS, RQ_TITLE

from tests._handler_harness import make_ctx, make_mapping, make_update

USER_ID = 42
ADMIN_ID = 999


def _linked_ctx(**kwargs):
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=make_mapping(telegram_id=USER_ID),
                   **kwargs)
    ctx.args = []
    return ctx


def _quota(remaining=4, limit=10):
    bucket = QuotaBucket(days=7, limit=limit, used=limit - remaining,
                         remaining=remaining, restricted=remaining == 0)
    return Quota(movie=bucket, tv=bucket)


# --- entry -------------------------------------------------------------------

async def test_start_unlinked_user_is_gated():
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=None)
    ctx.args = []
    upd = make_update(text="/request", user_id=USER_ID)
    state = await request_start(upd, ctx)
    assert state == ConversationHandler.END
    assert "link" in upd.effective_message.reply_calls[0]["text"]


async def test_start_prompts_for_title():
    ctx = _linked_ctx()
    upd = make_update(text="/request", user_id=USER_ID)
    state = await request_start(upd, ctx)
    assert state == RQ_TITLE
    assert "What movie or show" in upd.effective_message.reply_calls[0]["text"]


async def test_start_with_args_searches_immediately():
    ctx = _linked_ctx()
    ctx.args = ["dune", "part", "two"]
    ctx.bot_data["seerr"].search.return_value = [
        MediaResult("movie", 693134, "Dune: Part Two", "2024", 9,
                    status=MEDIA_STATUS_AVAILABLE),
    ]
    upd = make_update(text="/request dune part two", user_id=USER_ID)
    state = await request_start(upd, ctx)
    assert state == RQ_PICK_MEDIA
    ctx.bot_data["seerr"].search.assert_awaited_once()
    assert ctx.bot_data["seerr"].search.call_args.args[0] == "dune part two"


# --- search rendering --------------------------------------------------------

async def test_search_list_annotates_availability():
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].search.return_value = [
        MediaResult("movie", 1, "Available Movie", "2020", 5,
                    status=MEDIA_STATUS_AVAILABLE),
        MediaResult("tv", 2, "Fresh Show", "2026", None, status=None),
    ]
    upd = make_update(text="available movie", user_id=USER_ID)
    state = await request_title(upd, ctx)
    assert state == RQ_PICK_MEDIA
    text = upd.effective_message.reply_calls[0]["text"]
    assert "✅ available" in text
    assert "Fresh Show (2026)" in text
    assert "Fresh Show (2026) —" not in text  # no annotation when unknown


async def test_pick_available_movie_toasts_and_stays():
    ctx = _linked_ctx(user_data={
        "rq_search_results": {
            "version": 1,
            "by_key": {("movie", 1): MediaResult(
                "movie", 1, "Available Movie", "2020", 5,
                status=MEDIA_STATUS_AVAILABLE)},
        },
        "rq_search_version": 1,
    })
    upd = make_update(callback_data="rqm:1:movie:1", user_id=USER_ID)
    state = await request_pick_media(upd, ctx)
    assert state == RQ_PICK_MEDIA
    # Second answer() is the alert toast; no edit happened (keyboard stays).
    assert any("available" in a[0].lower() for a in upd.callback_query.answers)
    assert upd.callback_query.edits == []


async def test_pick_pending_movie_toasts_and_stays():
    ctx = _linked_ctx(user_data={
        "rq_search_results": {
            "version": 1,
            "by_key": {("movie", 1): MediaResult(
                "movie", 1, "On Its Way", "2020", 5,
                status=MEDIA_STATUS_PENDING)},
        },
        "rq_search_version": 1,
    })
    upd = make_update(callback_data="rqm:1:movie:1", user_id=USER_ID)
    state = await request_pick_media(upd, ctx)
    assert state == RQ_PICK_MEDIA
    assert any("requested" in a[0].lower() for a in upd.callback_query.answers)


async def test_pick_requestable_movie_shows_confirm_with_quota():
    ctx = _linked_ctx(user_data={
        "rq_search_results": {
            "version": 1,
            "by_key": {("movie", 1): MediaResult(
                "movie", 1, "New Movie", "2026", None, status=None)},
        },
        "rq_search_version": 1,
    })
    ctx.bot_data["seerr"].get_quota.return_value = _quota(remaining=4, limit=10)
    upd = make_update(callback_data="rqm:1:movie:1", user_id=USER_ID)
    state = await request_pick_media(upd, ctx)
    assert state == RQ_CONFIRM
    text = upd.callback_query.edits[0]["text"]
    assert "Request <b>New Movie (2026)</b>?" in text
    assert "4 of 10 requests" in text


async def test_stale_search_version_toasts_and_leaves_flow_alone():
    """A pick on a superseded list must not end or mutate the active flow."""
    ctx = _linked_ctx(user_data={
        "rq_search_results": {"version": 2, "by_key": {}},
        "rq_search_version": 2,
    })
    upd = make_update(callback_data="rqm:1:movie:1", user_id=USER_ID)
    state = await request_pick_media(upd, ctx)
    assert state == RQ_PICK_MEDIA
    assert upd.callback_query.edits == []
    assert any(alert for _, alert in upd.callback_query.answers)


# --- season multi-select -----------------------------------------------------

def _tv_pick_ctx():
    ctx = _linked_ctx(user_data={
        "rq_search_results": {
            "version": 1,
            "by_key": {("tv", 9): MediaResult("tv", 9, "Show", "2024", None,
                                              status=None)},
        },
        "rq_search_version": 1,
    })
    ctx.bot_data["seerr"].get_tv_season_availability.return_value = [
        SeasonAvailability(1, False),
        SeasonAvailability(2, True),
        SeasonAvailability(3, True),
    ]
    return ctx


async def test_tv_pick_shows_season_picker():
    ctx = _tv_pick_ctx()
    upd = make_update(callback_data="rqm:1:tv:9", user_id=USER_ID)
    state = await request_pick_media(upd, ctx)
    assert state == RQ_PICK_SEASONS
    assert "Which seasons?" in upd.callback_query.edits[0]["text"]
    assert ctx.user_data["rq_selected"] == set()


async def test_tv_all_seasons_covered_replies_and_keeps_list():
    """A fully-covered show replies as a NEW message and stays in the pick
    state, so the list survives -- symmetric with the movie toasts (the old
    behavior edited the list away and ended the conversation)."""
    ctx = _tv_pick_ctx()
    ctx.bot_data["seerr"].get_tv_season_availability.return_value = [
        SeasonAvailability(1, False),
    ]
    upd = make_update(callback_data="rqm:1:tv:9", user_id=USER_ID)
    state = await request_pick_media(upd, ctx)
    assert state == RQ_PICK_MEDIA
    assert upd.callback_query.edits == []  # the pick list is untouched
    reply = upd.callback_query.message.reply_calls[0]["text"]
    assert "already available or requested" in reply


async def test_toggle_and_done():
    ctx = _linked_ctx(user_data={
        "rq_media": {"type": "tv", "tmdb_id": 9, "title": "Show", "year": "2024"},
        "rq_seasons": [SeasonAvailability(2, True),
                       SeasonAvailability(3, True)],
        "rq_selected": set(),
        "rq_search_version": 1,
    })
    upd = make_update(callback_data="rqs:1:2", user_id=USER_ID)
    state = await request_toggle_season(upd, ctx)
    assert state == RQ_PICK_SEASONS
    assert ctx.user_data["rq_selected"] == {2}
    assert upd.callback_query.markup_edits  # keyboard re-rendered

    done_upd = make_update(callback_data="rqdone:1", user_id=USER_ID)
    state = await request_seasons_done(done_upd, ctx)
    assert state == RQ_CONFIRM
    assert "S2" in done_upd.callback_query.edits[0]["text"]


async def test_done_with_empty_selection_alerts():
    ctx = _linked_ctx(user_data={
        "rq_media": {"type": "tv", "tmdb_id": 9, "title": "Show", "year": ""},
        "rq_seasons": [SeasonAvailability(2, True)],
        "rq_selected": set(),
        "rq_search_version": 1,
    })
    upd = make_update(callback_data="rqdone:1", user_id=USER_ID)
    state = await request_seasons_done(upd, ctx)
    assert state == RQ_PICK_SEASONS
    assert any(alert for _, alert in upd.callback_query.answers)


async def test_select_all_selects_only_requestable():
    ctx = _linked_ctx(user_data={
        "rq_media": {"type": "tv", "tmdb_id": 9, "title": "Show", "year": ""},
        "rq_seasons": [SeasonAvailability(1, False),
                       SeasonAvailability(2, True),
                       SeasonAvailability(3, True)],
        "rq_selected": set(),
        "rq_search_version": 1,
    })
    upd = make_update(callback_data="rqall:1", user_id=USER_ID)
    state = await request_select_all(upd, ctx)
    assert state == RQ_PICK_SEASONS
    assert ctx.user_data["rq_selected"] == {2, 3}


def test_summarize_seasons_ranges():
    assert _summarize_seasons([1, 2, 3, 5]) == "S1-S3, S5"
    assert _summarize_seasons([4]) == "S4"
    assert _summarize_seasons([2, 1]) == "S1-S2"


# --- submit ------------------------------------------------------------------

def _confirm_ctx(media=None, selected=None):
    user_data = {"rq_media": media or {"type": "movie", "tmdb_id": 1,
                                       "title": "New Movie", "year": "2026"},
                 "rq_search_version": 1}
    if selected is not None:
        user_data["rq_selected"] = selected
    return _linked_ctx(user_data=user_data)


async def test_submit_approved_wording_and_user_attribution():
    ctx = _confirm_ctx()
    ctx.bot_data["seerr"].create_request.return_value = CreatedRequest(
        id=5, status=REQUEST_STATUS_APPROVED)
    upd = make_update(callback_data="rqgo:1", user_id=USER_ID)
    state = await request_submit(upd, ctx)
    assert state == ConversationHandler.END
    kwargs = ctx.bot_data["seerr"].create_request.call_args.kwargs
    assert kwargs["as_plex_token"] == "plex-abc"
    assert kwargs["media_type"] == "movie"
    assert kwargs["seasons"] is None
    assert "Approved" in upd.callback_query.edits[-1]["text"]


async def test_submit_pending_wording():
    ctx = _confirm_ctx()
    ctx.bot_data["seerr"].create_request.return_value = CreatedRequest(
        id=5, status=REQUEST_STATUS_PENDING)
    upd = make_update(callback_data="rqgo:1", user_id=USER_ID)
    await request_submit(upd, ctx)
    text = upd.callback_query.edits[-1]["text"]
    assert "Waiting for approval" in text


async def test_submit_tv_sends_sorted_seasons():
    ctx = _confirm_ctx(
        media={"type": "tv", "tmdb_id": 9, "title": "Show", "year": "2024"},
        selected={3, 2},
    )
    ctx.bot_data["seerr"].create_request.return_value = CreatedRequest(
        id=6, status=REQUEST_STATUS_PENDING)
    upd = make_update(callback_data="rqgo:1", user_id=USER_ID)
    await request_submit(upd, ctx)
    assert ctx.bot_data["seerr"].create_request.call_args.kwargs["seasons"] == [2, 3]


async def test_submit_quota_exceeded_message():
    ctx = _confirm_ctx()
    ctx.bot_data["seerr"].create_request.side_effect = QuotaExceededError(
        "Movie Quota exceeded.")
    upd = make_update(callback_data="rqgo:1", user_id=USER_ID)
    state = await request_submit(upd, ctx)
    assert state == ConversationHandler.END
    assert "Movie Quota exceeded." in upd.callback_query.edits[-1]["text"]


async def test_submit_duplicate_message():
    ctx = _confirm_ctx()
    ctx.bot_data["seerr"].create_request.side_effect = DuplicateRequestError(
        "Request for this media already exists.")
    upd = make_update(callback_data="rqgo:1", user_id=USER_ID)
    await request_submit(upd, ctx)
    assert "already requested" in upd.callback_query.edits[-1]["text"]


async def test_submit_nothing_to_request_message():
    ctx = _confirm_ctx(
        media={"type": "tv", "tmdb_id": 9, "title": "Show", "year": ""},
        selected={1},
    )
    ctx.bot_data["seerr"].create_request.side_effect = NothingToRequestError()
    upd = make_update(callback_data="rqgo:1", user_id=USER_ID)
    await request_submit(upd, ctx)
    assert "already available" in upd.callback_query.edits[-1]["text"]


async def test_submit_double_tap_guard():
    ctx = _confirm_ctx()
    ctx.user_data["rq_submitting"] = True
    upd = make_update(callback_data="rqgo:1", user_id=USER_ID)
    state = await request_submit(upd, ctx)
    assert state == ConversationHandler.END
    ctx.bot_data["seerr"].create_request.assert_not_awaited()


# --- pagination ---------------------------------------------------------------

def _many_results(n=12):
    return [MediaResult("movie", 100 + i, f"Movie {i + 1}", "2020", None,
                        status=None) for i in range(n)]


async def test_search_over_five_results_paginates():
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].search.return_value = _many_results(12)
    upd = make_update(text="movie", user_id=USER_ID)
    state = await request_title(upd, ctx)
    assert state == RQ_PICK_MEDIA
    call = upd.effective_message.reply_calls[0]
    text = call["text"]
    assert "1. " in text and "5. " in text and "6. " not in text
    callbacks = [b.callback_data for row in call["reply_markup"].inline_keyboard
                 for b in row]
    assert "rqpg:1:1" in callbacks
    assert not any(cb == "rqpg:1:0" for cb in callbacks)  # no back on page 1
    # Fetch asked for the full server page, not 5.
    assert ctx.bot_data["seerr"].search.call_args.kwargs["limit"] > 5


async def test_page_two_renders_next_slice_with_global_numbers():
    from bot.request_flow import request_page
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].search.return_value = _many_results(12)
    upd = make_update(text="movie", user_id=USER_ID)
    await request_title(upd, ctx)
    page_upd = make_update(callback_data="rqpg:1:1", user_id=USER_ID)
    state = await request_page(page_upd, ctx)
    assert state == RQ_PICK_MEDIA
    text = page_upd.callback_query.edits[0]["text"]
    assert "6. " in text and "10. " in text and "11. " not in text
    callbacks = [b.callback_data
                 for row in page_upd.callback_query.edits[0]["reply_markup"].inline_keyboard
                 for b in row]
    assert "rqpg:1:0" in callbacks and "rqpg:1:2" in callbacks


async def test_five_or_fewer_results_have_no_nav():
    ctx = _linked_ctx()
    ctx.bot_data["seerr"].search.return_value = _many_results(3)
    upd = make_update(text="movie", user_id=USER_ID)
    await request_title(upd, ctx)
    call = upd.effective_message.reply_calls[0]
    callbacks = [b.callback_data for row in call["reply_markup"].inline_keyboard
                 for b in row]
    assert not any(cb.startswith("rqpg:") for cb in callbacks)


# --- detail card --------------------------------------------------------------

def _info_ctx(poster="/abc.jpg"):
    r = MediaResult("movie", 550, "Fight Club", "1999", 9,
                    status=MEDIA_STATUS_AVAILABLE, overview="An insomniac.",
                    poster_path=poster, vote_average=8.4)
    return _linked_ctx(user_data={
        "rq_search_results": {"version": 1, "by_key": {("movie", 550): r}},
        "rq_search_version": 1,
    })


async def test_info_card_sends_poster_photo():
    from bot.request_flow import request_info
    ctx = _info_ctx()
    upd = make_update(callback_data="rqi:1:movie:550", user_id=USER_ID)
    state = await request_info(upd, ctx)
    assert state == RQ_PICK_MEDIA
    kwargs = ctx.bot.send_photo.call_args.kwargs
    assert kwargs["photo"].endswith("/abc.jpg")
    assert "Fight Club" in kwargs["caption"]
    assert "★ 8.4" in kwargs["caption"]
    assert "An insomniac." in kwargs["caption"]
    ctx.bot.send_message.assert_not_awaited()


async def test_info_card_without_poster_falls_back_to_text():
    from bot.request_flow import request_info
    ctx = _info_ctx(poster="")
    upd = make_update(callback_data="rqi:1:movie:550", user_id=USER_ID)
    await request_info(upd, ctx)
    ctx.bot.send_photo.assert_not_awaited()
    assert "Fight Club" in ctx.bot.send_message.call_args.kwargs["text"]


async def test_info_card_poster_failure_falls_back_to_text():
    from bot.request_flow import request_info
    ctx = _info_ctx()
    ctx.bot.send_photo.side_effect = RuntimeError("bad url")
    upd = make_update(callback_data="rqi:1:movie:550", user_id=USER_ID)
    await request_info(upd, ctx)
    assert "Fight Club" in ctx.bot.send_message.call_args.kwargs["text"]


async def test_info_card_stale_version_alerts():
    from bot.request_flow import request_info
    ctx = _info_ctx()
    upd = make_update(callback_data="rqi:9:movie:550", user_id=USER_ID)
    await request_info(upd, ctx)
    ctx.bot.send_photo.assert_not_awaited()
    ctx.bot.send_message.assert_not_awaited()
    assert any(alert for _, alert in upd.callback_query.answers)


def _card_markup():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "🗑 Dismiss", callback_data="rqx")]])


async def test_info_dismiss_deletes_card():
    from bot.request_flow import request_info_dismiss
    ctx = _linked_ctx()
    upd = make_update(callback_data="rqx", user_id=USER_ID)
    upd.callback_query.message.reply_markup = _card_markup()
    await request_info_dismiss(upd, ctx)
    upd.callback_query.message.delete.assert_awaited_once()


async def test_info_dismiss_refuses_non_card_message():
    """rqx is gate-exempt, so a forged callback could reference ANY bot
    message; only messages actually carrying the Dismiss button are
    deleted."""
    from bot.request_flow import request_info_dismiss
    ctx = _linked_ctx()
    upd = make_update(callback_data="rqx", user_id=USER_ID)
    # no reply_markup on the message -> not a detail card
    await request_info_dismiss(upd, ctx)
    upd.callback_query.message.delete.assert_not_awaited()


# --- 4K ----------------------------------------------------------------------

def _pickable_movie_ctx(*, perms, status=None, status_4k=None):
    r = MediaResult("movie", 1, "New Movie", "2026", None,
                    status=status, status_4k=status_4k)
    return _linked_ctx(user_data={
        "rq_search_results": {"version": 1, "by_key": {("movie", 1): r}},
        "rq_search_version": 1,
        "rq_perms": perms,
    })


async def test_confirm_offers_4k_button_when_permitted():
    from seerr import PERMISSION_REQUEST_4K
    ctx = _pickable_movie_ctx(perms=PERMISSION_REQUEST_4K)
    upd = make_update(callback_data="rqm:1:movie:1", user_id=USER_ID)
    state = await request_pick_media(upd, ctx)
    assert state == RQ_CONFIRM
    callbacks = [b.callback_data
                 for row in upd.callback_query.edits[0]["reply_markup"].inline_keyboard
                 for b in row]
    assert "rqgo:1" in callbacks and "rqgo4k:1" in callbacks


async def test_confirm_hides_4k_button_without_permission():
    ctx = _pickable_movie_ctx(perms=0)
    upd = make_update(callback_data="rqm:1:movie:1", user_id=USER_ID)
    await request_pick_media(upd, ctx)
    callbacks = [b.callback_data
                 for row in upd.callback_query.edits[0]["reply_markup"].inline_keyboard
                 for b in row]
    assert "rqgo:1" in callbacks and "rqgo4k:1" not in callbacks


async def test_available_movie_passes_through_to_4k_only_confirm():
    from seerr import PERMISSION_REQUEST_4K
    ctx = _pickable_movie_ctx(perms=PERMISSION_REQUEST_4K,
                              status=MEDIA_STATUS_AVAILABLE)
    upd = make_update(callback_data="rqm:1:movie:1", user_id=USER_ID)
    state = await request_pick_media(upd, ctx)
    assert state == RQ_CONFIRM
    edit = upd.callback_query.edits[0]
    assert "standard quality" in edit["text"]
    callbacks = [b.callback_data
                 for row in edit["reply_markup"].inline_keyboard for b in row]
    assert "rqgo4k:1" in callbacks and "rqgo:1" not in callbacks


async def test_available_movie_with_4k_also_covered_toasts():
    from seerr import PERMISSION_REQUEST_4K
    ctx = _pickable_movie_ctx(perms=PERMISSION_REQUEST_4K,
                              status=MEDIA_STATUS_AVAILABLE,
                              status_4k=MEDIA_STATUS_AVAILABLE)
    upd = make_update(callback_data="rqm:1:movie:1", user_id=USER_ID)
    state = await request_pick_media(upd, ctx)
    assert state == RQ_PICK_MEDIA
    assert any("including 4K" in a[0] for a in upd.callback_query.answers)


async def test_available_movie_without_4k_permission_still_toasts():
    ctx = _pickable_movie_ctx(perms=0, status=MEDIA_STATUS_AVAILABLE)
    upd = make_update(callback_data="rqm:1:movie:1", user_id=USER_ID)
    state = await request_pick_media(upd, ctx)
    assert state == RQ_PICK_MEDIA
    assert any("available" in a[0].lower() for a in upd.callback_query.answers)


async def test_submit_4k_passes_flag_and_labels():
    ctx = _confirm_ctx()
    ctx.bot_data["seerr"].create_request.return_value = CreatedRequest(
        id=5, status=REQUEST_STATUS_APPROVED)
    upd = make_update(callback_data="rqgo4k:1", user_id=USER_ID)
    await request_submit(upd, ctx)
    assert ctx.bot_data["seerr"].create_request.call_args.kwargs["is4k"] is True
    assert "[4K]" in upd.callback_query.edits[-1]["text"]


async def test_entry_caches_permissions():
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=make_mapping(telegram_id=USER_ID))
    ctx.args = []
    ctx.bot_data["seerr"].get_my_permissions.return_value = 1026
    upd = make_update(text="/request", user_id=USER_ID)
    await request_start(upd, ctx)
    assert ctx.user_data["rq_perms"] == 1026
    kwargs = ctx.bot_data["seerr"].get_my_permissions.call_args.kwargs
    assert kwargs["as_plex_token"] == "plex-abc"


# --- rqfi entry (jump from /issue dead-end) -----------------------------------

async def test_request_from_issue_uses_stashed_title():
    from bot.request_flow import request_from_issue
    ctx = _linked_ctx(user_data={
        "rq_jump_media": {"tmdb_id": 550, "title": "Stashed Movie",
                          "year": "2020"},
    })
    ctx.bot_data["conversations"] = {}
    ctx.bot_data["flow_convs"] = []
    upd = make_update(callback_data="rqfi:movie:550", user_id=USER_ID)
    state = await request_from_issue(upd, ctx)
    assert state == RQ_CONFIRM
    assert "Stashed Movie (2020)" in upd.callback_query.edits[0]["text"]
    # Stash consumed, no TMDb re-fetch needed.
    ctx.bot_data["seerr"].get_media_title.assert_not_awaited()


async def test_request_from_issue_refetches_on_stash_mismatch():
    from bot.request_flow import request_from_issue
    ctx = _linked_ctx(user_data={
        "rq_jump_media": {"tmdb_id": 999, "title": "Wrong Stash", "year": ""},
    })
    ctx.bot_data["conversations"] = {}
    ctx.bot_data["flow_convs"] = []
    ctx.bot_data["seerr"].get_media_title.return_value = ("Fetched Title", "2021")
    upd = make_update(callback_data="rqfi:movie:550", user_id=USER_ID)
    state = await request_from_issue(upd, ctx)
    assert state == RQ_CONFIRM
    assert "Fetched Title (2021)" in upd.callback_query.edits[0]["text"]


async def test_request_from_issue_gates_unlinked_user():
    from bot.request_flow import request_from_issue
    ctx = make_ctx(admin_id=ADMIN_ID, mapping=None)
    ctx.args = []
    ctx.bot_data["conversations"] = {}
    ctx.bot_data["flow_convs"] = []
    upd = make_update(callback_data="rqfi:movie:550", user_id=USER_ID)
    state = await request_from_issue(upd, ctx)
    assert state == ConversationHandler.END
    assert "link" in upd.effective_message.reply_calls[0]["text"]
    ctx.bot_data["seerr"].create_request.assert_not_awaited()


# --- stale page taps ----------------------------------------------------------

async def test_page_tap_on_superseded_search_toasts():
    from bot.request_flow import request_page
    ctx = _linked_ctx(user_data={"rq_search_version": 2, "rq_results": [
        MediaResult("movie", 1, "M", "2020", None)]})
    upd = make_update(callback_data="rqpg:1:1", user_id=USER_ID)
    state = await request_page(upd, ctx)
    assert state == RQ_PICK_MEDIA
    assert upd.callback_query.edits == []
    assert any(alert for _, alert in upd.callback_query.answers)


# --- relink resume ------------------------------------------------------------

async def test_resume_submit_request_resubmits_at_chosen_tier():
    from bot.request_flow import _resume_submit_request
    ctx = _linked_ctx(user_data={
        "rq_media": {"type": "movie", "tmdb_id": 550, "title": "Movie",
                     "year": "2020"},
        "rq_is4k": True,
    })
    ctx.bot_data["conversations"] = {}
    ctx.bot_data["seerr"].create_request.return_value = CreatedRequest(
        id=9, status=REQUEST_STATUS_APPROVED)
    upd = make_update(text="resumed", user_id=USER_ID)
    await _resume_submit_request(upd, ctx, {})
    kwargs = ctx.bot_data["seerr"].create_request.call_args.kwargs
    assert kwargs["is4k"] is True
    assert kwargs["as_plex_token"] == "plex-abc"
    assert "[4K]" in upd.effective_message.reply_calls[0]["text"]


async def test_resume_submit_request_without_draft_says_so():
    from bot.request_flow import _resume_submit_request
    ctx = _linked_ctx()
    ctx.bot_data["conversations"] = {}
    upd = make_update(text="resumed", user_id=USER_ID)
    await _resume_submit_request(upd, ctx, {})
    assert "draft is gone" in upd.effective_message.reply_calls[0]["text"]
    ctx.bot_data["seerr"].create_request.assert_not_awaited()


# --- 4K wording, blocklist, and quota-edge pins --------------------------------

async def test_pending_movie_passes_through_with_requested_wording():
    """force-4K wording must say 'requested', not 'available', when the
    standard track is merely pending/processing."""
    from seerr import PERMISSION_REQUEST_4K
    ctx = _pickable_movie_ctx(perms=PERMISSION_REQUEST_4K,
                              status=MEDIA_STATUS_PENDING)
    upd = make_update(callback_data="rqm:1:movie:1", user_id=USER_ID)
    state = await request_pick_media(upd, ctx)
    assert state == RQ_CONFIRM
    text = upd.callback_query.edits[0]["text"]
    assert "Already requested in standard quality" in text
    assert "Already available" not in text


async def test_blocklisted_movie_toasts():
    from seerr import MEDIA_STATUS_BLOCKLISTED
    ctx = _pickable_movie_ctx(perms=0, status=MEDIA_STATUS_BLOCKLISTED)
    upd = make_update(callback_data="rqm:1:movie:1", user_id=USER_ID)
    state = await request_pick_media(upd, ctx)
    assert state == RQ_PICK_MEDIA
    assert any("requestable" in a[0] for a in upd.callback_query.answers)


async def test_blocklisted_tv_toasts_instead_of_season_picker():
    from seerr import MEDIA_STATUS_BLOCKLISTED
    r = MediaResult("tv", 9, "Blocked Show", "2024", None,
                    status=MEDIA_STATUS_BLOCKLISTED)
    ctx = _linked_ctx(user_data={
        "rq_search_results": {"version": 1, "by_key": {("tv", 9): r}},
        "rq_search_version": 1,
    })
    upd = make_update(callback_data="rqm:1:tv:9", user_id=USER_ID)
    state = await request_pick_media(upd, ctx)
    assert state == RQ_PICK_MEDIA
    ctx.bot_data["seerr"].get_tv_season_availability.assert_not_awaited()
    assert any("requestable" in a[0] for a in upd.callback_query.answers)


async def test_exhausted_quota_renders_used_up_warning():
    ctx = _linked_ctx(user_data={
        "rq_search_results": {
            "version": 1,
            "by_key": {("movie", 1): MediaResult("movie", 1, "New Movie",
                                                 "2026", None, status=None)},
        },
        "rq_search_version": 1,
    })
    ctx.bot_data["seerr"].get_quota.return_value = _quota(remaining=0, limit=10)
    upd = make_update(callback_data="rqm:1:movie:1", user_id=USER_ID)
    await request_pick_media(upd, ctx)
    text = upd.callback_query.edits[0]["text"]
    assert "is used up" in text
    assert "0 of 10" not in text


async def test_from_issue_jump_bumps_version():
    """Two consecutive from-issue jumps must not share a version, or the
    first jump's still-gated buttons could act on the second flow."""
    from bot.request_flow import request_from_issue
    ctx = _linked_ctx(user_data={"rq_search_version": 3})
    ctx.bot_data["conversations"] = {}
    ctx.bot_data["flow_convs"] = []
    upd = make_update(callback_data="rqfi:movie:550", user_id=USER_ID)
    await request_from_issue(upd, ctx)
    assert ctx.user_data["rq_search_version"] == 4


def test_error_cleanup_preserves_version_counter():
    """on_error's cleanup keys must never include rq_search_version (the
    guard against cross-flow callback collisions depends on it)."""
    from bot.app import _CONVERSATION_USER_DATA_KEYS
    assert "rq_search_version" not in _CONVERSATION_USER_DATA_KEYS



# --- stale-cancel, dismiss-hardening, and jump-marker pins ---------------------

async def test_stale_cancel_toasts_and_keeps_active_flow():
    """A Cancel on a superseded message must not end the active flow (it was
    the one unversioned flow control)."""
    from bot.request_flow import request_cancel
    ctx = _linked_ctx(user_data={"rq_search_version": 2,
                                 "rq_media": {"type": "movie", "tmdb_id": 1,
                                              "title": "M", "year": ""}})
    upd = make_update(callback_data="rqcancel:1", user_id=USER_ID)
    state = await request_cancel(upd, ctx)
    assert state is None  # keep current conversation state
    assert upd.callback_query.edits == []
    assert any(alert for _, alert in upd.callback_query.answers)
    assert "rq_media" in ctx.user_data  # active draft untouched


async def test_current_cancel_still_cancels():
    from bot.request_flow import request_cancel
    ctx = _linked_ctx(user_data={"rq_search_version": 2, "rq_media": {}})
    upd = make_update(callback_data="rqcancel:2", user_id=USER_ID)
    state = await request_cancel(upd, ctx)
    assert state == ConversationHandler.END
    assert "Cancelled" in upd.callback_query.edits[0]["text"]
    assert "rq_media" not in ctx.user_data


async def test_from_issue_jump_drops_flow_markers():
    """force_end_flow_conversations owns the free-text markers now; the
    from-issue entry must clear link_active_loop so an in-flight Plex PIN
    poll can't outlive its force-ended conversation."""
    from bot.request_flow import request_from_issue
    ctx = _linked_ctx(user_data={
        "link_active_loop": object(),
        "rq_jump_media": {"tmdb_id": 550, "title": "T", "year": ""},
    })
    ctx.bot_data["conversations"] = {}
    ctx.bot_data["flow_convs"] = []
    upd = make_update(callback_data="rqfi:movie:550", user_id=USER_ID)
    await request_from_issue(upd, ctx)
    assert "link_active_loop" not in ctx.user_data


# --- stale-guard and degradation pins ------------------------------------------

async def test_stale_confirm_submit_toasts_and_keeps_flow():
    """The most destructive stale path: a confirm button from a superseded
    flow must never submit the ACTIVE flow's draft."""
    ctx = _confirm_ctx()  # rq_search_version = 1
    ctx.user_data["rq_search_version"] = 2
    upd = make_update(callback_data="rqgo:1", user_id=USER_ID)
    state = await request_submit(upd, ctx)
    assert state == RQ_CONFIRM
    ctx.bot_data["seerr"].create_request.assert_not_awaited()
    assert any(alert for _, alert in upd.callback_query.answers)


async def test_stale_season_toggle_toasts_without_mutating():
    ctx = _linked_ctx(user_data={
        "rq_search_version": 2,
        "rq_seasons": [SeasonAvailability(2, True)],
        "rq_selected": {2},
    })
    upd = make_update(callback_data="rqs:1:2", user_id=USER_ID)
    state = await request_toggle_season(upd, ctx)
    assert state == RQ_PICK_SEASONS
    assert ctx.user_data["rq_selected"] == {2}  # untouched
    assert upd.callback_query.markup_edits == []


async def test_stale_seasons_done_toasts():
    from bot.request_flow import request_seasons_done
    ctx = _linked_ctx(user_data={"rq_search_version": 2, "rq_selected": {1}})
    upd = make_update(callback_data="rqdone:1", user_id=USER_ID)
    state = await request_seasons_done(upd, ctx)
    assert state == RQ_PICK_SEASONS
    assert upd.callback_query.edits == []


async def test_stale_season_na_toasts_instead_of_wrong_show_narration():
    from bot.request_flow import request_season_na
    ctx = _linked_ctx(user_data={"rq_search_version": 2})
    upd = make_update(callback_data="rqna:1:3", user_id=USER_ID)
    await request_season_na(upd, ctx)
    text, alert = upd.callback_query.answers[0]
    assert alert  # stale toast, not "Season 3 is already available"
    assert "Season" not in text


async def test_quota_preview_failure_degrades_to_no_line():
    ctx = _linked_ctx(user_data={
        "rq_search_results": {
            "version": 1,
            "by_key": {("movie", 1): MediaResult("movie", 1, "New Movie",
                                                 "2026", None, status=None)},
        },
        "rq_search_version": 1,
    })
    ctx.bot_data["seerr"].get_quota.side_effect = RuntimeError("seerr down")
    upd = make_update(callback_data="rqm:1:movie:1", user_id=USER_ID)
    state = await request_pick_media(upd, ctx)
    assert state == RQ_CONFIRM
    text = upd.callback_query.edits[0]["text"]
    assert "Quota" not in text and "MagicMock" not in text


async def test_submit_reports_seasons_seerr_actually_granted():
    """Seerr silently drops seasons covered between picker render and
    submit; the confirmation words the outcome from the GRANTED set."""
    ctx = _confirm_ctx(
        media={"type": "tv", "tmdb_id": 9, "title": "Show", "year": ""},
        selected={1, 2},
    )
    ctx.bot_data["seerr"].create_request.return_value = CreatedRequest(
        id=6, status=REQUEST_STATUS_PENDING, seasons=[2])
    upd = make_update(callback_data="rqgo:1", user_id=USER_ID)
    await request_submit(upd, ctx)
    text = upd.callback_query.edits[-1]["text"]
    assert "S2" in text and "Show — S1-S2" not in text
    assert "skipped" in text
