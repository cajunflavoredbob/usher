"""Admin auth hardening primitives: CSRF tokens, login throttling,
first-run setup token, audit logging, and Secure-cookie detection.

Single-process in-memory state for throttle counters. Persisted state
(setup token file) lives under data_dir. Audit lines go to the
`hermes.audit` logger so they can be filtered separately from
operational logs.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hermes.auth")
audit_logger = logging.getLogger("hermes.audit")

# Sliding-window failure counter. A given IP gets at most THROTTLE_MAX_FAILURES
# failed logins inside THROTTLE_WINDOW_S seconds; further attempts return 429
# until the oldest failure ages out.
THROTTLE_MAX_FAILURES = 5
THROTTLE_WINDOW_S = 300


class LoginThrottle:
    """Per-key sliding-window failure counter (key is typically an IP)."""

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> None:
        bucket = self._failures[key]
        while bucket and now - bucket[0] > THROTTLE_WINDOW_S:
            bucket.popleft()

    def is_locked(self, key: str) -> Optional[float]:
        """Return seconds-until-unlock if locked, else None."""
        now = time.monotonic()
        self._prune(key, now)
        bucket = self._failures[key]
        if len(bucket) >= THROTTLE_MAX_FAILURES:
            return max(0.0, THROTTLE_WINDOW_S - (now - bucket[0]))
        return None

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        self._prune(key, now)
        self._failures[key].append(now)

    def record_success(self, key: str) -> None:
        self._failures.pop(key, None)


# --- CSRF tokens ---

CSRF_COOKIE = "hermes_csrf"
CSRF_FORM_FIELD = "csrf_token"


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_for_request(request) -> str:
    """Return the current CSRF token from the cookie, generating one if missing.
    Callers must call attach_csrf_cookie(resp, token, secure=...) on the
    outgoing response when the value is new."""
    return request.cookies.get(CSRF_COOKIE) or generate_csrf_token()


def attach_csrf_cookie(resp, token: str, *, secure: bool) -> None:
    """Set the CSRF cookie on the response. SameSite=Strict because there's
    no cross-site flow that legitimately submits to /admin/*."""
    resp.set_cookie(
        CSRF_COOKIE, token,
        max_age=24 * 3600,
        httponly=False,
        samesite="Strict",
        secure=secure,
    )


def validate_csrf(request, form_value: Optional[str]) -> bool:
    """Double-submit validation: cookie value must match the form field."""
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie or not form_value:
        return False
    return hmac.compare_digest(cookie, form_value)


# --- Setup token ---

SETUP_TOKEN_FILE = "setup_token"


def load_or_create_setup_token(data_dir: Path) -> Optional[str]:
    """Return the active setup token. Generated and printed to logs on first
    run if not present. Returns None once setup is complete (file removed)
    or if the file exists but is empty."""
    p = Path(data_dir) / SETUP_TOKEN_FILE
    if p.exists():
        try:
            existing = p.read_text().strip()
            if existing:
                return existing
        except OSError:
            pass
        return None
    token = secrets.token_urlsafe(24)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    logger.warning(
        "First-run setup token generated. Required to access /admin/setup. "
        "Token: %s (stored at %s; delete the file to invalidate)",
        token, p,
    )
    return token


def clear_setup_token(data_dir: Path) -> None:
    p = Path(data_dir) / SETUP_TOKEN_FILE
    try:
        p.unlink()
    except FileNotFoundError:
        pass


# --- Audit log ---

def audit(event: str, *, user: str = "-", ip: str = "-", **extra) -> None:
    """Structured audit log entry. Filter with `grep hermes.audit` in logs."""
    fields = " ".join(f"{k}={v}" for k, v in extra.items()) if extra else ""
    audit_logger.warning("event=%s user=%s ip=%s %s", event, user, ip, fields)


# --- Secure-cookie + IP detection ---

def request_is_secure(request) -> bool:
    """True if the request came in over HTTPS or via a proxy reporting https."""
    if request.scheme == "https":
        return True
    xfp = request.headers.get("X-Forwarded-Proto", "")
    return xfp.lower() == "https"


def client_ip(request) -> str:
    """Best-effort client IP, honoring X-Forwarded-For when present."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    if request.remote:
        return request.remote
    return "-"
