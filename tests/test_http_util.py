"""Tests for http_util.py: response classification, retry-on-transient,
user-friendly message formatting."""
from __future__ import annotations

import httpx
import pytest

from http_util import (
    NotFoundAPIError,
    PermanentAPIError,
    TransientAPIError,
    classify_response,
    execute,
    user_friendly_message,
    with_retry,
)


def _response(status: int, body: bytes = b"", content_type: str = "application/json") -> httpx.Response:
    return httpx.Response(status, content=body, headers={"Content-Type": content_type})


class _FakeClient:
    """Minimal httpx.AsyncClient stand-in for execute() tests. Each call to
    request() yields the next item from `script`; an Exception is raised, an
    httpx.Response is returned. The last item repeats once the script runs out."""

    def __init__(self, script: list):
        self.script = script
        self.calls = 0

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


_FAST = {"backoff_base": 0.001, "backoff_cap": 0.01}


# --- classify_response ---


def test_classify_2xx_is_noop():
    classify_response(_response(200, b'{"ok": true}'), service="X")
    classify_response(_response(204), service="X")


def test_classify_404_raises_not_found():
    with pytest.raises(NotFoundAPIError) as ei:
        classify_response(_response(404, b'{"message": "not here"}'), service="Sonarr")
    assert ei.value.status_code == 404
    assert ei.value.service == "Sonarr"
    assert "not here" in str(ei.value)


def test_classify_429_is_transient():
    with pytest.raises(TransientAPIError):
        classify_response(_response(429, b'{"message": "slow down"}'), service="Seerr")


@pytest.mark.parametrize("code", [500, 502, 503, 504, 408])
def test_classify_5xx_subset_transient(code):
    with pytest.raises(TransientAPIError):
        classify_response(_response(code), service="X")


def test_classify_501_is_permanent():
    """501 Not Implemented is a permanent 'endpoint doesn't exist', not a
    transient server blip -- it must not be retried."""
    with pytest.raises(PermanentAPIError) as ei:
        classify_response(_response(501), service="X")
    assert not isinstance(ei.value, TransientAPIError)


@pytest.mark.parametrize("code", [301, 302, 307, 308])
def test_classify_3xx_is_permanent_redirect(code):
    """Clients don't follow redirects; a 3xx is a misconfiguration surfaced
    as an error, not a pass-through 'success'."""
    with pytest.raises(PermanentAPIError) as ei:
        classify_response(_response(code), service="X")
    assert ei.value.status_code == code
    assert "redirect" in ei.value.user_message.lower()
    assert not isinstance(ei.value, TransientAPIError)


@pytest.mark.parametrize("code", [400, 401, 403, 422])
def test_classify_4xx_permanent(code):
    with pytest.raises(PermanentAPIError) as ei:
        classify_response(_response(code, b'{"message": "rejected"}'), service="X")
    assert "rejected" in str(ei.value)


def test_classify_parses_message_field():
    with pytest.raises(PermanentAPIError) as ei:
        classify_response(
            _response(400, b'{"message": "Validation failed: tmdbId required"}'),
            service="Sonarr",
        )
    assert "Validation failed" in ei.value.user_message


def test_classify_falls_back_when_no_json_message():
    with pytest.raises(PermanentAPIError) as ei:
        classify_response(_response(418, b"short text body", content_type="text/plain"),
                          service="X")
    assert ei.value.user_message == "short text body"


def test_classify_truncates_long_body():
    long_body = b"x" * 500
    with pytest.raises(PermanentAPIError) as ei:
        classify_response(_response(418, long_body, content_type="text/plain"),
                          service="X")
    # Long body falls back to "HTTP 418".
    assert ei.value.user_message == "HTTP 418"


# --- with_retry ---


async def test_with_retry_succeeds_first_try():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        return "ok"

    result = await with_retry(fn, retries=3, backoff_base=0.001)
    assert result == "ok"
    assert calls["n"] == 1


async def test_with_retry_retries_transient_then_succeeds():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientAPIError("blip")
        return "ok"

    result = await with_retry(fn, retries=3, backoff_base=0.001, backoff_cap=0.01)
    assert result == "ok"
    assert calls["n"] == 3


async def test_with_retry_gives_up_after_retries_exhausted():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise TransientAPIError("persistent blip")

    with pytest.raises(TransientAPIError):
        await with_retry(fn, retries=2, backoff_base=0.001, backoff_cap=0.01)
    assert calls["n"] == 3  # 2 retries + initial attempt


async def test_with_retry_does_not_retry_permanent():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise PermanentAPIError("bad request")

    with pytest.raises(PermanentAPIError):
        await with_retry(fn, retries=5, backoff_base=0.001)
    assert calls["n"] == 1


async def test_with_retry_does_not_retry_not_found():
    """NotFoundAPIError is PermanentAPIError; same no-retry behavior."""
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise NotFoundAPIError("missing", status_code=404, service="X")

    with pytest.raises(NotFoundAPIError):
        await with_retry(fn, retries=5, backoff_base=0.001)
    assert calls["n"] == 1


