"""Admin auth hardening primitives: CSRF tokens, login throttling,
first-run setup token, audit logging, and Secure-cookie detection.

Single-process in-memory state for throttle counters. Persisted state
(setup token file) lives under data_dir. Audit lines go to the
`hermes.audit` logger so they can be filtered separately from
operational logs.
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import re
import secrets
import time
from collections import deque
from pathlib import Path
from typing import Optional

from fsutil import atomic_write_text

logger = logging.getLogger("hermes.auth")
audit_logger = logging.getLogger("hermes.audit")

# Sliding-window failure counter. A given IP gets at most THROTTLE_MAX_FAILURES
# failed logins inside THROTTLE_WINDOW_S seconds; further attempts return 429
# until the oldest failure ages out.
THROTTLE_MAX_FAILURES = 5
THROTTLE_WINDOW_S = 300
# Per-IP aggregate cap, checked BEFORE any password hashing. Keyed on the
# source IP alone so rotating the username field can't mint fresh unlocked
# buckets and drive unbounded PBKDF2 work (a login-flood DoS + guess-reset).
# Higher than the per-(ip,username) cap so a single fat-fingering admin
# isn't blocked by it in normal use.
THROTTLE_IP_MAX_FAILURES = 20
# Hard cap on tracked keys: a botnet spraying unique source
# addresses must not grow the map toward OOM. Far above any legitimate
# concurrent-attacker count for a single-admin LAN app.
THROTTLE_MAX_KEYS = 4096


class LoginThrottle:
    """Per-key sliding-window failure counter (key is typically an IP).
    Bounded: empty buckets are dropped, is_locked never inserts (the old
    defaultdict minted a bucket for every key it merely checked), and the
    key count is capped with expired-then-oldest eviction. max_failures
    defaults to THROTTLE_MAX_FAILURES; pass a higher value for a coarser
    aggregate (per-IP) throttle."""

    def __init__(self, max_failures: int = THROTTLE_MAX_FAILURES) -> None:
        self._failures: dict[str, deque[float]] = {}
        self._max_failures = max_failures

    def _prune(self, key: str, now: float) -> None:
        bucket = self._failures.get(key)
        if bucket is None:
            return
        while bucket and now - bucket[0] > THROTTLE_WINDOW_S:
            bucket.popleft()
        if not bucket:
            self._failures.pop(key, None)

    def is_locked(self, key: str) -> Optional[float]:
        """Return seconds-until-unlock if locked, else None."""
        now = time.monotonic()
        self._prune(key, now)
        bucket = self._failures.get(key)
        if bucket and len(bucket) >= self._max_failures:
            return max(0.0, THROTTLE_WINDOW_S - (now - bucket[0]))
        return None

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        self._prune(key, now)
        if key not in self._failures and len(self._failures) >= THROTTLE_MAX_KEYS:
            self._evict_one(now)
        self._failures.setdefault(key, deque()).append(now)

    def record_success(self, key: str) -> None:
        self._failures.pop(key, None)

    def _evict_one(self, now: float) -> None:
        """Free a slot: sweep out expired buckets first; then evict the
        oldest NON-LOCKED bucket so a flood of filler keys can't evict a
        locked bucket and hand the attacker fresh guesses. Only if every
        bucket is locked do we drop the oldest outright."""
        for k in list(self._failures):
            self._prune(k, now)
            if len(self._failures) < THROTTLE_MAX_KEYS:
                return
        for k in list(self._failures):
            if len(self._failures[k]) < self._max_failures:
                self._failures.pop(k, None)
                return
        if self._failures:
            self._failures.pop(next(iter(self._failures)))


# --- CSRF tokens ---

CSRF_COOKIE = "hermes_csrf"
CSRF_FORM_FIELD = "csrf_token"
# Must match webui.SESSION_COOKIE. Used only to bind CSRF tokens to the
# session that minted them (see _session_binding); a divergence would just
# make every authenticated CSRF token look session-less, forcing a re-mint.
SESSION_COOKIE_NAME = "hermes_session"


def _session_binding(request) -> str:
    """A per-session binding value for CSRF tokens: the signature of the
    session cookie, or '' when unauthenticated (login/setup). Binding the
    CSRF HMAC to this stops a token minted for one session (or for no
    session, fetched from the public login page) being replayed against a
    different authenticated session -- the cookie-planting attack a bare
    signed double-submit still allows."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME, "")
    return cookie.rsplit(".", 1)[-1] if cookie else ""


def generate_csrf_token(secret: bytes, binding: str = "") -> str:
    """Mint a CSRF token signed with the session secret AND the session
    binding. Signing defeats a cookie-planting attacker who can't produce the
    HMAC; binding defeats one who lifts a validly-signed token from the
    public login page and plants it against a logged-in session."""
    rand = secrets.token_urlsafe(32)
    sig = hmac.new(secret, f"{rand}.{binding}".encode(), "sha256").hexdigest()[:32]
    return f"{rand}.{sig}"


def _csrf_token_signed(secret: bytes, token: str, binding: str = "") -> bool:
    try:
        rand, sig = token.split(".")
    except (ValueError, AttributeError):
        return False
    expected = hmac.new(secret, f"{rand}.{binding}".encode(), "sha256").hexdigest()[:32]
    return hmac.compare_digest(sig, expected)


