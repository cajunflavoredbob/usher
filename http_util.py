"""Shared HTTP utilities: structured API exceptions, retry-on-transient,
and a user-friendly message helper for surfacing API errors to humans.

Single exception hierarchy across all four API clients so bot.py can handle
them uniformly. Retries idempotent requests on transient failures (429/5xx
+ connection/timeout). Parses Seerr/Arr error body so user-facing messages
carry the real reason instead of "Client error '422 ...'".
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, Optional, TypeVar

import httpx

logger = logging.getLogger(__name__)
T = TypeVar("T")

# HTTP status codes that warrant a retry. 408 Request Timeout, 429 Too Many
# Requests, and the standard 5xx-but-not-501 server-side transients.
RETRYABLE_STATUSES = {408, 429, 502, 503, 504}


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
    """Raise the appropriate APIError if the response is not 2xx."""
    if r.status_code < 400:
        return
    detail = _parse_error_body(r)
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
) -> T:
    """Run an idempotent async operation, retrying transient failures.

    Retries on TransientAPIError + httpx ConnectError/TimeoutException/
    RemoteProtocolError. Permanent errors (including NotFoundAPIError)
    raise immediately. Backoff = backoff_base * 2^attempt + 0-25% jitter;
    capped at backoff_cap. Worst-case total wait with defaults ~3.5s.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:
            return await fn()
        except (httpx.ConnectError, httpx.TimeoutException,
                httpx.RemoteProtocolError) as exc:
            last_exc = TransientAPIError(
                f"{service} connection error: {type(exc).__name__}",
                service=service,
            )
        except TransientAPIError as exc:
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
    **kwargs,
) -> httpx.Response:
    """Issue an HTTP request via the given client, retrying transient errors
    and raising APIError on 4xx. The returned response is guaranteed 2xx."""
    async def _do() -> httpx.Response:
        r = await client.request(method, url, **kwargs)
        classify_response(r, service=service)
        return r
    return await with_retry(_do, service=service, retries=retries)


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