async def test_with_retry_wraps_connection_errors():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("dns broke")
        return "recovered"

    result = await with_retry(fn, retries=3, backoff_base=0.001, backoff_cap=0.01,
                              service="Seerr")
    assert result == "recovered"
    assert calls["n"] == 2


async def test_with_retry_wraps_timeout():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise httpx.TimeoutException("timed out")

    with pytest.raises(TransientAPIError) as ei:
        await with_retry(fn, retries=1, backoff_base=0.001, backoff_cap=0.01)
    assert "TimeoutException" in str(ei.value)
    assert calls["n"] == 2


async def test_with_retry_idempotent_retries_read_error():
    """ReadError/WriteError were previously uncaught and bypassed retry +
    friendly wrapping. For an idempotent call they should now retry."""
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ReadError("connection reset")
        return "recovered"

    result = await with_retry(fn, retries=3, **_FAST)
    assert result == "recovered"
    assert calls["n"] == 2


# --- with_retry: idempotency-aware (non-idempotent must not duplicate) ---


async def test_non_idempotent_does_not_retry_5xx():
    """A 5xx on a POST may mean the server processed it then failed to reply;
    retrying could double the side effect, so it raises immediately."""
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise TransientAPIError("server error", status_code=503, service="Seerr")

    with pytest.raises(TransientAPIError):
        await with_retry(fn, retries=5, idempotent=False, **_FAST)
    assert calls["n"] == 1


async def test_non_idempotent_does_not_retry_post_send_error():
    """A read timeout means the request was already in flight -- don't retry
    a non-idempotent call."""
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise httpx.ReadTimeout("slow")

    with pytest.raises(TransientAPIError):
        await with_retry(fn, retries=5, idempotent=False, **_FAST)
    assert calls["n"] == 1


async def test_non_idempotent_retries_pre_send_connect_error():
    """A connect error means the server never saw the request -- safe to retry
    even for a non-idempotent call."""
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("dns broke")
        return "ok"

    result = await with_retry(fn, retries=5, idempotent=False, **_FAST)
    assert result == "ok"
    assert calls["n"] == 2


async def test_non_idempotent_retries_429():
    """429 is an explicit 'rejected, not processed' -- safe to retry even for
    a non-idempotent call."""
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientAPIError("slow down", status_code=429, service="Seerr")
        return "ok"

    result = await with_retry(fn, retries=5, idempotent=False, **_FAST)
    assert result == "ok"
    assert calls["n"] == 3


# --- execute: retry safety derived from HTTP method ---


async def test_execute_get_retries_transient():
    client = _FakeClient([_response(503), _response(200, b'{"ok": true}')])
    r = await execute(client, "GET", "/thing", service="X", **_FAST)
    assert r.status_code == 200
    assert client.calls == 2


async def test_execute_post_does_not_retry_transient():
    """The core fix: a POST that 503s is not retried, so no duplicate ticket."""
    client = _FakeClient([_response(503), _response(200)])
    with pytest.raises(TransientAPIError):
        await execute(client, "POST", "/issue", service="Seerr", **_FAST)
    assert client.calls == 1


async def test_execute_post_retries_pre_send_connect_error():
    client = _FakeClient([httpx.ConnectError("dns"), _response(200)])
    r = await execute(client, "POST", "/issue", service="Seerr", **_FAST)
    assert r.status_code == 200
    assert client.calls == 2


async def test_execute_post_idempotent_override_retries():
    """An explicit idempotent=True override restores retry-on-transient for a
    POST the server treats idempotently."""
    client = _FakeClient([_response(503), _response(200)])
    r = await execute(client, "POST", "/x", service="X", idempotent=True, **_FAST)
    assert r.status_code == 200
    assert client.calls == 2


async def test_execute_delete_retries_transient():
    """DELETE is idempotent -- still retries."""
    client = _FakeClient([_response(503), _response(200)])
    r = await execute(client, "DELETE", "/moviefile/5", service="Radarr", **_FAST)
    assert r.status_code == 200
    assert client.calls == 2


# --- user_friendly_message ---


def test_user_friendly_transient_mentions_service():
    msg = user_friendly_message(TransientAPIError("oops", service="Sonarr"))
    assert "Sonarr" in msg
    assert "try again" in msg.lower()


def test_user_friendly_not_found_mentions_deleted():
    msg = user_friendly_message(NotFoundAPIError("gone", service="Radarr"))
    assert "Radarr" in msg
    assert "deleted" in msg.lower()


def test_user_friendly_permanent_includes_message():
    msg = user_friendly_message(PermanentAPIError("validation failed", service="Seerr"))
    assert "Seerr" in msg
    assert "validation failed" in msg


def test_user_friendly_connect_error_generic():
    msg = user_friendly_message(httpx.ConnectError("dns"))
    assert "connection problem" in msg.lower()


def test_user_friendly_unknown_exception_hides_internals():
    msg = user_friendly_message(ValueError("internal: /api/v3/movies key=ABC123"))
    # Should not leak the URL or the key.
    assert "ABC123" not in msg
    assert "/api/v3" not in msg
    assert "unexpected" in msg.lower()
