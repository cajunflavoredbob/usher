"""Settings management for Usher.

settings.json under /data is the source of truth. Env vars seed it on
first run, then become inert.

Includes admin password helpers (pbkdf2_sha256, stdlib-only) and the
session-secret loader used by the webui.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from dataclasses import asdict, dataclass, field
from hashlib import pbkdf2_hmac
from hmac import compare_digest
from pathlib import Path
from typing import Optional

from fsutil import atomic_write_bytes

logger = logging.getLogger("usher.settings")

PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16


def parse_id_list(values) -> list[int]:
    """Coerce an iterable of Telegram-id-ish values to ints, dropping junk.
    One parser for all three allowlist writers (settings.json load, env seed,
    web form) so their semantics can't drift; tolerates a leading '-' since
    hand-edited files have carried negative ids."""
    out: list[int] = []
    for v in values:
        v = str(v).strip()
        if v.lstrip("-").isdigit():
            out.append(int(v))
    return out


@dataclass
class AdminAccount:
    username: str = ""
    # Format: pbkdf2_sha256$<iter>$<salt_hex>$<hash_hex>
    password_hash: str = ""
    # Bumped on every password change; session cookies embed the value they
    # were minted with, so a bump invalidates all outstanding sessions
    # (a stolen 7-day cookie survived a password rotation).
    password_version: int = 0

    def is_set(self) -> bool:
        return bool(self.username and self.password_hash)


DEFAULT_DAILY_AUTOFIX_LIMIT = 3


@dataclass
class Settings:
    telegram_bot_token: str = ""
    admin_telegram_id: int = 0
    usher_public_url: str = ""
    seerr_url: str = ""
    seerr_api_key: str = ""
    seerr_public_url: str = ""
    radarr_url: str = ""
    radarr_api_key: str = ""
    sonarr_url: str = ""
    sonarr_api_key: str = ""
    sabnzbd_url: str = ""
    sabnzbd_api_key: str = ""
    # Queue-priority boost for downloads that originate from this bot
    # (requests + auto-fixes): "off", "high", or "force".
    sabnzbd_boost: str = "off"
    # Bot DM classes, all on by default. Admin-level switches: turning one
    # off silences that class for everyone.
    tg_notify_requester: bool = True      # approved/declined/available/failed to requesters
    tg_notify_admin_requests: bool = True  # new-request + auto-approved FYIs to the admin
    tg_notify_admin_failed: bool = True    # failed-download alarm to the admin
    tg_notify_issues: bool = True          # issue reported/comment/resolved DMs
    tg_notify_subscriptions: bool = True   # availability-watch fan-out
    tg_progress_cards: bool = True         # morphing request/auto-fix cards
    allowed_autofix_telegram_ids: list[int] = field(default_factory=list)
    # When True, every linked user may auto-fix regardless of the allowlist
    # (the admin is always allowed). The list above is retained either way.
    autofix_allow_all: bool = False
    daily_autofix_limit: int = DEFAULT_DAILY_AUTOFIX_LIMIT
    # When True, the per-user daily cap is not enforced. The numeric limit
    # above is retained for when this is turned back off.
    daily_autofix_unlimited: bool = False
    webhook_secret: str = ""
    # Admin dismissed the "Seerr's New Plex Sign-In is enabled" banner.
    # Cleared automatically whenever a check observes the setting OFF, so
    # the warning re-arms only on an off -> on transition.
    seerr_new_plex_login_ack: bool = False
    admin: AdminAccount = field(default_factory=AdminAccount)

    def to_dict(self) -> dict:
        return asdict(self)

    def is_bot_configured(self) -> bool:
        """True iff the irreducible-minimum fields to run the Telegram bot are set."""
        return bool(self.telegram_bot_token and self.admin_telegram_id)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        admin_data = data.get("admin") or {}
        try:
            admin_tg_id = int(data.get("admin_telegram_id") or 0)
        except (TypeError, ValueError):
            admin_tg_id = 0
        try:
            daily_limit = int(data.get("daily_autofix_limit") or DEFAULT_DAILY_AUTOFIX_LIMIT)
        except (TypeError, ValueError):
            daily_limit = DEFAULT_DAILY_AUTOFIX_LIMIT
        if daily_limit < 1:
            daily_limit = DEFAULT_DAILY_AUTOFIX_LIMIT
        return cls(
            telegram_bot_token=data.get("telegram_bot_token", "") or "",
            admin_telegram_id=admin_tg_id,
            # settings.json written before the rename stores the old key;
            # keep reading it so an in-place upgrade preserves the URL.
            usher_public_url=(data.get("usher_public_url")
                              or data.get("hermes_public_url") or ""),
            seerr_url=data.get("seerr_url", "") or "",
            seerr_api_key=data.get("seerr_api_key", "") or "",
            seerr_public_url=data.get("seerr_public_url", "") or "",
            radarr_url=data.get("radarr_url", "") or "",
            sabnzbd_url=data.get("sabnzbd_url", "") or "",
            sabnzbd_api_key=data.get("sabnzbd_api_key", "") or "",
            sabnzbd_boost=(data.get("sabnzbd_boost") or "off")
            if (data.get("sabnzbd_boost") or "off") in ("off", "high", "force")
            else "off",
            tg_notify_requester=bool(data.get("tg_notify_requester", True)),
            tg_notify_admin_requests=bool(data.get("tg_notify_admin_requests", True)),
            tg_notify_admin_failed=bool(data.get("tg_notify_admin_failed", True)),
            tg_notify_issues=bool(data.get("tg_notify_issues", True)),
            tg_notify_subscriptions=bool(data.get("tg_notify_subscriptions", True)),
            tg_progress_cards=bool(data.get("tg_progress_cards", True)),
            radarr_api_key=data.get("radarr_api_key", "") or "",
            sonarr_url=data.get("sonarr_url", "") or "",
            sonarr_api_key=data.get("sonarr_api_key", "") or "",
            # Coerce to int and drop junk: hand-edited string ids used to
            # pass through untouched and silently never match the int compare.
            allowed_autofix_telegram_ids=parse_id_list(
                data.get("allowed_autofix_telegram_ids") or []),
            autofix_allow_all=bool(data.get("autofix_allow_all")),
            daily_autofix_limit=daily_limit,
            daily_autofix_unlimited=bool(data.get("daily_autofix_unlimited")),
            webhook_secret=data.get("webhook_secret", "") or "",
            seerr_new_plex_login_ack=bool(data.get("seerr_new_plex_login_ack")),
            admin=AdminAccount(
                username=admin_data.get("username", "") or "",
                password_hash=admin_data.get("password_hash", "") or "",
                password_version=int(admin_data.get("password_version") or 0),
            ),
        )


class SettingsStore:
    """Loads/persists Settings JSON. Seeds from env on first run."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.settings = self._load_or_seed()

    def _load_or_seed(self) -> Settings:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                logger.info("Loaded settings from %s", self.path)
                s = Settings.from_dict(data)
            except Exception:
                # An existing-but-unreadable file is an anomaly, not a fresh
                # install: it usually means an unsafe shutdown truncated the
                # last write (see _write). Preserve the corrupt bytes -- never
                # silently destroy the admin password hash / webhook secret /
                # autofix allowlist -- before reseeding from env. Loud ERROR so
                # the operator can recover values from the sidecar.
                backup = self._preserve_corrupt_file()
                logger.error(
                    "Could not parse %s; preserved corrupt file at %s and "
                    "seeding fresh from env. Recover values from the backup if needed.",
                    self.path, backup,
                )
                s = self._seed_from_env()
                self._write(s)
                logger.info("Seeded settings from env vars -> %s", self.path)
        else:
            s = self._seed_from_env()
            self._write(s)
            logger.info("Seeded settings from env vars -> %s", self.path)

        # Auto-generate webhook_secret if missing. Covers fresh installs
        # (env var unset) and upgrades from <0.11.0 (where the secret was
        # optional). The webhook handler refuses POSTs without a secret,
        # so we guarantee one exists before the bot starts.
        if not s.webhook_secret:
            s.webhook_secret = secrets.token_urlsafe(32)
            logger.warning(
                "Auto-generated webhook_secret. Copy it from /admin (Webhook tab) "
                "into your Seerr webhook 'Authorization' header before Seerr can deliver events."
            )
            self._write(s)

        return s

    @staticmethod
    def _seed_from_env() -> Settings:
        def ids(raw: str) -> list[int]:
            return parse_id_list((raw or "").split(","))

        try:
            admin_tg_id = int(os.environ.get("ADMIN_TELEGRAM_ID", "0") or "0")
        except ValueError:
            admin_tg_id = 0

        return Settings(
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            admin_telegram_id=admin_tg_id,
            seerr_url=os.environ.get("SEERR_URL", "").strip(),
            seerr_api_key=os.environ.get("SEERR_API_KEY", "").strip(),
            seerr_public_url=os.environ.get("SEERR_PUBLIC_URL", "").strip(),
            # Pre-rename installs may still set the HERMES_* env names.
            usher_public_url=(os.environ.get("USHER_PUBLIC_URL")
                              or os.environ.get("HERMES_PUBLIC_URL") or "").strip(),
            radarr_url=os.environ.get("RADARR_URL", "").strip(),
            radarr_api_key=os.environ.get("RADARR_API_KEY", "").strip(),
            sonarr_url=os.environ.get("SONARR_URL", "").strip(),
            sonarr_api_key=os.environ.get("SONARR_API_KEY", "").strip(),
            allowed_autofix_telegram_ids=ids(os.environ.get("ALLOWED_AUTOFIX_TELEGRAM_IDS", "")),
            webhook_secret=(os.environ.get("USHER_WEBHOOK_SECRET")
                            or os.environ.get("HERMES_WEBHOOK_SECRET") or "").strip(),
            admin=AdminAccount(),  # always unset on first run
        )

    def _preserve_corrupt_file(self) -> Optional[Path]:
        """Move the unparseable settings file aside to a numbered sidecar so
        its contents survive the reseed. Numbered (.corrupt.1, .corrupt.2, ...)
        so a bad file that reappears boot-after-boot never clobbers an earlier
        rescue copy. Returns the sidecar path, or None if it could not be
        preserved (in which case the caller's reseed overwrites in place)."""
        for n in range(1, 1000):
            candidate = self.path.parent / f"{self.path.name}.corrupt.{n}"
            if not candidate.exists():
                try:
                    os.replace(self.path, candidate)
                    return candidate
                except OSError:
                    logger.exception("Failed to preserve corrupt %s", self.path)
                    return None
        return None

    def _fsync_parent_dir(self) -> None:
        """fsync the containing directory so a rename is itself durable.
        Best-effort: some platforms/filesystems can't open a dir for fsync."""
        try:
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

    def _write(self, s: Settings) -> None:
        # Crash-safe write: flush + fsync the temp file's contents to disk
        # BEFORE the atomic rename, then fsync the parent directory so the
        # rename is durable too. Without this, an unsafe shutdown (the host
        # loses power mid-write) can land the rename before the data, leaving a
        # truncated settings.json -- which _load_or_seed would then have to
        # preserve-and-reseed, locking the admin out. POSIX rename is already
        # atomic; durability is what we add here.
        tmp = self.path.with_suffix(".tmp")
        # Create the temp file 0600 up front (like fsutil.atomic_write_bytes)
        # so the secrets file never exists with looser permissions, not even
        # between the rename and a chmod.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(s.to_dict(), indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        self._fsync_parent_dir()

    def save(self) -> None:
        self._write(self.settings)

    async def save_async(self) -> None:
        """save() off the event loop: _write does two fsyncs synchronously,
        which stalls the webhook receiver and bot when called from an
        aiohttp handler on slow storage."""
        await asyncio.to_thread(self.save)


