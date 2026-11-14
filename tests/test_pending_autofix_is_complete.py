"""Tests for PendingAutofix.is_complete polymorphic dispatch."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from store import PendingAutofix


def _make_fix(
    media_type: str = "movie",
    radarr_movie_id=None,
    sonarr_series_id=None,
    sonarr_episode_id=None,
    sonarr_season=None,
    expected_episode_ids=None,
) -> PendingAutofix:
    return PendingAutofix(
        id=1, chat_id=100, user_id=42,
        media_type=media_type,
        radarr_movie_id=radarr_movie_id,
        sonarr_series_id=sonarr_series_id,
        sonarr_episode_id=sonarr_episode_id,
        sonarr_season=sonarr_season,
        expected_episode_ids=expected_episode_ids or [],
        label="Label",
        ticket_id=1, ticket_url="http://x/issues/1",
        started_at="2026-05-30 00:00:00",
        timeout_at="2026-05-30 06:00:00",
    )


# --- movies ---


async def test_movie_complete():
    fix = _make_fix(media_type="movie", radarr_movie_id=555)
    radarr = SimpleNamespace(movie_has_file=AsyncMock(return_value=True))
    done, extra = await fix.is_complete(radarr, None)
    assert done is True
    assert extra == ""


async def test_movie_pending():
    fix = _make_fix(media_type="movie", radarr_movie_id=555)
    radarr = SimpleNamespace(movie_has_file=AsyncMock(return_value=False))
    done, extra = await fix.is_complete(radarr, None)
    assert done is False
    assert extra == ""


async def test_movie_without_radarr_returns_false():
    fix = _make_fix(media_type="movie", radarr_movie_id=555)
    done, extra = await fix.is_complete(None, None)
    assert done is False
    assert extra == ""


async def test_movie_without_id_returns_false():
    fix = _make_fix(media_type="movie", radarr_movie_id=None)
    radarr = SimpleNamespace(movie_has_file=AsyncMock(return_value=True))
    done, extra = await fix.is_complete(radarr, None)
    assert done is False


# --- single episode ---


async def test_episode_complete():
    fix = _make_fix(media_type="tv", sonarr_episode_id=999)
    sonarr = SimpleNamespace(episode_has_file=AsyncMock(return_value=True))
    done, extra = await fix.is_complete(None, sonarr)
    assert done is True
    assert extra == ""


async def test_episode_pending():
    fix = _make_fix(media_type="tv", sonarr_episode_id=999)
    sonarr = SimpleNamespace(episode_has_file=AsyncMock(return_value=False))
    done, extra = await fix.is_complete(None, sonarr)
    assert done is False


# --- whole-season branch removed in 0.12.0 (dead code) ---


# --- error propagation ---


async def test_movie_error_propagates():
    """is_complete doesn't swallow exceptions -- the poller handles them."""
    fix = _make_fix(media_type="movie", radarr_movie_id=555)
    radarr = SimpleNamespace(movie_has_file=AsyncMock(side_effect=RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        await fix.is_complete(radarr, None)


# --- multi-episode file: every episode the file held ---


async def test_episode_incomplete_until_every_sibling_has_a_file():
    fix = _make_fix(media_type="tv", sonarr_episode_id=1,
                    expected_episode_ids=[2])
    has_file = {1: True, 2: False}
    sonarr = SimpleNamespace(
        episode_has_file=AsyncMock(side_effect=lambda i: has_file[i]))
    assert (await fix.is_complete(None, sonarr))[0] is False
    has_file[2] = True
    assert (await fix.is_complete(None, sonarr))[0] is True


async def test_old_row_without_sibling_ids_still_completes():
    fix = _make_fix(media_type="tv", sonarr_episode_id=999,
                    expected_episode_ids=[])
    sonarr = SimpleNamespace(episode_has_file=AsyncMock(return_value=True))
    assert (await fix.is_complete(None, sonarr))[0] is True
    sonarr.episode_has_file.assert_awaited_once_with(999)
