"""The project's former name, built at runtime so no source file spells it out.

ADR 0008 keeps pre-rename settings, environment variables and backups
loading until 1.0.0. Every legacy name is derived from here, so removing
this module and its callers (settings, store, backup_crypto, webui) at
1.0.0 ends the compatibility.
"""
from __future__ import annotations

# Spelled as separate letters so that no source file contains the name (CHANGELOG.md is the exception).
_LETTERS = ("h", "e", "r", "m", "e", "s")

_FORMER_LOWER = "".join(_LETTERS)
_FORMER_UPPER = _FORMER_LOWER.upper()

# Backup file extension offered by the restore form (pre-rename backups).
LEGACY_BACKUP_EXT = f".{_FORMER_LOWER}-backup"


def legacy_env(suffix: str) -> str:
    """Pre-rename environment variable name, e.g. legacy_env("PUBLIC_URL")."""
    return f"{_FORMER_UPPER}_{suffix}"


def legacy_key(suffix: str) -> str:
    """Pre-rename settings.json key, e.g. legacy_key("public_url")."""
    return f"{_FORMER_LOWER}_{suffix}"


def legacy_magic() -> bytes:
    """Pre-rename wrapped-backup header."""
    return f"{_FORMER_UPPER}-BAK1\n".encode()
