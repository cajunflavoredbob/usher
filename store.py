"""SQLite-backed store of Links, each tying a Telegram user ID to a Seerr user ID.

All public methods are async and run their SQLite work in a thread pool
so they don't block the event loop. Connections enable WAL mode and a
5-second busy_timeout; locked-database errors are retried with backoff.

Token decryption distinguishes three states on the Link it returns:
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
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional, TypeVar

from cryptography.fernet import Fernet, InvalidToken

from const import AUTOFIX_TIMEOUT_HOURS
from fix_kinds import (KIND_AUTO_FIX, OUTCOME_COMPLETE, OUTCOME_FAILED,
                       OUTCOME_PENDING, OUTCOME_REPEAT, OUTCOME_TIMEOUT)
from former_name import legacy_env
from fix_result import ReleaseFingerprint
from fsutil import atomic_write_bytes

logger = logging.getLogger("usher." + __name__)

T = TypeVar("T")

# Fix history is kept this long, and the repeat check looks back the same
# window: shortening one without the other would blind the retry warning.
AUTOFIX_HISTORY_DAYS = 30
# "Ask the Admin" claims are dropped after this long (see the prune).
TICKET_ESCALATION_KEEP_DAYS = 90

# Locked-DB retry budget. Total worst-case sleep = 50+100+200+400+800 = 1550ms.
_LOCKED_MAX_ATTEMPTS = 5
_LOCKED_BASE_DELAY_S = 0.05


@dataclass
class Link:
    telegram_id: int
    seerr_id: int
    seerr_display: str
    plex_token: Optional[str]      # decrypted; None for legacy or decrypt-failed
    # Stored but currently unread: kept as the stable Plex identity for a
    # future username-change reconcile (usernames can change, uuid can't).
    plex_uuid: Optional[str]
    plex_username: Optional[str]
    plex_token_decrypt_failed: bool = False  # True if a ciphertext exists but won't decrypt


class TokenCrypto:
    """Fernet-based encryption for Plex tokens stored at rest."""

    def __init__(self, key_path: str | Path = "/data/encryption.key"):
        self.key_path = Path(key_path)
        # Pre-rename installs may still set the former-name env name.
        env_key = (os.environ.get("USHER_ENCRYPTION_KEY")
                   or os.environ.get(legacy_env("ENCRYPTION_KEY")) or "").strip()
        if env_key:
            self.key = env_key.encode()
            logger.info("Using encryption key from env")
        else:
            self.key = self._load_or_create_key()
        try:
            self.fernet = Fernet(self.key)
        except Exception as exc:
            raise SystemExit(
                "Invalid encryption key. USHER_ENCRYPTION_KEY must be a valid "
                "urlsafe-base64-encoded 32-byte Fernet key. "
                f"Generate one with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'. ({exc})"
            )

    def _load_or_create_key(self) -> bytes:
        try:
            existing = self.key_path.read_bytes().strip()
            if existing:
                return existing
        except FileNotFoundError as exc:
            logger.debug("no stored encryption key yet: %s", exc)
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


@dataclass(frozen=True)
class RecentFixEvent:
    """A Fix event on an item: kind ('auto-fix', 'redownload', 'mark
    failed'), outcome, and how many seconds ago it ran."""
    kind: str
    outcome: str
    age_seconds: int


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
    ticket_id: int
    ticket_url: str
    started_at: str
    timeout_at: str
    message_id: Optional[int] = None    # the fix's Progress card (the /issue
                                        # confirmation or an admin Fix's new
                                        # message; None = legacy row /
                                        # send-only behavior)
    last_progress: str = ""             # last rendered progress line (edit
                                        # dedupe)
    bumped: int = 0                     # SABnzbd priority boost applied
    reply_markup: str = ""              # the card's keyboard as JSON, kept on
                                        # each progress edit ("" = none)
    action_name: str = ""               # an admin Fix's own name ("Redownload");
                                        # "" = a Reporter's /issue Auto-fix
    before_fingerprint: ReleaseFingerprint = field(
        default_factory=ReleaseFingerprint)  # the release the Fix deleted
                                             # (empty = unknown)
    auto_blocklisted: int = 0           # 1 = the bot's own Mark Failed on a
                                        # repeat; its own repeat never
                                        # triggers another

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
        # Serializes DB access against maintenance (webui restore swaps the
        # DB file under us); see _run.
        self.maintenance_lock = asyncio.Lock()
        self._init_schema()
        self._migrate_schema()

    # Connection helpers

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        # busy_timeout: wait up to 5s for a writer to release the lock
        # before raising OperationalError. Combined with WAL mode (set
        # once in _init_schema), this serializes writes cleanly under
        # concurrent load without throwing.
        #
        # Context manager wraps the sqlite3 connection's own
        # commit-or-rollback semantics AND closes the connection on exit:
        # sqlite3's native `with conn` never closes, which leaked every
        # call's connection to refcount GC.
        c = sqlite3.connect(self.path, timeout=5.0)
        try:
            c.execute("PRAGMA busy_timeout = 5000")
            with c:
                yield c
        finally:
            c.close()

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
        """Run a sync DB function in a thread with locked-retry, serialized
        against maintenance operations (restore's DB-file swap) via
        maintenance_lock. Ordinary calls just pass through the lock; a
        restore holds it so no write can land between the DB rename and the
        stale-WAL cleanup."""
        async with self.maintenance_lock:
            return await asyncio.to_thread(self._run_sync_with_retry, fn)

    # Schema

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
                    episode INTEGER,
                    kind TEXT NOT NULL DEFAULT 'auto-fix',
                    outcome TEXT NOT NULL DEFAULT 'pending',
                    refunded INTEGER NOT NULL DEFAULT 0,
                    pending_id INTEGER
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
            # One row per Ticket the Reporter asked the Admin to try a
            # different release for. CREATE IF NOT EXISTS runs on every start,
            # so a database already stamped schema 4 gets it too.
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS ticket_escalations (
                    ticket_id INTEGER PRIMARY KEY,
                    escalated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS availability_subs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    tmdb_id INTEGER NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(telegram_id, media_type, tmdb_id)
                )
                """
            )
            c.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_subs_media
                ON availability_subs(media_type, tmdb_id)
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS request_watches (
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
                    timeout_at TEXT NOT NULL,
                    seasons TEXT
                )
                """
            )
            c.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_watch_media
                ON request_watches(media_type, tmdb_id)
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
    SCHEMA_VERSION = 4

    # Full expected column sets, table -> (column, ADD COLUMN ddl). ALTER
    # TABLE ADD COLUMN needs a default for NOT NULL adds, so nullable/
    # defaulted forms are used -- fine for reconciliation, since rows that
    # predate a column have no better value anyway.
    _EXPECTED_COLUMNS: dict[str, list[tuple[str, str]]] = {
        "request_watches": [
            # JSON list of the requested season numbers (TV cards); NULL
            # when unknown (movies, and cards made before this column).
            ("seasons", "ALTER TABLE request_watches ADD COLUMN seasons TEXT"),
        ],
        "user_mapping": [
            ("plex_token_enc", "ALTER TABLE user_mapping ADD COLUMN plex_token_enc TEXT"),
            ("plex_uuid",      "ALTER TABLE user_mapping ADD COLUMN plex_uuid TEXT"),
            ("plex_username",  "ALTER TABLE user_mapping ADD COLUMN plex_username TEXT"),
        ],
        "autofix_events": [
            ("season",  "ALTER TABLE autofix_events ADD COLUMN season INTEGER"),
            ("episode", "ALTER TABLE autofix_events ADD COLUMN episode INTEGER"),
            # Schema 4 (unreleased): what Fix ran ('auto-fix', 'redownload',
            # 'mark failed'; older rows read as auto-fix), how it ended
            # ('pending', 'complete', 'repeat', 'timeout', 'failed'), whether
            # the Reporter got the use back, and the pending fix row it ties
            # to. A database already stamped 4 by an earlier build gets them
            # here too, since the reconcile adds any missing column.
            ("kind",     "ALTER TABLE autofix_events ADD COLUMN kind TEXT NOT NULL DEFAULT 'auto-fix'"),
            ("outcome",  "ALTER TABLE autofix_events ADD COLUMN outcome TEXT NOT NULL DEFAULT 'pending'"),
            ("refunded", "ALTER TABLE autofix_events ADD COLUMN refunded INTEGER NOT NULL DEFAULT 0"),
            ("pending_id", "ALTER TABLE autofix_events ADD COLUMN pending_id INTEGER"),
        ],
        "pending_autofixes": [
            ("radarr_movie_id",      "ALTER TABLE pending_autofixes ADD COLUMN radarr_movie_id INTEGER"),
            ("sonarr_series_id",     "ALTER TABLE pending_autofixes ADD COLUMN sonarr_series_id INTEGER"),
            ("sonarr_episode_id",    "ALTER TABLE pending_autofixes ADD COLUMN sonarr_episode_id INTEGER"),
            ("sonarr_season",        "ALTER TABLE pending_autofixes ADD COLUMN sonarr_season INTEGER"),
            ("expected_episode_ids", "ALTER TABLE pending_autofixes ADD COLUMN expected_episode_ids TEXT NOT NULL DEFAULT '[]'"),
            ("issue_url",            "ALTER TABLE pending_autofixes ADD COLUMN issue_url TEXT NOT NULL DEFAULT ''"),
            ("status",               "ALTER TABLE pending_autofixes ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"),
            ("message_id",           "ALTER TABLE pending_autofixes ADD COLUMN message_id INTEGER"),
            ("last_progress",        "ALTER TABLE pending_autofixes ADD COLUMN last_progress TEXT NOT NULL DEFAULT ''"),
            ("bumped",               "ALTER TABLE pending_autofixes ADD COLUMN bumped INTEGER NOT NULL DEFAULT 0"),
            ("reply_markup",         "ALTER TABLE pending_autofixes ADD COLUMN reply_markup TEXT NOT NULL DEFAULT ''"),
            ("action_name",          "ALTER TABLE pending_autofixes ADD COLUMN action_name TEXT NOT NULL DEFAULT ''"),
            # Schema 4: the release the Fix deleted, as ReleaseFingerprint
            # JSON ('' = unknown, which is what every older row reads as).
            ("before_fingerprint",   "ALTER TABLE pending_autofixes ADD COLUMN before_fingerprint TEXT NOT NULL DEFAULT ''"),
            ("auto_blocklisted",     "ALTER TABLE pending_autofixes ADD COLUMN auto_blocklisted INTEGER NOT NULL DEFAULT 0"),
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

    # Token decryption helper

    def _decrypt_field(self, raw: Optional[str]) -> tuple[Optional[str], bool]:
        """Return (decrypted_or_None, decrypt_failed)."""
        if not raw:
            return None, False
        try:
            return self.crypto.decrypt(raw), False
        except InvalidToken:
            logger.warning("Couldn't decrypt Plex token (key rotated or row corrupted)")
            return None, True

    # Link CRUD

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

    async def find_by_seerr_id(self, seerr_id: int) -> Optional[Link]:
        """Lookup by Seerr user id. The robust join key for webhook events
        whose requester was resolved via the Seerr API (display names are
        user-editable in Seerr; the numeric id is not). Newest link wins,
        same as find_by_plex_username."""
        if not seerr_id:
            return None

        def _do() -> Optional[tuple]:
            with self._conn() as c:
                return c.execute(
                    """
                    SELECT telegram_id, seerr_id, seerr_display,
                           plex_token_enc, plex_uuid, plex_username
                    FROM user_mapping
                    WHERE seerr_id = ?
                    ORDER BY linked_at DESC, rowid DESC
                    LIMIT 1
                    """,
                    (seerr_id,),
                ).fetchone()

        row = await self._run(_do)
        if row is None:
            return None
        token, failed = self._decrypt_field(row[3])
        return Link(
            telegram_id=row[0],
            seerr_id=row[1],
            seerr_display=row[2],
            plex_token=token,
            plex_uuid=row[4],
            plex_username=row[5],
            plex_token_decrypt_failed=failed,
        )

    async def find_by_plex_username(self, plex_username: str) -> Optional[Link]:
        """Lookup by Plex username (case-insensitive). Maps Seerr webhook
        payloads (which carry reportedBy_username) back to a linked TG user.
        Nothing stops two Telegram accounts from linking the same Plex
        account; ordering on linked_at (refreshed on every re-link) makes
        the newest link win instead of whichever row SQLite returns first.
        telegram_id is the rowid, so ordering on rowid alone would pick the
        largest Telegram id.
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
                    ORDER BY linked_at DESC, rowid DESC
                    LIMIT 1
                    """,
                    (plex_username,),
                ).fetchone()

        row = await self._run(_do)
        if row is None:
            return None
        token, failed = self._decrypt_field(row[3])
        return Link(
            telegram_id=row[0],
            seerr_id=row[1],
            seerr_display=row[2],
            plex_token=token,
            plex_uuid=row[4],
            plex_username=row[5],
            plex_token_decrypt_failed=failed,
        )

    # Request Progress cards
    # One row per /request Progress card (the message sent after the
    # confirmation) that edits itself through the download.
    # status: waiting (pending approval) -> grabbing -> gone
    # (finalized rows are deleted, not kept).

    async def add_progress_card(self, *, chat_id: int, message_id: int,
                                user_id: int, media_type: str, tmdb_id: int,
                                label: str, is4k: bool,
                                status: str, timeout_hours: int,
                                seasons: Optional[list] = None) -> int:
        def _do() -> int:
            with self._conn() as c:
                cur = c.execute(
                    """
                    INSERT INTO request_watches (
                        chat_id, message_id, user_id, media_type, tmdb_id,
                        label, is4k, status, timeout_at, seasons
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', ?), ?)
                    """,
                    (chat_id, message_id, user_id, media_type, tmdb_id,
                     label, 1 if is4k else 0, status,
                     f"+{timeout_hours} hours",
                     json.dumps(sorted(seasons)) if seasons else None),
                )
                return cur.lastrowid

        return await self._run(_do)

    @staticmethod
    def _card_row(keys: tuple, row: tuple) -> dict:
        card = dict(zip(keys, row))
        raw = card["seasons"]
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        card["seasons"] = parsed if isinstance(parsed, list) else None
        return card

    async def list_progress_cards(self) -> list[dict]:
        def _do() -> list[tuple]:
            with self._conn() as c:
                return c.execute(
                    """
                    SELECT id, chat_id, message_id, user_id, media_type,
                           tmdb_id, label, is4k, status, arr_id,
                           last_progress, bumped, timeout_at, seasons
                    FROM request_watches ORDER BY id
                    """
                ).fetchall()

        rows = await self._run(_do)
        keys = ("id", "chat_id", "message_id", "user_id", "media_type",
                "tmdb_id", "label", "is4k", "status", "arr_id",
                "last_progress", "bumped", "timeout_at", "seasons")
        return [self._card_row(keys, r) for r in rows]

    async def find_progress_cards(self, media_type: str,
                                  tmdb_id: int) -> list[dict]:
        def _do() -> list[tuple]:
            with self._conn() as c:
                return c.execute(
                    """
                    SELECT id, chat_id, message_id, user_id, media_type,
                           tmdb_id, label, is4k, status, arr_id,
                           last_progress, bumped, timeout_at, seasons
                    FROM request_watches
                    WHERE media_type = ? AND tmdb_id = ?
                    """,
                    (media_type, tmdb_id),
                ).fetchall()

        rows = await self._run(_do)
        keys = ("id", "chat_id", "message_id", "user_id", "media_type",
                "tmdb_id", "label", "is4k", "status", "arr_id",
                "last_progress", "bumped", "timeout_at", "seasons")
        return [self._card_row(keys, r) for r in rows]

    async def update_progress_card(self, card_id: int, **fields) -> bool:
        """Update whitelisted columns on one Progress card row. Returns True when
        the row still existed -- the poller uses this to skip painting
        progress over a card the webhook just finalized and deleted."""
        allowed = {"status", "arr_id", "last_progress", "bumped"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False

        def _do() -> bool:
            with self._conn() as c:
                sets = ", ".join(f"{k} = ?" for k in updates)
                cur = c.execute(
                    f"UPDATE request_watches SET {sets} WHERE id = ?",
                    (*updates.values(), card_id))
                return cur.rowcount > 0

        return await self._run(_do)

    async def restart_progress_card_timeout(self, card_id: int,
                                            hours: int) -> None:
        """Start a card's download timeout now. Called when a waiting card
        flips to grabbing: its timeout_at was set at creation, and a Request
        approved a day later would otherwise time out on the next poll."""

        def _do() -> None:
            with self._conn() as c:
                c.execute(
                    "UPDATE request_watches SET timeout_at = datetime('now', ?) "
                    "WHERE id = ?", (f"+{int(hours)} hours", card_id))

        await self._run(_do)

    async def delete_progress_card(self, card_id: int) -> None:
        def _do():
            with self._conn() as c:
                c.execute("DELETE FROM request_watches WHERE id = ?",
                          (card_id,))

        await self._run(_do)

    # Availability subscriptions
    # Strictly per-user: a user only ever sees and manages their own rows,
    # and nothing renders who else (or how many others) subscribed to a title.

    async def add_subscription(self, telegram_id: int, media_type: str,
                               tmdb_id: int, title: str) -> bool:
        """Subscribe a user to a title's availability. Returns False when
        the subscription already existed (idempotent re-tap)."""

        def _do() -> bool:
            with self._conn() as c:
                cur = c.execute(
                    """
                    INSERT OR IGNORE INTO availability_subs
                        (telegram_id, media_type, tmdb_id, title)
                    VALUES (?, ?, ?, ?)
                    """,
                    (telegram_id, media_type, tmdb_id, title),
                )
                return cur.rowcount > 0

        return await self._run(_do)

    async def set_subscription_label(self, telegram_id: int, media_type: str,
                                     tmdb_id: int, title: str) -> None:
        """Replace a subscription's display label (set after the add, once
        the title lookup comes back)."""

        def _do() -> None:
            with self._conn() as c:
                c.execute(
                    """
                    UPDATE availability_subs SET title = ?
                    WHERE telegram_id = ? AND media_type = ? AND tmdb_id = ?
                    """,
                    (title, telegram_id, media_type, tmdb_id),
                )

        await self._run(_do)

    async def remove_subscription(self, subscription_id: int, telegram_id: int) -> bool:
        """Delete one subscription -- only the owner's (the telegram_id
        predicate makes a forged subdel callback a no-op)."""

        def _do() -> bool:
            with self._conn() as c:
                cur = c.execute(
                    "DELETE FROM availability_subs WHERE id = ? AND telegram_id = ?",
                    (subscription_id, telegram_id),
                )
                return cur.rowcount > 0

        return await self._run(_do)

    async def list_subscriptions(self, telegram_id: int) -> list[tuple]:
        """A user's own subscriptions: [(id, media_type, tmdb_id, title)]."""

        def _do() -> list[tuple]:
            with self._conn() as c:
                return c.execute(
                    """
                    SELECT id, media_type, tmdb_id, title
                    FROM availability_subs WHERE telegram_id = ?
                    ORDER BY id
                    """,
                    (telegram_id,),
                ).fetchall()

        return await self._run(_do)

    async def pop_subscribers(self, media_type: str, tmdb_id: int) -> list[int]:
        """All telegram_ids subscribed to a title, deleting the rows in the
        same transaction (one-shot semantics: re-adds, auto-fix
        replacements, and per-episode rescans can re-fire MEDIA_AVAILABLE,
        and a consumed subscription must not fire twice)."""

        def _do() -> list[int]:
            with self._conn() as c:
                rows = c.execute(
                    """
                    SELECT DISTINCT telegram_id FROM availability_subs
                    WHERE media_type = ? AND tmdb_id = ?
                    """,
                    (media_type, tmdb_id),
                ).fetchall()
                c.execute(
                    "DELETE FROM availability_subs WHERE media_type = ? AND tmdb_id = ?",
                    (media_type, tmdb_id),
                )
                return [r[0] for r in rows]

        return await self._run(_do)

    async def unlink(self, telegram_id: int) -> bool:
        def _do() -> bool:
            with self._conn() as c:
                cur = c.execute(
                    "DELETE FROM user_mapping WHERE telegram_id = ?",
                    (telegram_id,),
                )
                return cur.rowcount > 0

        return await self._run(_do)

    async def get(self, telegram_id: int) -> Optional[Link]:
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
        return Link(
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

    # Auto-fix rate limiting

    async def count_autofix_24h(self, telegram_id: int) -> int:
        def _do() -> int:
            with self._conn() as c:
                row = c.execute(
                    """
                    SELECT COUNT(*) FROM autofix_events
                    WHERE telegram_id = ?
                      AND occurred_at >= datetime('now', '-24 hours')
                      AND refunded = 0
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
        kind: str = KIND_AUTO_FIX,
    ) -> int:
        """Record a Fix event; returns its id so the caller can tie it to
        the pending fix row (add_pending_autofix event_id=). kind is
        'auto-fix', 'redownload' or 'mark failed'."""
        def _do() -> int:
            with self._conn() as c:
                return c.execute(
                    """
                    INSERT INTO autofix_events
                        (telegram_id, media_type, tmdb_id, season, episode, kind)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (telegram_id, media_type, tmdb_id, season, episode, kind),
                ).lastrowid

        return await self._run(_do)

    async def recent_fix_event(
        self,
        media_type: str,
        tmdb_id: int,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> Optional[RecentFixEvent]:
        """The newest Fix event on this item from anyone in the last 30
        days: a movie by tmdb_id; an episode by tmdb_id, season and episode.
        None when there is none."""
        def _do() -> Optional[RecentFixEvent]:
            with self._conn() as c:
                row = c.execute(
                    """
                    SELECT kind, outcome,
                           CAST((julianday('now') - julianday(occurred_at)) * 86400
                                AS INTEGER)
                    FROM autofix_events
                    WHERE media_type = ? AND tmdb_id = ?
                      AND season IS ? AND episode IS ?
                      AND occurred_at >= datetime('now', ?)
                    ORDER BY occurred_at DESC, id DESC LIMIT 1
                    """,
                    (media_type, tmdb_id, season, episode,
                     f"-{AUTOFIX_HISTORY_DAYS} days"),
                ).fetchone()
            return RecentFixEvent(row[0], row[1], max(0, row[2])) if row else None

        return await self._run(_do)

    async def mark_autofix_repeat(self, pending_id: int, *, refunded: bool) -> None:
        """The Fix tied to this pending row came back as a repeat: outcome
        'repeat', and refunded when the Reporter gets the use back (the
        24-hour count then skips the event)."""
        def _do():
            with self._conn() as c:
                c.execute(
                    "UPDATE autofix_events SET outcome = ?, refunded = ? "
                    "WHERE pending_id = ?",
                    (OUTCOME_REPEAT, 1 if refunded else 0, pending_id),
                )

        await self._run(_do)

    # Pending autofix tracking

    async def add_pending_autofix(
        self,
        *,
        chat_id: int,
        user_id: int,
        media_type: str,
        label: str,
        ticket_id: int,
        ticket_url: str,
        # Single source of truth: no caller passes this, so the
        # const IS the effective timeout, and the DM text derives from the
        # same value instead of lying when it's tuned.
        timeout_hours: int = AUTOFIX_TIMEOUT_HOURS,
        radarr_movie_id: Optional[int] = None,
        sonarr_series_id: Optional[int] = None,
        sonarr_episode_id: Optional[int] = None,
        # sonarr_season / expected_episode_ids: no production caller passes
        # these anymore (the whole-season workflow was removed in 0.12.0), but
        # the columns are deliberately retained for a future re-wiring, so the
        # params stay to populate them. Only tests exercise them today.
        sonarr_season: Optional[int] = None,
        expected_episode_ids: Optional[list[int]] = None,
        message_id: Optional[int] = None,
        action_name: str = "",
        before_fingerprint: Optional[ReleaseFingerprint] = None,
        event_id: Optional[int] = None,
        auto_blocklisted: bool = False,
    ) -> int:
        """event_id: the autofix_events row this Fix logged; it is tied to
        the new pending row so the poller can record the outcome on it.
        auto_blocklisted: the bot's own Mark Failed after a repeat (the
        blocklist-on-repeat switch); a row so marked never triggers it."""
        fingerprint_json = before_fingerprint.to_json() if before_fingerprint else ""

        def _do() -> int:
            with self._conn() as c:
                cur = c.execute(
                    """
                    INSERT INTO pending_autofixes (
                        chat_id, user_id, media_type,
                        radarr_movie_id, sonarr_series_id, sonarr_episode_id, sonarr_season,
                        expected_episode_ids, label, issue_id, issue_url,
                        message_id, action_name, before_fingerprint, auto_blocklisted,
                        timeout_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', ?))
                    """,
                    (
                        chat_id, user_id, media_type,
                        radarr_movie_id, sonarr_series_id, sonarr_episode_id, sonarr_season,
                        json.dumps(expected_episode_ids or []), label, ticket_id, ticket_url,
                        message_id, action_name, fingerprint_json,
                        1 if auto_blocklisted else 0, f"+{timeout_hours} hours",
                    ),
                )
                if event_id is not None:
                    c.execute(
                        "UPDATE autofix_events SET pending_id = ? WHERE id = ?",
                        (cur.lastrowid, event_id),
                    )
                return cur.lastrowid

        return await self._run(_do)

    _PENDING_COLUMNS = """id, chat_id, user_id, media_type, radarr_movie_id,
                           sonarr_series_id, sonarr_episode_id, sonarr_season,
                           expected_episode_ids, label, issue_id, issue_url,
                           started_at, timeout_at, message_id, last_progress,
                           bumped, reply_markup, action_name, before_fingerprint,
                           auto_blocklisted"""

    @staticmethod
    def _pending_from_row(r) -> PendingAutofix:
        ids = json.loads(r[8] or "[]")
        if not isinstance(ids, list):
            raise ValueError(f"expected_episode_ids is {type(ids).__name__}, not list")
        return PendingAutofix(
            id=r[0], chat_id=r[1], user_id=r[2], media_type=r[3],
            radarr_movie_id=r[4], sonarr_series_id=r[5],
            sonarr_episode_id=r[6], sonarr_season=r[7],
            expected_episode_ids=ids,
            label=r[9], ticket_id=r[10], ticket_url=r[11],
            started_at=r[12], timeout_at=r[13],
            message_id=r[14], last_progress=r[15] or "",
            bumped=r[16] or 0,
            reply_markup=r[17] or "",
            action_name=r[18] or "",
            before_fingerprint=ReleaseFingerprint.from_json(r[19]),
            auto_blocklisted=r[20] or 0,
        )

    async def list_pending_autofixes(self) -> list[PendingAutofix]:
        def _do() -> list[tuple]:
            with self._conn() as c:
                return c.execute(
                    f"""
                    SELECT {self._PENDING_COLUMNS}
                    FROM pending_autofixes
                    WHERE status = ?
                    ORDER BY id
                    """,
                    (OUTCOME_PENDING,),
                ).fetchall()

        rows = await self._run(_do)
        # One bad item must not kill the batch: a single corrupt row (garbage
        # JSON in expected_episode_ids, wrong types) previously raised here
        # on every poll tick, permanently stopping ALL completion/timeout DMs
        # for everyone. Skip and log the bad row; process the rest.
        out: list[PendingAutofix] = []
        for r in rows:
            try:
                out.append(self._pending_from_row(r))
            except Exception as exc:
                logger.exception("Skipping corrupt pending_autofix row id=%s", r[0], exc_info=exc)
        return out

    async def latest_autofix_for_ticket(self, ticket_id: int) -> Optional[PendingAutofix]:
        """The newest Fix row for a Ticket in any state (the row outlives its
        poll for 7 days), or None when there is none or it is unreadable."""
        def _do():
            with self._conn() as c:
                return c.execute(
                    f"""
                    SELECT {self._PENDING_COLUMNS}
                    FROM pending_autofixes
                    WHERE issue_id = ?
                    ORDER BY id DESC LIMIT 1
                    """, (ticket_id,)).fetchone()

        r = await self._run(_do)
        if r is None:
            return None
        try:
            return self._pending_from_row(r)
        except Exception as exc:
            logger.exception("corrupt pending_autofix row id=%s", r[0], exc_info=exc)
            return None

    async def claim_ticket_escalation(self, ticket_id: int) -> bool:
        """Record that the Reporter asked the Admin about this Ticket.
        True for the first claim, False when it was already made."""
        def _do() -> bool:
            with self._conn() as c:
                return c.execute(
                    "INSERT OR IGNORE INTO ticket_escalations (ticket_id) VALUES (?)",
                    (ticket_id,)).rowcount == 1

        return await self._run(_do)

    async def release_ticket_escalation(self, ticket_id: int) -> None:
        """Undo a claim whose DM never reached the Admin, so a retap can retry."""
        def _do():
            with self._conn() as c:
                c.execute("DELETE FROM ticket_escalations WHERE ticket_id = ?",
                          (ticket_id,))

        await self._run(_do)

    async def set_autofix_message(self, pending_id: int, message_id: int,
                                  reply_markup: str = "") -> None:
        """Attach the message a fix's Progress card lives on,
        with its keyboard as JSON ("" for none) so progress edits keep it."""

        def _do():
            with self._conn() as c:
                c.execute(
                    "UPDATE pending_autofixes SET message_id = ?, "
                    "reply_markup = ? WHERE id = ?",
                    (message_id, reply_markup, pending_id))

        await self._run(_do)

    async def set_autofix_progress(self, pending_id: int,
                                   last_progress: str) -> None:
        """Remember the last rendered progress line (edit dedupe)."""

        def _do():
            with self._conn() as c:
                c.execute(
                    "UPDATE pending_autofixes SET last_progress = ? WHERE id = ?",
                    (last_progress, pending_id))

        await self._run(_do)

    async def mark_autofix_bumped(self, pending_id: int) -> None:
        def _do():
            with self._conn() as c:
                c.execute(
                    "UPDATE pending_autofixes SET bumped = 1 WHERE id = ?",
                    (pending_id,))

        await self._run(_do)

    async def mark_autofix_status(self, pending_id: int, status: str) -> None:
        """status: 'complete', 'timeout', or 'failed'."""
        def _do():
            with self._conn() as c:
                c.execute(
                    "UPDATE pending_autofixes SET status = ? WHERE id = ?",
                    (status, pending_id),
                )
                # The tied event ends the same way (a repeat is recorded
                # after, by mark_autofix_repeat).
                c.execute(
                    "UPDATE autofix_events SET outcome = ? WHERE pending_id = ?",
                    (status, pending_id),
                )

        await self._run(_do)

    async def prune_autofix_history(self) -> tuple[int, int]:
        """Delete rows nothing reads anymore, so neither table grows for the
        life of the install: pending_autofixes rows in a terminal state
        (only 'pending' is ever polled) older than 7 days, and
        autofix_events older than 30 days (count_autofix_24h looks back 24h;
        the rest is kept for later lookups by item), and "Ask the Admin"
        claims older than TICKET_ESCALATION_KEEP_DAYS. Returns
        (pending_deleted, events_deleted).
        Called at startup and daily by the bot's job queue."""
        def _do() -> tuple[int, int]:
            with self._conn() as c:
                pending = c.execute(
                    """
                    DELETE FROM pending_autofixes
                    WHERE status IN (?, ?, ?)
                      AND started_at < datetime('now', '-7 days')
                    """,
                    (OUTCOME_COMPLETE, OUTCOME_TIMEOUT, OUTCOME_FAILED),
                ).rowcount
                events = c.execute(
                    "DELETE FROM autofix_events WHERE occurred_at < datetime('now', ?)",
                    (f"-{AUTOFIX_HISTORY_DAYS} days",),
                ).rowcount
                # Escalations are keyed by Seerr's ticket id alone; a new
                # Seerr install reuses ids, so an old claim must not block
                # "Ask the Admin" on a new Ticket forever.
                c.execute(
                    "DELETE FROM ticket_escalations WHERE escalated_at < datetime('now', ?)",
                    (f"-{TICKET_ESCALATION_KEEP_DAYS} days",),
                )
                return pending, events

        return await self._run(_do)
