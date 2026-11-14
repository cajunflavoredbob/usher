"""Tests for store.py: UserStore lifecycle, decrypt-failed semantics, autofix tracking."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from store import Link, TokenCrypto, UserStore


async def test_init_creates_schema_and_wal(tmp_db_path: Path, fresh_token_crypto):
    UserStore(tmp_db_path, crypto=fresh_token_crypto)
    with sqlite3.connect(tmp_db_path) as c:
        mode = c.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


async def test_link_and_get_roundtrip(fresh_store: UserStore):
    await fresh_store.link_with_plex(
        telegram_id=42,
        seerr_id=7,
        seerr_display="User1",
        plex_token="plex-token-secret",
        plex_uuid="uuid-abc",
        plex_username="user1",
    )
    link = await fresh_store.get(42)
    assert link is not None
    assert link.telegram_id == 42
    assert link.seerr_id == 7
    assert link.plex_token == "plex-token-secret"
    assert link.plex_username == "user1"
    assert link.plex_token_decrypt_failed is False


async def test_get_missing_returns_none(fresh_store: UserStore):
    assert (await fresh_store.get(9999)) is None


async def test_find_by_plex_username_case_insensitive(fresh_store: UserStore):
    await fresh_store.link_with_plex(
        telegram_id=42, seerr_id=7, seerr_display="User1",
        plex_token="t", plex_uuid="u", plex_username="User1Plex",
    )
    assert (await fresh_store.find_by_plex_username("user1plex")) is not None
    assert (await fresh_store.find_by_plex_username("USER1PLEX")) is not None
    assert (await fresh_store.find_by_plex_username("someone-else")) is None


async def test_find_by_plex_username_empty_returns_none(fresh_store: UserStore):
    assert (await fresh_store.find_by_plex_username("")) is None


async def test_unlink(fresh_store: UserStore):
    await fresh_store.link_with_plex(
        telegram_id=42, seerr_id=7, seerr_display="User1",
        plex_token="t", plex_uuid="u", plex_username="user1",
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
        telegram_id=42, seerr_id=7, seerr_display="User1",
        plex_token="t", plex_uuid="u", plex_username="user1",
    )
    # Re-open with key B.
    key_b_path = tmp_path / "key_b.bin"
    key_b_path.write_bytes(Fernet.generate_key())
    key_b = TokenCrypto(key_path=key_b_path)
    store_b = UserStore(tmp_db_path, crypto=key_b)

    link = await store_b.get(42)
    assert link is not None
    assert link.plex_token is None
    assert link.plex_token_decrypt_failed is True


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
        label="Movie (2026)", ticket_id=1, ticket_url="http://x/issues/1",
        radarr_movie_id=555,
    )
    assert isinstance(pid, int)

    pending = await fresh_store.list_pending_autofixes()
    assert len(pending) == 1
    assert pending[0].id == pid
    assert pending[0].media_type == "movie"
    assert pending[0].radarr_movie_id == 555
    assert pending[0].ticket_id == 1

    await fresh_store.mark_autofix_status(pid, "complete")
    assert (await fresh_store.list_pending_autofixes()) == []  # only 'pending' rows listed


async def test_pending_autofix_whole_season_carries_episode_ids(fresh_store: UserStore):
    await fresh_store.add_pending_autofix(
        chat_id=100, user_id=42, media_type="tv",
        label="Show S01", ticket_id=2, ticket_url="http://x/issues/2",
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


# --- corrupt-row resilience + schema stamp -----------------------------------


async def test_corrupt_pending_row_is_skipped_not_fatal(fresh_store: UserStore,
                                                        tmp_db_path: Path):
    """One bad item must not kill the batch: garbage JSON in a
    single expected_episode_ids cell previously raised on every poll tick,
    permanently stopping all completion/timeout DMs."""
    good = await fresh_store.add_pending_autofix(
        chat_id=100, user_id=42, media_type="movie",
        label="Good Movie", ticket_id=1, ticket_url="http://x/issues/1",
        radarr_movie_id=555,
    )
    bad = await fresh_store.add_pending_autofix(
        chat_id=100, user_id=42, media_type="tv",
        label="Bad Show", ticket_id=2, ticket_url="http://x/issues/2",
        sonarr_series_id=10, sonarr_season=1, expected_episode_ids=[101],
    )
    with sqlite3.connect(tmp_db_path) as c:
        c.execute("UPDATE pending_autofixes SET expected_episode_ids = ? WHERE id = ?",
                  ("{not json", bad))
    pending = await fresh_store.list_pending_autofixes()
    assert [p.id for p in pending] == [good]


async def test_non_list_episode_ids_is_skipped(fresh_store: UserStore,
                                               tmp_db_path: Path):
    pid = await fresh_store.add_pending_autofix(
        chat_id=100, user_id=42, media_type="tv",
        label="Show", ticket_id=2, ticket_url="http://x/issues/2",
        sonarr_series_id=10, sonarr_season=1, expected_episode_ids=[101],
    )
    with sqlite3.connect(tmp_db_path) as c:
        c.execute("UPDATE pending_autofixes SET expected_episode_ids = ? WHERE id = ?",
                  ('{"a": 1}', pid))  # valid JSON, wrong shape
    assert await fresh_store.list_pending_autofixes() == []


async def test_schema_version_is_stamped(tmp_db_path: Path, fresh_token_crypto):
    UserStore(tmp_db_path, crypto=fresh_token_crypto)
    with sqlite3.connect(tmp_db_path) as c:
        version = c.execute("PRAGMA user_version").fetchone()[0]
    assert version == UserStore.SCHEMA_VERSION


async def test_old_shape_autofix_table_is_reconciled(tmp_db_path: Path,
                                                     fresh_token_crypto):
    """CREATE TABLE IF NOT EXISTS never fixes an existing old-shape table
   ; opening the store must add the missing columns instead of
    leaving a permanent 'no such column' poller kill."""
    with sqlite3.connect(tmp_db_path) as c:
        c.execute(
            """
            CREATE TABLE pending_autofixes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                media_type TEXT NOT NULL,
                label TEXT NOT NULL,
                issue_id INTEGER NOT NULL,
                started_at TEXT NOT NULL DEFAULT (datetime('now')),
                timeout_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
    store = UserStore(tmp_db_path, crypto=fresh_token_crypto)
    # The full modern insert + list path must work against the migrated table.
    pid = await store.add_pending_autofix(
        chat_id=100, user_id=42, media_type="movie",
        label="Movie", ticket_id=1, ticket_url="http://x/issues/1",
        radarr_movie_id=555,
    )
    pending = await store.list_pending_autofixes()
    assert [p.id for p in pending] == [pid]


