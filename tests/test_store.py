"""Tests for store.py: UserStore lifecycle, decrypt-failed semantics, autofix tracking."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from store import Mapping, TokenCrypto, UserStore


async def test_init_creates_schema_and_wal(tmp_db_path: Path, fresh_token_crypto):
    UserStore(tmp_db_path, crypto=fresh_token_crypto)
    with sqlite3.connect(tmp_db_path) as c:
        mode = c.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


async def test_link_and_get_roundtrip(fresh_store: UserStore):
    await fresh_store.link_with_plex(
        telegram_id=42,
        seerr_id=7,
        seerr_display="Kenny",
        plex_token="plex-token-secret",
        plex_uuid="uuid-abc",
        plex_username="kenny",
    )
    m = await fresh_store.get(42)
    assert m is not None
    assert m.telegram_id == 42
    assert m.seerr_id == 7
    assert m.plex_token == "plex-token-secret"
    assert m.plex_username == "kenny"
    assert m.plex_token_decrypt_failed is False


async def test_get_missing_returns_none(fresh_store: UserStore):
    assert (await fresh_store.get(9999)) is None


async def test_find_by_plex_username_case_insensitive(fresh_store: UserStore):
    await fresh_store.link_with_plex(
        telegram_id=42, seerr_id=7, seerr_display="Kenny",
        plex_token="t", plex_uuid="u", plex_username="KennyPlex",
    )
    assert (await fresh_store.find_by_plex_username("kennyplex")) is not None
    assert (await fresh_store.find_by_plex_username("KENNYPLEX")) is not None
    assert (await fresh_store.find_by_plex_username("someone-else")) is None


async def test_find_by_plex_username_empty_returns_none(fresh_store: UserStore):
    assert (await fresh_store.find_by_plex_username("")) is None


async def test_unlink(fresh_store: UserStore):
    await fresh_store.link_with_plex(
        telegram_id=42, seerr_id=7, seerr_display="Kenny",
        plex_token="t", plex_uuid="u", plex_username="kenny",
    )
    assert await fresh_store.unlink(42) is True
    assert await fresh_store.unlink(42) is False
    assert (await fresh_store.get(42)) is None


# --- decrypt-failed handling ---


async def test_decrypt_failed_when_key_changes(tmp_db_path: Path, tmp_path: Path):
    # Link with key A.
    key_a = TokenCrypto(key_path=tmp_path / "key_a.bin")
    store_a = UserStore(tmp_db_path, crypto=key_a)
    await store_a.link_with_plex(
        telegram_id=42, seerr_id=7, seerr_display="Kenny",
        plex_token="t", plex_uuid="u", plex_username="kenny",
    )
    # Re-open with key B.
    key_b_path = tmp_path / "key_b.bin"
    key_b_path.write_bytes(Fernet.generate_key())
    key_b = TokenCrypto(key_path=key_b_path)
    store_b = UserStore(tmp_db_path, crypto=key_b)

    m = await store_b.get(42)
    assert m is not None
    assert m.plex_token is None
    assert m.plex_token_decrypt_failed is True


async def test_count_decrypt_failures_zero_when_healthy(fresh_store: UserStore):
    await fresh_store.link_with_plex(
        telegram_id=42, seerr_id=7, seerr_display="K",
        plex_token="t", plex_uuid="u", plex_username="k",
    )
    assert await fresh_store.count_decrypt_failures() == 0


async def test_count_decrypt_failures_after_key_change(tmp_db_path: Path, tmp_path: Path):
    key_a = TokenCrypto(key_path=tmp_path / "key_a.bin")
    store_a = UserStore(tmp_db_path, crypto=key_a)
    for tg_id in (1, 2, 3):
        await store_a.link_with_plex(
            telegram_id=tg_id, seerr_id=tg_id, seerr_display=f"u{tg_id}",
            plex_token=f"t{tg_id}", plex_uuid=f"u{tg_id}", plex_username=f"name{tg_id}",
        )
    key_b_path = tmp_path / "key_b.bin"
    key_b_path.write_bytes(Fernet.generate_key())
    store_b = UserStore(tmp_db_path, crypto=TokenCrypto(key_path=key_b_path))
    assert await store_b.count_decrypt_failures() == 3


# --- autofix rate-limiting ---


async def test_count_autofix_24h_starts_at_zero(fresh_store: UserStore):
    assert await fresh_store.count_autofix_24h(42) == 0


async def test_log_autofix_then_count(fresh_store: UserStore):
    await fresh_store.log_autofix(42, "movie", 555)
    await fresh_store.log_autofix(42, "tv", 777, season=1, episode=4)
    await fresh_store.log_autofix(99, "movie", 111)  # different user
    assert await fresh_store.count_autofix_24h(42) == 2
    assert await fresh_store.count_autofix_24h(99) == 1


# --- pending autofix lifecycle ---


async def test_pending_autofix_lifecycle(fresh_store: UserStore):
    pid = await fresh_store.add_pending_autofix(
        chat_id=100, user_id=42, media_type="movie",
        label="Movie (2026)", issue_id=1, issue_url="http://x/issues/1",
        radarr_movie_id=555,
    )
    assert isinstance(pid, int)

    pending = await fresh_store.list_pending_autofixes()
    assert len(pending) == 1
    assert pending[0].id == pid
    assert pending[0].media_type == "movie"
    assert pending[0].radarr_movie_id == 555
    assert pending[0].issue_id == 1

    await fresh_store.mark_autofix_status(pid, "complete")
    assert (await fresh_store.list_pending_autofixes()) == []  # only 'pending' rows listed


async def test_pending_autofix_whole_season_carries_episode_ids(fresh_store: UserStore):
    await fresh_store.add_pending_autofix(
        chat_id=100, user_id=42, media_type="tv",
        label="Show S01", issue_id=2, issue_url="http://x/issues/2",
        sonarr_series_id=10, sonarr_season=1,
        expected_episode_ids=[101, 102, 103],
    )
    pending = await fresh_store.list_pending_autofixes()
    assert pending[0].expected_episode_ids == [101, 102, 103]


# --- concurrency smoke ---


async def test_concurrent_writes_dont_raise(fresh_store: UserStore):
    """Smoke check that WAL + busy_timeout + retry handles overlap."""
    async def link_one(i: int):
        await fresh_store.link_with_plex(
            telegram_id=i, seerr_id=i, seerr_display=f"u{i}",
            plex_token=f"t{i}", plex_uuid=f"u{i}", plex_username=f"u{i}",
        )

    await asyncio.gather(*(link_one(i) for i in range(20)))
    # All should be retrievable.
    for i in range(20):
        assert (await fresh_store.get(i)) is not None
