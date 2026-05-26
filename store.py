"""SQLite-backed store mapping Telegram user IDs to Seerr user IDs."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Mapping:
    telegram_id: int
    seerr_id: int
    seerr_display: str


@dataclass
class PendingAutofix:
    id: int
    chat_id: int
    user_id: int
    media_type: str                     # "movie" or "tv"
    radarr_movie_id: Optional[int]      # for movies
    sonarr_series_id: Optional[int]     # for tv
    sonarr_episode_id: Optional[int]    # for single-episode tv
    sonarr_season: Optional[int]        # for whole-season tv
    expected_episode_ids: list[int]     # episodes that had files at fix time (whole-season)
    label: str                          # for the notification text
    issue_id: int
    issue_url: str
    started_at: str
    timeout_at: str


class UserStore:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS user_mapping (
                    telegram_id INTEGER PRIMARY KEY,
                    seerr_id INTEGER NOT NULL,
                    seerr_display TEXT NOT NULL,
                    linked_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS autofix_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
                    media_type TEXT NOT NULL,
                    tmdb_id INTEGER NOT NULL,
                    season INTEGER,
                    episode INTEGER
                )
                """
            )
            c.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_autofix_user_time
                ON autofix_events(telegram_id, occurred_at)
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_autofixes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    radarr_movie_id INTEGER,
                    sonarr_series_id INTEGER,
                    sonarr_episode_id INTEGER,
                    sonarr_season INTEGER,
                    expected_episode_ids TEXT NOT NULL DEFAULT '[]',
                    label TEXT NOT NULL,
                    issue_id INTEGER NOT NULL,
                    issue_url TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT (datetime('now')),
                    timeout_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                )
                """
            )
            c.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_pending_status
                ON pending_autofixes(status)
                """
            )

    def link(self, telegram_id: int, seerr_id: int, seerr_display: str) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO user_mapping (telegram_id, seerr_id, seerr_display)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    seerr_id = excluded.seerr_id,
                    seerr_display = excluded.seerr_display,
                    linked_at = datetime('now')
                """,
                (telegram_id, seerr_id, seerr_display),
            )

    def unlink(self, telegram_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM user_mapping WHERE telegram_id = ?",
                (telegram_id,),
            )
            return cur.rowcount > 0

    def get(self, telegram_id: int) -> Optional[Mapping]:
        with self._conn() as c:
            row = c.execute(
                "SELECT telegram_id, seerr_id, seerr_display FROM user_mapping WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
        if row is None:
            return None
        return Mapping(telegram_id=row[0], seerr_id=row[1], seerr_display=row[2])

    # --- Auto-fix rate limiting -----------------------------------------

    def count_autofix_24h(self, telegram_id: int) -> int:
        with self._conn() as c:
            row = c.execute(
                """
                SELECT COUNT(*) FROM autofix_events
                WHERE telegram_id = ?
                  AND occurred_at >= datetime('now', '-24 hours')
                """,
                (telegram_id,),
            ).fetchone()
        return row[0] if row else 0

    def log_autofix(
        self,
        telegram_id: int,
        media_type: str,
        tmdb_id: int,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO autofix_events (telegram_id, media_type, tmdb_id, season, episode)
                VALUES (?, ?, ?, ?, ?)
                """,
                (telegram_id, media_type, tmdb_id, season, episode),
            )

    # --- Pending autofix tracking ---------------------------------------

    def add_pending_autofix(
        self,
        *,
        chat_id: int,
        user_id: int,
        media_type: str,
        label: str,
        issue_id: int,
        issue_url: str,
        timeout_hours: int = 6,
        radarr_movie_id: Optional[int] = None,
        sonarr_series_id: Optional[int] = None,
        sonarr_episode_id: Optional[int] = None,
        sonarr_season: Optional[int] = None,
        expected_episode_ids: Optional[list[int]] = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO pending_autofixes (
                    chat_id, user_id, media_type,
                    radarr_movie_id, sonarr_series_id, sonarr_episode_id, sonarr_season,
                    expected_episode_ids, label, issue_id, issue_url,
                    timeout_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', ?))
                """,
                (
                    chat_id, user_id, media_type,
                    radarr_movie_id, sonarr_series_id, sonarr_episode_id, sonarr_season,
                    json.dumps(expected_episode_ids or []), label, issue_id, issue_url,
                    f"+{timeout_hours} hours",
                ),
            )
            return cur.lastrowid

    def list_pending_autofixes(self) -> list[PendingAutofix]:
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT id, chat_id, user_id, media_type, radarr_movie_id,
                       sonarr_series_id, sonarr_episode_id, sonarr_season,
                       expected_episode_ids, label, issue_id, issue_url,
                       started_at, timeout_at
                FROM pending_autofixes
                WHERE status = 'pending'
                ORDER BY id
                """
            ).fetchall()
        return [
            PendingAutofix(
                id=r[0], chat_id=r[1], user_id=r[2], media_type=r[3],
                radarr_movie_id=r[4], sonarr_series_id=r[5],
                sonarr_episode_id=r[6], sonarr_season=r[7],
                expected_episode_ids=json.loads(r[8] or "[]"),
                label=r[9], issue_id=r[10], issue_url=r[11],
                started_at=r[12], timeout_at=r[13],
            )
            for r in rows
        ]

    def mark_autofix_status(self, pending_id: int, status: str) -> None:
        """status: 'complete', 'timeout', or 'failed'."""
        with self._conn() as c:
            c.execute(
                "UPDATE pending_autofixes SET status = ? WHERE id = ?",
                (status, pending_id),
            )
