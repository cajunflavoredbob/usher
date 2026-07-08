"""Shared fixtures for the Hermes test suite."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

# Make the project root importable so `from settings import ...` works
# regardless of where pytest is invoked from.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_settings_path(tmp_path: Path) -> Path:
    return tmp_path / "settings.json"


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "mappings.sqlite"


@pytest.fixture
def fresh_fernet_key(monkeypatch) -> str:
    """Set HERMES_ENCRYPTION_KEY to a fresh key for the duration of the test."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("HERMES_ENCRYPTION_KEY", key)
    return key


@pytest.fixture
def fresh_token_crypto(tmp_path: Path, fresh_fernet_key: str):
    """A TokenCrypto bound to a tmp key file and the env key."""
    from store import TokenCrypto
    return TokenCrypto(key_path=tmp_path / "encryption.key")


@pytest.fixture
async def fresh_store(tmp_db_path: Path, fresh_token_crypto):
    """A UserStore on a fresh tmp DB with a fresh key. Schema is initialized."""
    from store import UserStore
    return UserStore(tmp_db_path, crypto=fresh_token_crypto)
