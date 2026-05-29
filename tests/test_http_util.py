"""Tests for http_util.py: response classification, retry-on-transient,
user-friendly message formatting."""
from __future__ import annotations

import httpx
import pytest

from http_util import (
    APIError,
    NotFoundAPIError,
    PermanentAPIError,
    TransientAPIError,
    classify_response,
    user_friendly_message,
    with_retry,
)


def _response(status: int, body: bytes = b"", content_type: str = "application/json") -> httpx.Response:
    return httpx.Response(status, content=body, headers={"Content-Type": content_type})


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


@pytest.mark.parametrize("code", [502, 503, 504, 408])
def test_classify_5xx_subset_transient(code):
    with pytest.raises(TransientAPIError):
        classify_response(_response(code), service="X")


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
