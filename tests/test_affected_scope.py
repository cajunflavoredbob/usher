"""Tests for the affected season/episode helpers (v0.11.24).

Seerr webhooks deliver the affected season/episode in the top-level `extra`
array ('Affected Season' / 'Affected Episode'), not as issue.problemSeason/
problemEpisode (those are REST API fields). extract_affected_se reads both;
format_scope_label renders the human-readable scope line.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.shared import extract_affected_se, followup_scope_label, format_scope_label


# --- extract_affected_se ----------------------------------------------------


def test_extract_from_extra_array():
    payload = {
        "issue": {},
        "media": {"media_type": "tv"},
        "extra": [
            {"name": "Affected Season", "value": "2"},
            {"name": "Affected Episode", "value": "7"},
        ],
    }
    assert extract_affected_se(payload) == (2, 7)


def test_extract_season_only_from_extra():
    payload = {"issue": {}, "media": {"media_type": "tv"},
               "extra": [{"name": "Affected Season", "value": "3"}]}
    assert extract_affected_se(payload) == (3, None)


def test_extract_falls_back_to_problem_fields():
    # Custom webhook template that sets problemSeason/problemEpisode directly.
    payload = {"issue": {"problemSeason": 1, "problemEpisode": 4}, "media": {"media_type": "tv"}}
    assert extract_affected_se(payload) == (1, 4)


def test_extract_none_when_absent():
    payload = {"issue": {}, "media": {"media_type": "movie"}}
    assert extract_affected_se(payload) == (None, None)


def test_extract_tolerates_garbage_values():
    payload = {"issue": {}, "media": {"media_type": "tv"},
               "extra": [{"name": "Affected Season", "value": "not-a-number"}]}
    assert extract_affected_se(payload) == (None, None)


# --- format_scope_label -----------------------------------------------------


@pytest.mark.parametrize("season,episode,expected", [
    (1, 5, "Season 1, Episode 5"),
    (2, None, "Season 2"),
    (None, None, "All seasons"),
    (0, None, "All seasons"),   # Seerr uses season 0 for whole-series issues
])
def test_scope_label_tv(season, episode, expected):
    assert format_scope_label("tv", season, episode) == expected


def test_scope_label_movie_is_empty():
    assert format_scope_label("movie", None, None) == ""
    assert format_scope_label("movie", 1, 5) == ""   # movies have no S/E


def test_scope_label_unknown_media_empty():
    assert format_scope_label("", 1, 5) == ""


# --- followup_scope_label ---------------------------------------------------
# Follow-up webhooks (ISSUE_COMMENT / ISSUE_RESOLVED) carry no affected
# season/episode, so absence means "unknown", not "whole series". Regression
# for the live bug: a comment DM on a single-episode ticket said "All seasons".


def _seerr(*, season=None, episode=None, fail=False):
    issue = SimpleNamespace(problem_season=season, problem_episode=episode)
    get_issue = AsyncMock(side_effect=RuntimeError("seerr down")) if fail \
        else AsyncMock(return_value=issue)
    return SimpleNamespace(get_issue=get_issue)


async def test_followup_prefers_payload_and_skips_lookup():
    """When the payload does carry a scope, use it and don't call the API."""
    seerr = _seerr(season=9, episode=9)
    payload = {"issue": {}, "extra": [
        {"name": "Affected Season", "value": "1"},
        {"name": "Affected Episode", "value": "5"},
    ]}
    assert await followup_scope_label(seerr, payload, 42, "tv") == "Season 1, Episode 5"
    seerr.get_issue.assert_not_called()


async def test_followup_falls_back_to_api_for_real_scope():
    """The reported bug: empty comment payload must resolve to the ticket's
    real scope (S6E9), never 'All seasons'."""
    seerr = _seerr(season=6, episode=9)
    label = await followup_scope_label(seerr, {"issue": {}}, 47, "tv")
    assert label == "Season 6, Episode 9"
    assert "All seasons" not in label
    seerr.get_issue.assert_awaited_once_with(47)


async def test_followup_unknown_renders_nothing_not_all_seasons():
    """No payload scope and the API has none either -> render no line at all
    rather than claiming a wider scope than the ticket."""
    seerr = _seerr(season=None, episode=None)
    assert await followup_scope_label(seerr, {"issue": {}}, 42, "tv") == ""


async def test_followup_api_failure_renders_nothing():
    seerr = _seerr(fail=True)
    assert await followup_scope_label(seerr, {"issue": {}}, 42, "tv") == ""


async def test_followup_no_seerr_client_renders_nothing():
    assert await followup_scope_label(None, {"issue": {}}, 42, "tv") == ""


async def test_followup_season_zero_is_all_seasons():
    """A genuine whole-series ticket (season 0) still reads 'All seasons'."""
    seerr = _seerr(season=0, episode=None)
    assert await followup_scope_label(seerr, {"issue": {}}, 42, "tv") == "All seasons"


async def test_followup_whole_season_ticket():
    seerr = _seerr(season=6, episode=None)
    assert await followup_scope_label(seerr, {"issue": {}}, 42, "tv") == "Season 6"


async def test_followup_movie_skips_lookup_entirely():
    seerr = _seerr(season=6, episode=9)
    assert await followup_scope_label(seerr, {"issue": {}}, 42, "movie") == ""
    seerr.get_issue.assert_not_called()