async def test_pending_autofix_keeps_the_card_keyboard(fresh_store: UserStore):
    pid = await fresh_store.add_pending_autofix(
        chat_id=100, user_id=42, media_type="movie",
        label="Movie (2026)", ticket_id=1, ticket_url="http://x/issues/1",
        radarr_movie_id=555,
    )
    assert (await fresh_store.list_pending_autofixes())[0].reply_markup == ""
    await fresh_store.set_autofix_message(pid, 77, reply_markup='{"k": 1}')
    row = (await fresh_store.list_pending_autofixes())[0]
    assert row.message_id == 77 and row.reply_markup == '{"k": 1}'


# --- pre-rename encryption-key env name ---


def test_encryption_key_env_falls_back_to_pre_rename_name(monkeypatch, tmp_path: Path):
    from former_name import legacy_env

    key = Fernet.generate_key().decode()
    monkeypatch.delenv("USHER_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv(legacy_env("ENCRYPTION_KEY"), key)
    assert TokenCrypto(key_path=tmp_path / "k.bin").key == key.encode()


def test_encryption_key_env_prefers_new_name(monkeypatch, tmp_path: Path):
    from former_name import legacy_env

    new_key = Fernet.generate_key().decode()
    monkeypatch.setenv("USHER_ENCRYPTION_KEY", new_key)
    monkeypatch.setenv(legacy_env("ENCRYPTION_KEY"), Fernet.generate_key().decode())
    assert TokenCrypto(key_path=tmp_path / "k.bin").key == new_key.encode()


async def test_pending_autofix_keeps_the_fix_action_name(fresh_store: UserStore):
    await fresh_store.add_pending_autofix(
        chat_id=100, user_id=42, media_type="movie",
        label="Movie", ticket_id=1, ticket_url="http://x/issues/1",
        radarr_movie_id=555)
    await fresh_store.add_pending_autofix(
        chat_id=100, user_id=42, media_type="movie",
        label="Movie", ticket_id=2, ticket_url="http://x/issues/2",
        radarr_movie_id=556, action_name="Redownload")
    rows = await fresh_store.list_pending_autofixes()
    assert [r.action_name for r in rows] == ["", "Redownload"]


async def test_old_shape_request_watches_gets_the_seasons_column(
        tmp_db_path: Path, fresh_token_crypto):
    """A database from before the seasons column keeps its card rows (seasons
    unknown) and can store a TV card's seasons after opening."""
    with sqlite3.connect(tmp_db_path) as c:
        c.execute(
            """
            CREATE TABLE request_watches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                media_type TEXT NOT NULL,
                tmdb_id INTEGER NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                is4k INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'waiting',
                arr_id INTEGER,
                last_progress TEXT NOT NULL DEFAULT '',
                bumped INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                timeout_at TEXT NOT NULL
            )
            """
        )
        c.execute(
            "INSERT INTO request_watches (chat_id, message_id, user_id, "
            "media_type, tmdb_id, timeout_at) "
            "VALUES (1, 2, 3, 'tv', 1399, datetime('now', '+1 hours'))")
        c.execute("PRAGMA user_version = 2")
    store = UserStore(tmp_db_path, crypto=fresh_token_crypto)
    old = (await store.find_progress_cards("tv", 1399))[0]
    assert old["seasons"] is None
    await store.add_progress_card(
        chat_id=1, message_id=9, user_id=3, media_type="tv", tmdb_id=1399,
        label="Show", is4k=False, status="waiting", timeout_hours=1,
        seasons=[2])
    rows = await store.find_progress_cards("tv", 1399)
    assert sorted(map(str, (r["seasons"] for r in rows))) == ["None", "[2]"]
    with sqlite3.connect(tmp_db_path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == UserStore.SCHEMA_VERSION


async def test_newest_link_wins_not_the_largest_telegram_id(fresh_store: UserStore):
    import sqlite3
    big, small = 900000001, 100000002
    await fresh_store.link_with_plex(telegram_id=big, seerr_id=7,
                                     seerr_display="U", plex_token="t1",
                                     plex_uuid="u1", plex_username="shared")
    await fresh_store.link_with_plex(telegram_id=small, seerr_id=7,
                                     seerr_display="U", plex_token="t2",
                                     plex_uuid="u2", plex_username="shared")
    # Make the order unambiguous at one-second timestamp resolution.
    with sqlite3.connect(fresh_store.path) as c:
        c.execute("UPDATE user_mapping SET linked_at = datetime('now', '-1 hour')"
                  " WHERE telegram_id = ?", (big,))
    assert (await fresh_store.find_by_seerr_id(7)).telegram_id == small
    assert (await fresh_store.find_by_plex_username("shared")).telegram_id == small


async def test_old_ticket_escalations_are_pruned(fresh_store: UserStore):
    import sqlite3
    assert await fresh_store.claim_ticket_escalation(5)
    with sqlite3.connect(fresh_store.path) as c:
        c.execute("UPDATE ticket_escalations SET escalated_at = "
                  "datetime('now', '-200 days')")
    await fresh_store.prune_autofix_history()
    assert await fresh_store.claim_ticket_escalation(5)