def csrf_for_request(request) -> str:
    """Return the current CSRF token from the cookie, generating one if the
    cookie is missing, unsigned, or not bound to this session (pre-0.13 and
    cross-session cookies rotate transparently). Callers must call
    attach_csrf_cookie(resp, token, secure=...) on the outgoing response when
    the value is new."""
    secret: bytes = request.app["session_secret"]
    binding = _session_binding(request)
    token = request.cookies.get(CSRF_COOKIE)
    if token and _csrf_token_signed(secret, token, binding):
        return token
    return generate_csrf_token(secret, binding)


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
    """Double-submit validation plus signed+bound check: the cookie value
    must match the form field AND carry a valid HMAC from this install's
    secret for THIS session's binding."""
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie or not form_value:
        return False
    if not hmac.compare_digest(cookie, form_value):
        return False
    return _csrf_token_signed(request.app["session_secret"], cookie,
                              _session_binding(request))


# --- Setup token ---

SETUP_TOKEN_FILE = "setup_token"

# Sentinel returned when the token file exists but can't be read: the setup
# endpoint treats it as "check impossible, refuse" rather than "no token".
_SETUP_TOKEN_UNREADABLE = "\x00unreadable\x00"


def load_or_create_setup_token(data_dir: Path) -> Optional[str]:
    """Return the active setup token, or None once setup is complete (file
    removed). Returns the _SETUP_TOKEN_UNREADABLE sentinel when the file
    exists but can't be read (the caller must refuse setup, never skip the
    check). An EMPTY file (e.g. a torn pre-fsutil write) regenerates a token
    rather than disabling the check."""
    p = Path(data_dir) / SETUP_TOKEN_FILE
    if p.exists():
        try:
            existing = p.read_text().strip()
        except OSError:
            # Fail CLOSED: a sentinel the setup endpoint refuses on, rather
            # than None (which historically skipped the token check and
            # opened admin creation on an IO fault).
            logger.error("setup_token exists but is unreadable; refusing setup "
                         "until it can be read.")
            return _SETUP_TOKEN_UNREADABLE
        if existing:
            return existing
        logger.warning("Empty setup_token file found; regenerating a token "
                       "rather than skipping the setup-token check.")
        p.unlink(missing_ok=True)
    token = secrets.token_urlsafe(24)
    # Atomic + durable (fsutil): consistent with the other first-boot writes.
    atomic_write_text(p, token)
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

_AUDIT_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")


def _audit_safe(value) -> str:
    """Neutralize control characters (newlines above all) in values that can
    be attacker-controlled, so one login attempt can't forge audit lines."""
    return _AUDIT_UNSAFE.sub("?", str(value))


def audit(event: str, *, user: str = "-", ip: str = "-", **extra) -> None:
    """Structured audit log entry. Filter with `grep hermes.audit` in logs."""
    fields = (" ".join(f"{k}={_audit_safe(v)}" for k, v in extra.items())
              if extra else "")
    audit_logger.warning("event=%s user=%s ip=%s %s",
                         event, _audit_safe(user), _audit_safe(ip), fields)


# --- Secure-cookie + IP detection ---

def parse_trusted_proxies(raw: str) -> tuple:
    """Parse a comma-separated list of CIDRs (or bare IPs) into networks.
    Invalid entries are logged and skipped rather than failing startup."""
    networks = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            logger.warning("TRUSTED_PROXIES entry %r is not a valid IP/CIDR; ignored", part)
    return tuple(networks)


def _trusted_networks(request) -> tuple:
    """Trusted-proxy networks from app state; empty when unset or when the
    request object has no app (unit-test fakes)."""
    app = getattr(request, "app", None)
    if app is None:
        return ()
    try:
        return app.get("trusted_proxies") or ()
    except AttributeError:
        return ()


def _in_networks(ip_str: str, networks) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def request_is_secure(request) -> bool:
    """True if the request came in over HTTPS, or a TRUSTED proxy reports
    https via X-Forwarded-Proto. The header is attacker-supplied on a
    direct connection, so it is honored only from configured proxies."""
    if request.scheme == "https":
        return True
    networks = _trusted_networks(request)
    if not (networks and request.remote and _in_networks(request.remote, networks)):
        return False
    xfp = request.headers.get("X-Forwarded-Proto", "")
    return xfp.lower() == "https"


def client_ip(request) -> str:
    """Client IP for throttling/audit. The socket peer is the only value an
    attacker can't choose, so X-Forwarded-For is honored ONLY when the peer
    is a configured trusted proxy (TRUSTED_PROXIES env, CIDR list) -- and
    then by walking the chain right-to-left past trusted hops, so a spoofed
    value prepended by the client can never win. With no trusted proxies
    the header is ignored entirely (it previously keyed the login throttle,
    letting an attacker rotate headers into fresh buckets)."""
    peer = request.remote or "-"
    networks = _trusted_networks(request)
    if not (networks and peer != "-" and _in_networks(peer, networks)):
        return peer
    xff = request.headers.get("X-Forwarded-For", "")
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    for hop in reversed(hops):
        if not _in_networks(hop, networks):
            # Behind a trusted proxy the hop text is still client-supplied;
            # only ever return something that parses as an IP so garbage
            # can't flow into throttle keys and audit lines.
            try:
                ipaddress.ip_address(hop)
            except ValueError:
                return peer
            return hop
    return peer
