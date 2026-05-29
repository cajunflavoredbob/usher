"""Optional passphrase-wrapped backup format.

Layout:
  MAGIC (12 bytes): b"HERMES-BAK1\\n"
  SALT  (16 bytes): per-backup random salt
  TOKEN (variable): Fernet-encrypted plaintext (the original ZIP bytes)

Key derivation: PBKDF2(HMAC-SHA256, 600_000 iters) over the passphrase.
Fernet provides AES-128-CBC + HMAC-SHA256 + nonce internally.
"""
from __future__ import annotations

import base64
import secrets

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

MAGIC = b"HERMES-BAK1\n"
SALT_LEN = 16
PBKDF2_ITERATIONS = 600_000


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def wrap(plain_zip: bytes, passphrase: str) -> bytes:
    salt = secrets.token_bytes(SALT_LEN)
    key = _derive_key(passphrase, salt)
    token = Fernet(key).encrypt(plain_zip)
    return MAGIC + salt + token


def is_wrapped(blob: bytes) -> bool:
    return blob.startswith(MAGIC)


def unwrap(blob: bytes, passphrase: str) -> bytes:
    """Raises ValueError on bad passphrase or malformed input."""
    if not is_wrapped(blob):
        raise ValueError("Not a wrapped backup.")
    header_end = len(MAGIC) + SALT_LEN
    if len(blob) < header_end:
        raise ValueError("Wrapped backup truncated.")
    salt = blob[len(MAGIC):header_end]
    token = blob[header_end:]
    key = _derive_key(passphrase, salt)
    try:
        return Fernet(key).decrypt(token)
    except InvalidToken:
        raise ValueError("Wrong passphrase or corrupted backup.")
