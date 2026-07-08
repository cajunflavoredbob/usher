"""Tests for the affected season/episode helpers (v0.11.24).

Seerr webhooks deliver the affected season/episode in the top-level `extra`
array ('Affected Season' / 'Affected Episode'), not as issue.problemSeason/
problemEpisode (those are REST API fields). extract_affected_se reads both;
format_scope_label renders the human-readable scope line.
"""
from __future__ import annotations

import pytest

from bot.shared import extract_affected_se, format_scope_label


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
