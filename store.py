"""SQLite-backed store mapping Telegram user IDs to Seerr user IDs.

All public methods are async and run their SQLite work in a thread pool
so they don't block the event loop. Connections enable WAL mode and a
5-second busy_timeout; locked-database errors are retried with backoff.

Token decryption distinguishes three states on the Mapping it returns:
  - plex_token=str, decrypt_failed=False  -> usable link
  - plex_token=None, decrypt_failed=False -> no token stored (legacy)
  - plex_token=None, decrypt_failed=True  -> token row exists but won't
    decrypt with the current encryption key. Callers should surface this
    distinctly from 'not linked'.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, TypeVar

from cryptography.fernet import Fernet, InvalidToken

from const import AUTOFIX_TIMEOUT_HOURS
from fsutil import atomic_write_bytes

logger = logging.getLogger("hermes." + __name__)

T = TypeVar("T")

# Locked-DB retry budget. Total worst-case sleep = 50+100+200+400+800 = 1550ms.
_LOCKED_MAX_ATTEMPTS = 5
_LOCKED_BASE_DELAY_S = 0.05


@dataclass
class Mapping:
    telegram_id: int
    seerr_id: int
    seerr_display: str
    plex_token: Optional[str]      # decrypted; None for legacy or decrypt-failed
    plex_uuid: Optional[str]
    plex_username: Optional[str]
    plex_token_decrypt_failed: bool = False  # True if a ciphertext exists but won't decrypt


class TokenCrypto:
    """Fernet-based encryption for Plex tokens stored at rest."""

    def __init__(self, key_path: str | Path = "/data/encryption.key"):
        self.key_path = Path(key_path)
        env_key = os.environ.get("HERMES_ENCRYPTION_KEY", "").strip()
        if env_key:
            self.key = env_key.encode()
            logger.info("Using HERMES_ENCRYPTION_KEY from env")
        else:
            self.key = self._load_or_create_key()
        try:
            self.fernet = Fernet(self.key)
        except Exception as exc:
            raise SystemExit(
                "Invalid encryption key. HERMES_ENCRYPTION_KEY must be a valid "
                "urlsafe-base64-encoded 32-byte Fernet key. "
                f"Generate one with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'. ({exc})"
            )

    def _load_or_create_key(self) -> bytes:
        try:
            existing = self.key_path.read_bytes().strip()
            if existing:
                return existing
        except FileNotFoundError:
            pass
        key = Fernet.generate_key()
        # Atomic + durable: a torn write here crash-loops the container on
        # every subsequent boot (Fernet(key) raises SystemExit).
        atomic_write_bytes(self.key_path, key)
        logger.info("Generated new encryption key at %s", self.key_path)
        return key

    def encrypt(self, plaintext: str) -> str:
        return self.fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        return self.fernet.decrypt(ciphertext.encode()).decode()


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

    async def is_complete(self, radarr, sonarr) -> tuple[bool, str]:
        """Returns (done, extra_suffix). Polymorphic dispatch over media_type
        lives here so the poller stays flat. Only {movie_id} and
        {series_id, episode_id} poll shapes exist; the whole-season branch
        was removed as dead code in 0.12.0 (columns kept)."""
        if self.media_type == "movie" and radarr and self.radarr_movie_id:
            return await radarr.movie_has_file(self.radarr_movie_id), ""
        if self.media_type == "tv" and sonarr and self.sonarr_episode_id:
            return await sonarr.episode_has_file(self.sonarr_episode_id), ""
        return False, ""


class UserStore:
    def __init__(self, db_path: str | Path, crypto: Optional[TokenCrypto] = None):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.crypto = crypto or TokenCrypto(key_path=self.path.parent / "encryption.key")
        self._init_schema()
        self._migrate_schema()

    # --- Connection helpers ---------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        # busy_timeout: wait up to 5s for a writer to release the lock
        # before raising OperationalError. Combined with WAL mode (set
        # once in _init_schema), this serializes writes cleanly under
        # concurrent load without throwing.
        c = sqlite3.connect(self.path, timeout=5.0)
        c.execute("PRAGMA busy_timeout = 5000")
        return c

    def _run_sync_with_retry(self, fn: Callable[[], T]) -> T:
        """Run fn synchronously, retrying on OperationalError(locked).
        Caller is responsible for putting this inside asyncio.to_thread.
        """
        for attempt in range(_LOCKED_MAX_ATTEMPTS):
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == _LOCKED_MAX_ATTEMPTS - 1:
                    raise
                time.sleep(_LOCKED_BASE_DELAY_S * (2 ** attempt))
        # Unreachable -- last attempt's raise gets us out.
        raise RuntimeError("retry loop exited without success or raise")

    async def _run(self, fn: Callable[[], T]) -> T:
        """Run a sync DB function in a thread with locked-retry."""
        return await asyncio.to_thread(self._run_sync_with_retry, fn)

    # --- Schema ----------------------------------------------------------

    def _init_schema(self) -> None:
        with self._conn() as c:
            # WAL gives concurrent readers + non-blocking writers. Set once
            # per database file; persists across connections.
            c.execute("PRAGMA journal_mode = WAL")
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
            # NOTE: idx_pending_status is created in _migrate_schema, after
            # column reconciliation -- an old-shape table may not have the
            # status column yet when this runs.

    # Bump when any table's expected shape changes, and add the reconciling
    # entries to _EXPECTED_COLUMNS below. Version 1 = the stamped 0.12.0
    # schema; 0 = any pre-stamp database (CREATE TABLE IF NOT
    # EXISTS never reconciles an existing old-shape table, and a missing
    # column is a permanent poller kill).
    SCHEMA_VERSION = 1

    # Full expected column sets, table -> (column, ADD COLUMN ddl). ALTER
    # TABLE ADD COLUMN needs a default for NOT NULL adds, so nullable/
    # defaulted forms are used -- fine for reconciliation, since rows that
    # predate a column have no better value anyway.
    _EXPECTED_COLUMNS: dict[str, list[tuple[str, str]]] = {
        "user_mapping": [
            ("plex_token_enc", "ALTER TABLE user_mapping ADD COLUMN plex_token_enc TEXT"),
            ("plex_uuid",      "ALTER TABLE user_mapping ADD COLUMN plex_uuid TEXT"),
            ("plex_username",  "ALTER TABLE user_mapping ADD COLUMN plex_username TEXT"),
        ],
        "autofix_events": [
            ("season",  "ALTER TABLE autofix_events ADD COLUMN season INTEGER"),
            ("episode", "ALTER TABLE autofix_events ADD COLUMN episode INTEGER"),
        ],
        "pending_autofixes": [
            ("radarr_movie_id",      "ALTER TABLE pending_autofixes ADD COLUMN radarr_movie_id INTEGER"),
            ("sonarr_series_id",     "ALTER TABLE pending_autofixes ADD COLUMN sonarr_series_id INTEGER"),
            ("sonarr_episode_id",    "ALTER TABLE pending_autofixes ADD COLUMN sonarr_episode_id INTEGER"),
            ("sonarr_season",        "ALTER TABLE pending_autofixes ADD COLUMN sonarr_season INTEGER"),
            ("expected_episode_ids", "ALTER TABLE pending_autofixes ADD COLUMN expected_episode_ids TEXT NOT NULL DEFAULT '[]'"),
            ("issue_url",            "ALTER TABLE pending_autofixes ADD COLUMN issue_url TEXT NOT NULL DEFAULT ''"),
            ("status",               "ALTER TABLE pending_autofixes ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"),
        ],
    }

    def _migrate_schema(self) -> None:
        """Reconcile every table to the current shape, then stamp
        PRAGMA user_version so future migrations can branch on it."""
        with self._conn() as c:
            for table, expected in self._EXPECTED_COLUMNS.items():
                cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
                if not cols:
                    continue  # table missing entirely; _init_schema creates it
                for col, ddl in expected:
                    if col not in cols:
                        logger.info("Schema migration: adding %s.%s", table, col)
                        c.execute(ddl)
            # Index depends on the status column, which may only just have
            # been added by the reconciliation above.
            c.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_pending_status
                ON pending_autofixes(status)
                """
            )
            version = c.execute("PRAGMA user_version").fetchone()[0]
            if version < self.SCHEMA_VERSION:
                c.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

    # --- Token decryption helper ----------------------------------------

    def _decrypt_field(self, raw: Optional[str]) -> tuple[Optional[str], bool]:
        """Return (decrypted_or_None, decrypt_failed)."""
        if not raw:
            return None, False
        try:
            return self.crypto.decrypt(raw), False
        except InvalidToken:
            logger.warning("Couldn't decrypt Plex token (key rotated or row corrupted)")
            return None, True

    # --- Mapping CRUD ----------------------------------------------------

    async def link_with_plex(
        self,
        *,
        telegram_id: int,
        seerr_id: int,
        seerr_display: str,
        plex_token: str,
        plex_uuid: str,
        plex_username: str,
    ) -> None:
        enc = self.crypto.encrypt(plex_token)

        def _do():
            with self._conn() as c:
                c.execute(
                    """
                    INSERT INTO user_mapping
                        (telegram_id, seerr_id, seerr_display, plex_token_enc, plex_uuid, plex_username)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(telegram_id) DO UPDATE SET
                        seerr_id = excluded.seerr_id,
                        seerr_display = excluded.seerr_display,
                        plex_token_enc = excluded.plex_token_enc,
                        plex_uuid = excluded.plex_uuid,
                        plex_username = excluded.plex_username,
                        linked_at = datetime('now')
                    """,
                    (telegram_id, seerr_id, seerr_display, enc, plex_uuid, plex_username),
                )

        await self._run(_do)

    async def find_by_plex_username(self, plex_username: str) -> Optional[Mapping]:
        """Lookup by Plex username (case-insensitive). Maps Seerr webhook
        payloads (which carry reportedBy_username) back to a linked TG user.
        """
        if not plex_username:
            return None

        def _do() -> Optional[tuple]:
            with self._conn() as c:
                return c.execute(
                    """
                    SELECT telegram_id, seerr_id, seerr_display,
                           plex_token_enc, plex_uuid, plex_username
                    FROM user_mapping
                    WHERE LOWER(plex_username) = LOWER(?)
                    LIMIT 1
                    """,
                    (plex_username,),
                ).fetchone()

        row = await self._run(_do)
        if row is None:
            return None
        token, failed = self._decrypt_field(row[3])
        return Mapping(
            telegram_id=row[0],
            seerr_id=row[1],
            seerr_display=row[2],
            plex_token=token,
            plex_uuid=row[4],
            plex_username=row[5],
            plex_token_decrypt_failed=failed,
        )

    async def unlink(self, telegram_id: int) -> bool:
        def _do() -> bool:
            with self._conn() as c:
                cur = c.execute(
                    "DELETE FROM user_mapping WHERE telegram_id = ?",
                    (telegram_id,),
                )
                return cur.rowcount > 0

        return await self._run(_do)

    async def get(self, telegram_id: int) -> Optional[Mapping]:
        def _do() -> Optional[tuple]:
            with self._conn() as c:
                return c.execute(
                    """
                    SELECT telegram_id, seerr_id, seerr_display,
                           plex_token_enc, plex_uuid, plex_username
                    FROM user_mapping WHERE telegram_id = ?
                    """,
                    (telegram_id,),
                ).fetchone()

        row = await self._run(_do)
        if row is None:
            return None
        token, failed = self._decrypt_field(row[3])
        return Mapping(
            telegram_id=row[0],
            seerr_id=row[1],
            seerr_display=row[2],
            plex_token=token,
            plex_uuid=row[4],
            plex_username=row[5],
            plex_token_decrypt_failed=failed,
        )

    async def count_decrypt_failures(self) -> int:
        """Count user_mapping rows whose plex_token_enc is non-empty but
        won't decrypt with the current encryption key. Called at startup
        so the admin can be alerted if a key rotation orphaned linked users.
        """
        def _do() -> list[Optional[str]]:
            with self._conn() as c:
                return [
                    r[0] for r in c.execute(
                        "SELECT plex_token_enc FROM user_mapping WHERE plex_token_enc IS NOT NULL AND plex_token_enc != ''"
                    ).fetchall()
                ]

        rows = await self._run(_do)
        count = 0
        for raw in rows:
            _, failed = self._decrypt_field(raw)
            if failed:
                count += 1
        return count

    # --- Auto-fix rate limiting -----------------------------------------

    async def count_autofix_24h(self, telegram_id: int) -> int:
        def _do() -> int:
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

        return await self._run(_do)

    async def log_autofix(
        self,
        telegram_id: int,
        media_type: str,
        tmdb_id: int,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> None:
        def _do():
            with self._conn() as c:
                c.execute(
                    """
                    INSERT INTO autofix_events (telegram_id, media_type, tmdb_id, season, episode)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (telegram_id, media_type, tmdb_id, season, episode),
                )

        await self._run(_do)

    # --- Pending autofix tracking ---------------------------------------

    async def add_pending_autofix(
        self,
        *,
        chat_id: int,
        user_id: int,
        media_type: str,
        label: str,
        issue_id: int,
        issue_url: str,
        # Single source of truth: no caller passes this, so the
        # const IS the effective timeout, and the DM text derives from the
        # same value instead of lying when it's tuned.
        timeout_hours: int = AUTOFIX_TIMEOUT_HOURS,
        radarr_movie_id: Optional[int] = None,
        sonarr_series_id: Optional[int] = None,
        sonarr_episode_id: Optional[int] = None,
        sonarr_season: Optional[int] = None,
        expected_episode_ids: Optional[list[int]] = None,
    ) -> int:
        def _do() -> int:
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

        return await self._run(_do)

    async def list_pending_autofixes(self) -> list[PendingAutofix]:
        def _do() -> list[tuple]:
            with self._conn() as c:
                return c.execute(
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

        rows = await self._run(_do)
        # One bad item must not kill the batch: a single corrupt row (garbage
        # JSON in expected_episode_ids, wrong types) previously raised here
        # on every poll tick, permanently stopping ALL completion/timeout DMs
        #. Skip and log the bad row; process the rest.
        out: list[PendingAutofix] = []
        for r in rows:
            try:
                ids = json.loads(r[8] or "[]")
                if not isinstance(ids, list):
                    raise ValueError(f"expected_episode_ids is {type(ids).__name__}, not list")
                out.append(PendingAutofix(
                    id=r[0], chat_id=r[1], user_id=r[2], media_type=r[3],
                    radarr_movie_id=r[4], sonarr_series_id=r[5],
                    sonarr_episode_id=r[6], sonarr_season=r[7],
                    expected_episode_ids=ids,
                    label=r[9], issue_id=r[10], issue_url=r[11],
                    started_at=r[12], timeout_at=r[13],
                ))
            except Exception:
                logger.exception("Skipping corrupt pending_autofix row id=%s", r[0])
        return out

    async def mark_autofix_status(self, pending_id: int, status: str) -> None:
        """status: 'complete', 'timeout', or 'failed'."""
        def _do():
            with self._conn() as c:
                c.execute(
                    "UPDATE pending_autofixes SET status = ? WHERE id = ?",
                    (status, pending_id),
                )

        await self._run(_do)
