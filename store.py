"""SQLite-backed store mapping Telegram user IDs to Seerr user IDs."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Mapping:
    telegram_id: int
    seerr_id: int
    seerr_display: str


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
