"""Shared HTTP utilities: structured API exceptions, retry-on-transient,
and a user-friendly message helper for surfacing API errors to humans.

Single exception hierarchy across all four API clients so bot.py can handle
them uniformly. Retry policy is idempotency-aware: idempotent requests
(GET/HEAD/PUT/DELETE) retry on any transient failure (429/5xx + connection/
timeout); non-idempotent requests (POST/PATCH) retry ONLY when the request
provably never reached the server (pre-send connect errors) or was rejected
before processing (429), so a flaky network can't produce duplicate side
effects like a double-posted ticket or comment. Parses Seerr/Arr error body
so user-facing messages carry the real reason instead of "Client error
'422 ...'".
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, Optional, TypeVar

import httpx

logger = logging.getLogger("hermes." + __name__)
T = TypeVar("T")

# HTTP status codes that warrant a retry. 408 Request Timeout, 429 Too Many
# Requests, and the standard 5xx server-side transients EXCEPT 501 Not
# Implemented (a permanent "this endpoint doesn't exist" signal).
RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}

# Methods with no server-side side effect, or whose effect is identical on
# repeat -- safe to retry freely. POST/PATCH are absent on purpose.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"})

# httpx transport failures raised BEFORE the request could reach the server
# (no connection was ever established). Safe to retry even for a
# non-idempotent request -- the server never saw it.
_PRE_SEND_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# Every other transport-level failure (read/write timeouts and errors,
# protocol errors, or an ambiguous bare TimeoutException) may have left the
# request in flight, so retrying a non-idempotent call risks a duplicate.
_TRANSPORT_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


class APIError(Exception):
    """Base for all upstream API failures. Carries status_code (if applicable)
    and a user_message safe to show in Telegram (no URLs or headers)."""
    def __init__(self, message: str, *, status_code: Optional[int] = None,
                 service: str = "service"):
        super().__init__(message)
        self.status_code = status_code
        self.service = service
        self.user_message = message


class TransientAPIError(APIError):
    """Service was reachable but returned a retryable response (429/5xx) or
    a connection/timeout error fired. Retried automatically by `with_retry`."""


class PermanentAPIError(APIError):
    """4xx (except retryable). Will not succeed on retry."""


class NotFoundAPIError(PermanentAPIError):
    """404 specifically. Used by the autofix poller to give up cleanly when
    Sonarr/Radarr no longer knows about the media."""


def _parse_error_body(r: httpx.Response) -> str:
    """Extract a Seerr/Arr-style {"message": "..."} body. Falls back to
    short text or the HTTP status reason."""
    try:
        data = r.json()
        if isinstance(data, dict):
            msg = data.get("message") or data.get("error") or data.get("detail")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()
    except Exception:
        pass
    text = (r.text or "").strip()
    if text and len(text) < 200:
        return text
    return f"HTTP {r.status_code}"


def classify_response(r: httpx.Response, *, service: str) -> None:
    """Raise the appropriate APIError unless the response is 2xx. The API
    clients don't follow redirects, so a 3xx is an unexpected misconfiguration
    (e.g. an http->https bounce), not success -- it's surfaced as a permanent
    error rather than passed through as if it carried a usable body."""
    if r.status_code < 300:
        return
    detail = _parse_error_body(r)
    if 300 <= r.status_code < 400:
        raise PermanentAPIError(
            f"unexpected redirect (HTTP {r.status_code})",
            status_code=r.status_code, service=service,
        )
    if r.status_code == 404:
        raise NotFoundAPIError(detail, status_code=404, service=service)
    if r.status_code in RETRYABLE_STATUSES:
        raise TransientAPIError(detail, status_code=r.status_code, service=service)
    raise PermanentAPIError(detail, status_code=r.status_code, service=service)


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    retries: int = 3,
    backoff_base: float = 0.5,
    backoff_cap: float = 5.0,
    service: str = "service",
    idempotent: bool = True,
) -> T:
    """Run an async operation, retrying transient failures per `idempotent`.

    Always retries: pre-send connect failures (the server never saw the
    request). When `idempotent` (the default): also retries post-send
    transport errors and any TransientAPIError (429/5xx). When NOT idempotent:
    a post-send transport error or a 5xx/408 raises immediately (the request
    may already have taken effect, so a retry could duplicate it); only 429
    -- an explicit "rejected, not processed" -- is still retried.

    Permanent errors (including NotFoundAPIError) raise immediately. Backoff =
    backoff_base * 2^attempt + 0-25% jitter, capped at backoff_cap.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:
            return await fn()
        except _PRE_SEND_ERRORS as exc:
            last_exc = TransientAPIError(
                f"{service} connection error: {type(exc).__name__}",
                service=service,
            )
        except _TRANSPORT_ERRORS as exc:
            wrapped = TransientAPIError(
                f"{service} connection error: {type(exc).__name__}",
                service=service,
            )
            if not idempotent:
                raise wrapped from exc
            last_exc = wrapped
        except TransientAPIError as exc:
            if not idempotent and exc.status_code != 429:
                raise
            last_exc = exc
        if attempt >= retries:
            assert last_exc is not None
            raise last_exc
        delay = min(backoff_cap, backoff_base * (2 ** attempt))
        delay += random.uniform(0, delay * 0.25)
        logger.info("%s: %s; retrying in %.1fs (attempt %d/%d)",
                    service, type(last_exc).__name__, delay, attempt + 1, retries)
        await asyncio.sleep(delay)
    raise RuntimeError("with_retry: unreachable")


async def execute(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    service: str,
    retries: int = 3,
    idempotent: Optional[bool] = None,
    **kwargs,
) -> httpx.Response:
    """Issue an HTTP request via the given client and return the (guaranteed
    2xx) response, raising APIError on anything else. Retry safety is derived
    from the HTTP method (see IDEMPOTENT_METHODS); pass `idempotent` explicitly
    to override -- e.g. a POST the server treats idempotently, or a GET that
    must not be retried."""
    if idempotent is None:
        idempotent = method.upper() in IDEMPOTENT_METHODS

    async def _do() -> httpx.Response:
        r = await client.request(method, url, **kwargs)
        classify_response(r, service=service)
        return r
    return await with_retry(_do, service=service, retries=retries,
                            idempotent=idempotent)


def user_friendly_message(exc: BaseException) -> str:
    """Format an exception as a short user-facing message. Safe for Telegram.
    Falls back to a generic message for unknown exceptions to avoid leaking
    URLs, headers, or stack traces."""
    if isinstance(exc, TransientAPIError):
        return f"({exc.service} isn't responding right now; try again in a minute.)"
    if isinstance(exc, NotFoundAPIError):
        return f"({exc.service} doesn't know about this item — it may have been deleted.)"
    if isinstance(exc, PermanentAPIError):
        return f"({exc.service}: {exc.user_message})"
    if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException,
                        httpx.RemoteProtocolError)):
        return "(connection problem — try again in a moment.)"
    return "(unexpected error — check the bot logs.)"