def hash_password(plaintext: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    h = pbkdf2_hmac("sha256", plaintext.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${h.hex()}"


def iterations_of(stored: str) -> int:
    """PBKDF2 iteration count embedded in a stored hash, 0 if unparseable.
    Lives here so the "pbkdf2_sha256$iters$salt$hash" format has one owner;
    the login auto-upgrade must not hand-parse it."""
    try:
        return int(stored.split("$")[1])
    except (IndexError, ValueError, AttributeError):
        return 0


def verify_password(plaintext: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$")
    except (ValueError, AttributeError):
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    candidate = pbkdf2_hmac("sha256", plaintext.encode(), salt, iters)
    return compare_digest(candidate, expected)


def validate_public_url(url: str) -> Optional[str]:
    """Return None if the URL is acceptable, else a user-facing error string.
    Empty is acceptable (means: not configured)."""
    url = (url or "").strip()
    if not url:
        return None
    if not (url.startswith("http://") or url.startswith("https://")):
        return "URL must start with http:// or https://"
    return None


def load_or_create_session_secret(path: str | Path) -> bytes:
    p = Path(path)
    if p.exists():
        try:
            # Read raw bytes -- never .strip(), since the secret is random
            # binary and may legitimately start or end with byte 0x0a, 0x20,
            # 0x09, etc. (CI flake: a `\n` first byte got silently lost.)
            data = p.read_bytes()
            if data:
                return data
        except OSError:
            pass
    secret = secrets.token_bytes(32)
    # Atomic + durable (fsutil): a torn write here truncates the HMAC
    # signing key and silently weakens every session token.
    atomic_write_bytes(p, secret)
    return secret
