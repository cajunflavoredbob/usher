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

from const import (ACCESS_ASK_IGNORE_DAYS, ACCESS_ASK_KEEP_DAYS,
                   BROADCAST_KEEP_DAYS,
                   ACCESS_ASK_MAX_OPEN, AUTOFIX_TIMEOUT_HOURS)
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
class AccessAsk:
    """A stranger's request for an Invite (see GLOSSARY: Access ask)."""
    id: int
    telegram_id: int
    telegram_name: str
    plex_username: str
    plex_email: str
    status: str                       # open, approved or ignored
    admin_message_id: Optional[int]


@dataclass(frozen=True)
class Broadcast:
    """An Admin Broadcast (see GLOSSARY: Broadcast)."""
    id: int
    text: str
    chat_id: int
    message_id: Optional[int]
    status: str                       # sending or done


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
            # Service alarms: one row per active problem. `key` names it
            # (kind:service:detail), `text` is what the Admin was told, and
            # `notified` is 1 once the "new problem" DM went out.
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS alarm_state (
                    key TEXT PRIMARY KEY,
                    text TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT (datetime('now')),
                    notified INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # Access asks: one row per Telegram user (a stranger asking the
            # Admin for an Invite). status is open, approved or ignored.
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS access_asks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL UNIQUE,
                    telegram_name TEXT NOT NULL DEFAULT '',
                    plex_username TEXT NOT NULL DEFAULT '',
                    plex_email TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    decided_at TEXT,
                    admin_message_id INTEGER
                )
                """
            )
            # Admin Broadcasts: one row per message, one recipient row per
            # Linked user it goes to, so a restart resumes without repeats.
            # status: sending or done (a broadcast); pending, sent or failed
            # (a recipient). chat_id and message_id name the preview message
            # the final report edits.
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS broadcasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'sending',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS broadcast_recipients (
                    broadcast_id INTEGER NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    PRIMARY KEY (broadcast_id, telegram_id)
                )
                """
            )
            # Follows: a standing ask to be told about each new episode of a
            # show. One row per user and show; removed = 1 after Unfollow so
            # a later automatic follow can tell "never followed" from
            # "followed and removed". tvdb_id 0 = unknown.
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS follows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    tmdb_id INTEGER NOT NULL,
                    tvdb_id INTEGER NOT NULL DEFAULT 0,
                    title TEXT NOT NULL DEFAULT '',
                    auto INTEGER NOT NULL DEFAULT 0,
                    removed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(telegram_id, tmdb_id)
                )
                """
            )
            c.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_follows_show
                ON follows(tmdb_id)
                """
            )
            # New content: one row per import the poller saw. media_type is
            # movie or episode; for an episode tmdb_id and title name the
            # show. source_key ("radarr:<history id>") makes a record count
            # once however often it is read; pinged = 1 once the Follow
            # pings for the row were handled.
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS new_content (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_key TEXT NOT NULL UNIQUE,
                    media_type TEXT NOT NULL,
                    tmdb_id INTEGER NOT NULL,
                    tvdb_id INTEGER NOT NULL DEFAULT 0,
                    title TEXT NOT NULL DEFAULT '',
                    season INTEGER,
                    episode INTEGER,
                    added_at TEXT NOT NULL DEFAULT (datetime('now')),
                    pinged INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # Where each poller source left off (an ISO time).
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS poller_marks (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
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

    # Follows

    async def add_follow(self, telegram_id: int, tmdb_id: int, tvdb_id: int,
                         title: str, auto: bool = False) -> bool:
        """Follow a show. True when the Follow is newly active (a new row,
        or a removed one switched back on); False when already active."""
        def _do() -> bool:
            with self._conn() as c:
                cur = c.execute(
                    """
                    INSERT OR IGNORE INTO follows
                        (telegram_id, tmdb_id, tvdb_id, title, auto)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (telegram_id, tmdb_id, tvdb_id or 0, title, int(auto)))
                if cur.rowcount > 0:
                    return True
                cur = c.execute(
                    """
                    UPDATE follows SET removed = 0, auto = ?
                    WHERE telegram_id = ? AND tmdb_id = ? AND removed = 1
                    """,
                    (int(auto), telegram_id, tmdb_id))
                return cur.rowcount > 0
        return await self._run(_do)

    async def set_follow_info(self, telegram_id: int, tmdb_id: int,
                              tvdb_id: int, title: str) -> None:
        """Fill in a Follow's title and TVDb id once the lookup comes back."""
        def _do() -> None:
            with self._conn() as c:
                c.execute(
                    """
                    UPDATE follows SET title = ?, tvdb_id = ?
                    WHERE telegram_id = ? AND tmdb_id = ?
                    """,
                    (title, tvdb_id or 0, telegram_id, tmdb_id))
        await self._run(_do)

    async def remove_follow(self, telegram_id: int, tmdb_id: int) -> bool:
        """Unfollow: sets the removed flag. False when there was no active
        Follow."""
        def _do() -> bool:
            with self._conn() as c:
                cur = c.execute(
                    """
                    UPDATE follows SET removed = 1
                    WHERE telegram_id = ? AND tmdb_id = ? AND removed = 0
                    """,
                    (telegram_id, tmdb_id))
                return cur.rowcount > 0
        return await self._run(_do)

    async def remove_follow_by_id(self, follow_id: int,
                                  telegram_id: int) -> bool:
        """Unfollow by row id, only the owner's (a forged id is a no-op)."""
        def _do() -> bool:
            with self._conn() as c:
                cur = c.execute(
                    """
                    UPDATE follows SET removed = 1
                    WHERE id = ? AND telegram_id = ? AND removed = 0
                    """,
                    (follow_id, telegram_id))
                return cur.rowcount > 0
        return await self._run(_do)

    async def is_following(self, telegram_id: int, tmdb_id: int) -> bool:
        def _do() -> bool:
            with self._conn() as c:
                return c.execute(
                    """
                    SELECT 1 FROM follows
                    WHERE telegram_id = ? AND tmdb_id = ? AND removed = 0
                    """,
                    (telegram_id, tmdb_id)).fetchone() is not None
        return await self._run(_do)

    async def list_follows(self, telegram_id: int) -> list[tuple]:
        """A user's active Follows: [(id, tmdb_id, title)]."""
        def _do() -> list[tuple]:
            with self._conn() as c:
                return c.execute(
                    """
                    SELECT id, tmdb_id, title FROM follows
                    WHERE telegram_id = ? AND removed = 0 ORDER BY id
                    """,
                    (telegram_id,)).fetchall()
        return await self._run(_do)

    async def list_followers(self, tmdb_id: int) -> list[int]:
        """Telegram ids with an active Follow on a show."""
        def _do() -> list[int]:
            with self._conn() as c:
                return [r[0] for r in c.execute(
                    """
                    SELECT telegram_id FROM follows
                    WHERE tmdb_id = ? AND removed = 0 ORDER BY id
                    """,
                    (tmdb_id,)).fetchall()]
        return await self._run(_do)

    # New-content poller

    async def get_poller_mark(self, key: str) -> Optional[str]:
        def _do() -> Optional[str]:
            with self._conn() as c:
                row = c.execute("SELECT value FROM poller_marks WHERE key = ?",
                                (key,)).fetchone()
                return row[0] if row else None
        return await self._run(_do)

    async def set_poller_mark(self, key: str, value: str) -> None:
        def _do() -> None:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO poller_marks (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value))
        await self._run(_do)

    async def add_new_content(self, source_key: str, media_type: str,
                              tmdb_id: int, title: str,
                              season: Optional[int] = None,
                              episode: Optional[int] = None,
                              tvdb_id: int = 0,
                              added_at: Optional[str] = None) -> bool:
        """Record one import. False when this source record was already
        recorded, or (for an episode) the same episode already has a row, so
        an upgrade import never pings twice."""
        def _do() -> bool:
            with self._conn() as c:
                if media_type == "episode" and c.execute(
                        """
                        SELECT 1 FROM new_content WHERE media_type = 'episode'
                        AND tmdb_id = ? AND season = ? AND episode = ?
                        """,
                        (tmdb_id, season, episode)).fetchone():
                    return False
                cur = c.execute(
                    """
                    INSERT OR IGNORE INTO new_content
                        (source_key, media_type, tmdb_id, tvdb_id, title,
                         season, episode, added_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))
                    """,
                    (source_key, media_type, tmdb_id, tvdb_id or 0, title,
                     season, episode, added_at))
                return cur.rowcount > 0
        return await self._run(_do)

    async def list_unpinged_episodes(self) -> list[tuple]:
        """Episode rows whose Follow pings are not handled yet:
        [(id, tmdb_id, title, season, episode)] oldest first."""
        def _do() -> list[tuple]:
            with self._conn() as c:
                return c.execute(
                    """
                    SELECT id, tmdb_id, title, season, episode FROM new_content
                    WHERE media_type = 'episode' AND pinged = 0 ORDER BY id
                    """).fetchall()
        return await self._run(_do)

    async def mark_pinged(self, ids: list[int]) -> None:
        def _do() -> None:
            with self._conn() as c:
                c.executemany("UPDATE new_content SET pinged = 1 WHERE id = ?",
                              [(i,) for i in ids])
        await self._run(_do)

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

    # Service alarms

    async def list_alarms(self) -> list[tuple[str, str, bool]]:
        """Every active alarm as (key, text, notified)."""
        def _do():
            with self._conn() as c:
                return [(r[0], r[1], bool(r[2])) for r in c.execute(
                    "SELECT key, text, notified FROM alarm_state ORDER BY started_at, key")]
        return await self._run(_do)

    async def add_alarm(self, key: str, text: str) -> None:
        """Start tracking a problem (not yet notified). A key already
        tracked keeps its start time and notified flag."""
        def _do():
            with self._conn() as c:
                c.execute(
                    "INSERT OR IGNORE INTO alarm_state (key, text) VALUES (?, ?)",
                    (key, text))
        await self._run(_do)

    async def mark_alarm_notified(self, key: str) -> None:
        def _do():
            with self._conn() as c:
                c.execute("UPDATE alarm_state SET notified = 1 WHERE key = ?", (key,))
        await self._run(_do)

    async def delete_alarm(self, key: str) -> None:
        def _do():
            with self._conn() as c:
                c.execute("DELETE FROM alarm_state WHERE key = ?", (key,))
        await self._run(_do)

    # Access asks

    @staticmethod
    def _ask_from_row(r) -> "AccessAsk":
        return AccessAsk(id=r[0], telegram_id=r[1], telegram_name=r[2],
                         plex_username=r[3], plex_email=r[4], status=r[5],
                         admin_message_id=r[6])

    _ASK_COLS = ("id, telegram_id, telegram_name, plex_username, plex_email, "
                 "status, admin_message_id")

    async def create_access_ask(self, telegram_id: int, telegram_name: str,
                                plex_username: str, plex_email: str
                                ) -> tuple[Optional[int], str]:
        """Store a new open ask. Returns (id, "") or (None, reason) with
        reason "open" (this person already has an open ask), "ignored"
        (ignored within ACCESS_ASK_IGNORE_DAYS) or "full" (ACCESS_ASK_MAX_OPEN
        asks are open). An approved or long-ago ignored ask is replaced."""
        def _do() -> tuple[Optional[int], str]:
            with self._conn() as c:
                row = c.execute(
                    "SELECT status, decided_at > datetime('now', ?) "
                    "FROM access_asks WHERE telegram_id = ?",
                    (f"-{ACCESS_ASK_IGNORE_DAYS} days", telegram_id)).fetchone()
                if row is not None:
                    if row[0] == "open":
                        return None, "open"
                    if row[0] == "ignored" and row[1]:
                        return None, "ignored"
                open_count = c.execute(
                    "SELECT COUNT(*) FROM access_asks WHERE status = 'open'"
                ).fetchone()[0]
                if open_count >= ACCESS_ASK_MAX_OPEN:
                    return None, "full"
                c.execute("DELETE FROM access_asks WHERE telegram_id = ?",
                          (telegram_id,))
                cur = c.execute(
                    "INSERT INTO access_asks (telegram_id, telegram_name, "
                    "plex_username, plex_email) VALUES (?, ?, ?, ?)",
                    (telegram_id, telegram_name, plex_username, plex_email))
                return cur.lastrowid, ""
        return await self._run(_do)

    async def get_access_ask(self, ask_id: int) -> Optional["AccessAsk"]:
        def _do():
            with self._conn() as c:
                return c.execute(
                    f"SELECT {self._ASK_COLS} FROM access_asks WHERE id = ?",
                    (ask_id,)).fetchone()
        row = await self._run(_do)
        return None if row is None else self._ask_from_row(row)

    async def set_access_ask_message(self, ask_id: int, message_id: int) -> None:
        def _do():
            with self._conn() as c:
                c.execute("UPDATE access_asks SET admin_message_id = ? "
                          "WHERE id = ?", (message_id, ask_id))
        await self._run(_do)

    async def decide_access_ask(self, ask_id: int, status: str) -> bool:
        """Move an OPEN ask to `status` (approved or ignored). False when it
        was no longer open, so a second tap does nothing."""
        def _do() -> bool:
            with self._conn() as c:
                return c.execute(
                    "UPDATE access_asks SET status = ?, "
                    "decided_at = datetime('now') "
                    "WHERE id = ? AND status = 'open'",
                    (status, ask_id)).rowcount > 0
        return await self._run(_do)

    async def delete_access_ask(self, ask_id: int) -> None:
        def _do():
            with self._conn() as c:
                c.execute("DELETE FROM access_asks WHERE id = ?", (ask_id,))
        await self._run(_do)

    # Admin Broadcasts

    async def list_linked_ids(self) -> list[int]:
        """Telegram ids of every Linked user (a row in user_mapping)."""
        def _do() -> list[int]:
            with self._conn() as c:
                return [r[0] for r in c.execute(
                    "SELECT telegram_id FROM user_mapping "
                    "ORDER BY telegram_id").fetchall()]
        return await self._run(_do)

    async def create_broadcast(self, text: str, chat_id: int,
                               message_id: Optional[int],
                               recipient_ids: list[int]) -> int:
        """Store a broadcast and its recipients (all pending) in one
        transaction. Returns its id."""
        def _do() -> int:
            with self._conn() as c:
                bid = c.execute(
                    "INSERT INTO broadcasts (text, chat_id, message_id) "
                    "VALUES (?, ?, ?)", (text, chat_id, message_id)).lastrowid
                c.executemany(
                    "INSERT OR IGNORE INTO broadcast_recipients "
                    "(broadcast_id, telegram_id) VALUES (?, ?)",
                    [(bid, tid) for tid in recipient_ids])
                return bid
        return await self._run(_do)

    async def get_broadcast(self, broadcast_id: int) -> Optional["Broadcast"]:
        def _do():
            with self._conn() as c:
                return c.execute(
                    "SELECT id, text, chat_id, message_id, status "
                    "FROM broadcasts WHERE id = ?", (broadcast_id,)).fetchone()
        row = await self._run(_do)
        return None if row is None else Broadcast(*row)

    async def unfinished_broadcast_ids(self) -> list[int]:
        def _do() -> list[int]:
            with self._conn() as c:
                return [r[0] for r in c.execute(
                    "SELECT id FROM broadcasts WHERE status = 'sending' "
                    "ORDER BY id").fetchall()]
        return await self._run(_do)

    async def pending_broadcast_recipients(self, broadcast_id: int,
                                           limit: int = 50) -> list[int]:
        def _do() -> list[int]:
            with self._conn() as c:
                return [r[0] for r in c.execute(
                    "SELECT telegram_id FROM broadcast_recipients "
                    "WHERE broadcast_id = ? AND status = 'pending' "
                    "ORDER BY telegram_id LIMIT ?",
                    (broadcast_id, limit)).fetchall()]
        return await self._run(_do)

    async def mark_broadcast_recipient(self, broadcast_id: int,
                                       telegram_id: int, status: str) -> None:
        """status is sent or failed."""
        def _do():
            with self._conn() as c:
                c.execute(
                    "UPDATE broadcast_recipients SET status = ? "
                    "WHERE broadcast_id = ? AND telegram_id = ?",
                    (status, broadcast_id, telegram_id))
        await self._run(_do)

    async def broadcast_counts(self, broadcast_id: int) -> dict[str, int]:
        """{"pending": n, "sent": n, "failed": n} for one broadcast."""
        def _do():
            with self._conn() as c:
                return c.execute(
                    "SELECT status, COUNT(*) FROM broadcast_recipients "
                    "WHERE broadcast_id = ? GROUP BY status",
                    (broadcast_id,)).fetchall()
        counts = {"pending": 0, "sent": 0, "failed": 0}
        counts.update(dict(await self._run(_do)))
        return counts

    async def finish_broadcast(self, broadcast_id: int) -> bool:
        """Move a sending broadcast to done. False when it already was, so
        its report goes out once."""
        def _do() -> bool:
            with self._conn() as c:
                return c.execute(
                    "UPDATE broadcasts SET status = 'done' "
                    "WHERE id = ? AND status = 'sending'",
                    (broadcast_id,)).rowcount > 0
        return await self._run(_do)

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
                # Access asks older than ACCESS_ASK_KEEP_DAYS, in any state
                # (not counted in the return value).
                c.execute(
                    "DELETE FROM access_asks WHERE created_at < datetime('now', ?)",
                    (f"-{ACCESS_ASK_KEEP_DAYS} days",),
                )
                # Finished broadcasts older than BROADCAST_KEEP_DAYS, with
                # their recipients (not counted in the return value).
                old = f"-{BROADCAST_KEEP_DAYS} days"
                c.execute(
                    "DELETE FROM broadcast_recipients WHERE broadcast_id IN "
                    "(SELECT id FROM broadcasts WHERE status = 'done' "
                    "AND created_at < datetime('now', ?))", (old,))
                c.execute(
                    "DELETE FROM broadcasts WHERE status = 'done' "
                    "AND created_at < datetime('now', ?)", (old,))
                # New content older than 30 days (not counted either).
                c.execute(
                    "DELETE FROM new_content "
                    "WHERE added_at < datetime('now', '-30 days')")
                return pending, events

        return await self._run(_do)
