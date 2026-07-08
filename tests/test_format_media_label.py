"""Tests for bot.shared.format_media_label: the canonical title rendering."""
from __future__ import annotations

import pytest

from bot.shared import format_media_label


def test_title_only():
    assert format_media_label("Inception", "") == "Inception"


def test_title_and_year():
    assert format_media_label("Inception", "2010") == "Inception (2010)"


def test_title_year_season():
    assert format_media_label("Mating Season", "2026", season=1) == "Mating Season (2026) — S01"


def test_title_year_season_episode():
    assert format_media_label("Mating Season", "2026", season=1, episode=8) == \
        "Mating Season (2026) — S01E08"


def test_title_only_season_no_year():
    """Year-less but seasoned: still produces the canonical layout."""
    assert format_media_label("Foo", "", season=2) == "Foo — S02"


def test_empty_title_falls_back():
    assert format_media_label("", "2020") == "(unknown) (2020)"


def test_zero_pads_two_digit_season_and_episode():
    assert format_media_label("X", "", season=12, episode=34) == "X — S12E34"


def test_season_zero_renders_specials_style():
    # season 0 is falsy in Python, so the helper SKIPS adding the suffix.
    # The Specials season is handled separately at the picker level; here
    # documenting the behavior so future readers know it isn't a bug.
    assert format_media_label("Foo", "", season=0, episode=1) == "Foo"


@pytest.mark.parametrize("season,episode,expected_suffix", [
    (1, None, " — S01"),
    (1, 0, " — S01"),  # episode 0 is falsy: same as no episode
    (1, 1, " — S01E01"),
])
def test_episode_falsy_handling(season, episode, expected_suffix):
    out = format_media_label("Foo", "", season=season, episode=episode)
    assert out.endswith(expected_suffix)
