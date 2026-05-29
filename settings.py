"""Settings management for Hermes.

settings.json under /data is the source of truth. Env vars seed it on
first run, then become inert.

Includes admin password helpers (pbkdf2_sha256, stdlib-only) and the
session-secret loader used by the webui.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import asdict, dataclass, field
from hashlib import pbkdf2_hmac
from hmac import compare_digest
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hermes.settings")

PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16


@dataclass
class AdminAccount:
    username: str = ""
    # Format: pbkdf2_sha256$<iter>$<salt_hex>$<hash_hex>
    password_hash: str = ""

    def is_set(self) -> bool:
        return bool(self.username and self.password_hash)


DEFAULT_DAILY_AUTOFIX_LIMIT = 3


@dataclass
class Settings:
    telegram_bot_token: str = ""
    admin_telegram_id: int = 0
    hermes_public_url: str = ""
    seerr_url: str = ""
    seerr_api_key: str = ""
    seerr_public_url: str = ""
    radarr_url: str = ""
    radarr_api_key: str = ""
    sonarr_url: str = ""
    sonarr_api_key: str = ""
    allowed_autofix_telegram_ids: list[int] = field(default_factory=list)
    daily_autofix_limit: int = DEFAULT_DAILY_AUTOFIX_LIMIT
    webhook_secret: str = ""
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
            hermes_public_url=data.get("hermes_public_url", "") or "",
            seerr_url=data.get("seerr_url", "") or "",
            seerr_api_key=data.get("seerr_api_key", "") or "",
            seerr_public_url=data.get("seerr_public_url", "") or "",
            radarr_url=data.get("radarr_url", "") or "",
            radarr_api_key=data.get("radarr_api_key", "") or "",
            sonarr_url=data.get("sonarr_url", "") or "",
            sonarr_api_key=data.get("sonarr_api_key", "") or "",
            allowed_autofix_telegram_ids=list(data.get("allowed_autofix_telegram_ids") or []),
            daily_autofix_limit=daily_limit,
            webhook_secret=data.get("webhook_secret", "") or "",
            admin=AdminAccount(
                username=admin_data.get("username", "") or "",
                password_hash=admin_data.get("password_hash", "") or "",
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
                logger.exception("Failed to load %s; seeding fresh from env", self.path)
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
            out: list[int] = []
            for chunk in (raw or "").split(","):
                chunk = chunk.strip()
                if chunk.isdigit():
                    out.append(int(chunk))
            return out

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
            radarr_url=os.environ.get("RADARR_URL", "").strip(),
            radarr_api_key=os.environ.get("RADARR_API_KEY", "").strip(),
            sonarr_url=os.environ.get("SONARR_URL", "").strip(),
            sonarr_api_key=os.environ.get("SONARR_API_KEY", "").strip(),
            allowed_autofix_telegram_ids=ids(os.environ.get("ALLOWED_AUTOFIX_TELEGRAM_IDS", "")),
            webhook_secret=os.environ.get("HERMES_WEBHOOK_SECRET", "").strip(),
            admin=AdminAccount(),  # always unset on first run
        )

    def _write(self, s: Settings) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(s.to_dict(), indent=2))
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def save(self) -> None:
        self._write(self.settings)


def hash_password(plaintext: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    h = pbkdf2_hmac("sha256", plaintext.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${h.hex()}"


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
            data = p.read_bytes().strip()
            if data:
                return data
        except OSError:
            pass
    p.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    p.write_bytes(secret)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return secret
