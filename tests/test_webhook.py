"""Tests for webhook.py: auth, dispatch, dedupe, size cap, handler isolation."""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from webhook import MAX_BODY_BYTES, attach_webhook

SECRET = "test-secret-abc"


def _make_app(secret: str = SECRET, **handlers):
    """Build a web.Application with attach_webhook wired up.

    handlers: pass on_comment / on_resolved / on_reported as needed;
    defaults are recording no-ops that append payloads into `calls`.
    """
    calls = {"comment": [], "resolved": [], "reported": []}

    async def default_comment(payload: dict) -> None:
        calls["comment"].append(payload)

    async def default_resolved(payload: dict) -> None:
        calls["resolved"].append(payload)

    async def default_reported(payload: dict) -> None:
        calls["reported"].append(payload)

    app = web.Application()
    attach_webhook(
        app,
        on_comment=handlers.get("on_comment", default_comment),
        on_resolved=handlers.get("on_resolved", default_resolved),
        on_reported=handlers.get("on_reported", default_reported),
        secret_provider=lambda: secret,
    )
    return app, calls


@pytest.fixture
async def client():
    app, calls = _make_app()
    async with TestClient(TestServer(app)) as c:
        c.calls = calls  # type: ignore[attr-defined]
        yield c


# --- auth ---


async def test_rejects_missing_auth(client):
    r = await client.post("/webhook/seerr", json={"notification_type": "ISSUE_COMMENT"})
    assert r.status == 401


async def test_rejects_bad_auth(client):
    r = await client.post(
        "/webhook/seerr",
        json={"notification_type": "ISSUE_COMMENT"},
        headers={"Authorization": "wrong"},
    )
    assert r.status == 401


async def test_accepts_correct_auth(client):
    r = await client.post(
        "/webhook/seerr",
        json={"notification_type": "ISSUE_COMMENT", "issue": {"id": 1}},
        headers={"Authorization": SECRET},
    )
    assert r.status == 200
    assert len(client.calls["comment"]) == 1


async def test_rejects_when_secret_unset():
    app, _ = _make_app(secret="")
    async with TestClient(TestServer(app)) as c:
        r = await c.post(
            "/webhook/seerr",
            json={"notification_type": "ISSUE_COMMENT"},
            headers={"Authorization": "anything"},
        )
        assert r.status == 503


# --- dispatch ---


async def test_dispatches_comment(client):
    r = await client.post(
        "/webhook/seerr",
        json={"notification_type": "ISSUE_COMMENT", "issue": {"id": 7}},
        headers={"Authorization": SECRET},
    )
    assert r.status == 200
    assert client.calls["comment"][0]["issue"]["id"] == 7
    assert client.calls["reported"] == []
    assert client.calls["resolved"] == []


async def test_dispatches_resolved(client):
    r = await client.post(
        "/webhook/seerr",
        json={"notification_type": "ISSUE_RESOLVED", "issue": {"id": 8}},
        headers={"Authorization": SECRET},
    )
    assert r.status == 200
    assert len(client.calls["resolved"]) == 1


async def test_dispatches_issue_created(client):
    r = await client.post(
        "/webhook/seerr",
        json={"notification_type": "ISSUE_CREATED", "issue": {"id": 9}},
        headers={"Authorization": SECRET},
    )
    assert r.status == 200
    assert len(client.calls["reported"]) == 1


async def test_dispatches_issue_reported_legacy(client):
    r = await client.post(
        "/webhook/seerr",
        json={"notification_type": "ISSUE_REPORTED", "issue": {"id": 10}},
        headers={"Authorization": SECRET},
    )
    assert r.status == 200
    assert len(client.calls["reported"]) == 1


async def test_test_notification_no_dispatch(client):
    r = await client.post(
        "/webhook/seerr",
        json={"notification_type": "TEST_NOTIFICATION"},
        headers={"Authorization": SECRET},
    )
    assert r.status == 200
    assert client.calls["comment"] == []
    assert client.calls["reported"] == []
    assert client.calls["resolved"] == []


async def test_unknown_type_no_dispatch(client):
    r = await client.post(
        "/webhook/seerr",
        json={"notification_type": "MEDIA_PENDING"},
        headers={"Authorization": SECRET},
    )
    assert r.status == 200
    assert client.calls["comment"] == []


# --- error containment ---


async def test_handler_exception_returns_200():
    async def boom(payload: dict) -> None:
        raise RuntimeError("Telegram is down")

    app, _ = _make_app(on_comment=boom)
    async with TestClient(TestServer(app)) as c:
        r = await c.post(
            "/webhook/seerr",
            json={"notification_type": "ISSUE_COMMENT"},
            headers={"Authorization": SECRET},
        )
        assert r.status == 200


# --- size cap ---


async def test_rejects_oversize_content_length(client):
    # Set a bogus large Content-Length to trigger the early check.
    r = await client.post(
        "/webhook/seerr",
        data=b"x" * 100,
        headers={"Authorization": SECRET, "Content-Length": str(MAX_BODY_BYTES + 1)},
    )
    assert r.status == 413


# --- dedupe ---


async def test_duplicate_body_returns_duplicate(client):
    body = {"notification_type": "ISSUE_COMMENT", "issue": {"id": 7}, "comment": {"message": "x"}}
    r1 = await client.post("/webhook/seerr", json=body, headers={"Authorization": SECRET})
    r2 = await client.post("/webhook/seerr", json=body, headers={"Authorization": SECRET})
    assert r1.status == 200
    assert r2.status == 200
    assert await r2.text() == "duplicate"
    assert len(client.calls["comment"]) == 1


async def test_distinct_bodies_dont_dedupe(client):
    a = {"notification_type": "ISSUE_COMMENT", "issue": {"id": 1}}
    b = {"notification_type": "ISSUE_COMMENT", "issue": {"id": 2}}
    await client.post("/webhook/seerr", json=a, headers={"Authorization": SECRET})
    await client.post("/webhook/seerr", json=b, headers={"Authorization": SECRET})
    assert len(client.calls["comment"]) == 2


# --- malformed input ---


async def test_invalid_json_returns_400(client):
    r = await client.post(
        "/webhook/seerr",
        data=b"this is not json",
        headers={"Authorization": SECRET, "Content-Type": "application/json"},
    )
    assert r.status == 400


# --- healthcheck ---


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status == 200
    assert await r.text() == "ok"
