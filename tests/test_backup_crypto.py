"""Tests for backup_crypto.py: wrap/unwrap round-trip, error paths."""
from __future__ import annotations

import pytest

from backup_crypto import MAGIC, is_wrapped, unwrap, wrap


def test_wrap_then_unwrap_roundtrip():
    plain = b"this is the original zip content" * 10
    wrapped = wrap(plain, "correct-horse-battery-staple")
    assert is_wrapped(wrapped)
    assert unwrap(wrapped, "correct-horse-battery-staple") == plain


def test_wrong_passphrase_raises_value_error():
    wrapped = wrap(b"payload", "pass-one")
    with pytest.raises(ValueError):
        unwrap(wrapped, "pass-two")


def test_unwrap_on_plain_zip_raises():
    plain_zip = b"PK\x03\x04...not actually a zip but no magic prefix"
    with pytest.raises(ValueError):
        unwrap(plain_zip, "anything")


def test_unwrap_on_truncated_raises():
    with pytest.raises(ValueError):
        unwrap(MAGIC + b"\x00", "anything")


def test_is_wrapped_detects_magic():
    assert is_wrapped(MAGIC + b"junk") is True
    assert is_wrapped(b"PK\x03\x04zipdata") is False
    assert is_wrapped(b"") is False


def test_different_salts_per_wrap():
    """Two wraps of the same plaintext + passphrase produce different ciphertexts."""
    plain = b"same"
    a = wrap(plain, "same-pass")
    b = wrap(plain, "same-pass")
    assert a != b
    assert unwrap(a, "same-pass") == plain
    assert unwrap(b, "same-pass") == plain
