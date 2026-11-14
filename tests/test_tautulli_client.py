"""Wire tests for the Tautulli client: envelope unwrap, error results, ping,
POST commands, and the user-mapping helpers."""
from __future__ import annotations

import httpx
import pytest

from http_util import PermanentAPIError
from tautulli import (TautulliClient, find_user_by_plex_username,
                      find_user_for_share)


def _client(handler) -> TautulliClient:
    c = TautulliClient("http://tau.test:8181", "key123")
    c._client = httpx.AsyncClient(base_url="http://tau.test:8181",
                                  transport=httpx.MockTransport(handler))
    return c


def _ok(data):
    return {"response": {"result": "success", "message": None, "data": data}}


async def test_success_unwraps_data_and_sends_cmd_and_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        seen["path"] = request.url.path
        return httpx.Response(200, json=_ok({"rows": [1]}))

    c = _client(handler)
    assert await c.call("get_history", user_id=5, skip=None) == {"rows": [1]}
    assert seen["path"] == "/api/v2"
    assert seen["cmd"] == "get_history"
    assert seen["apikey"] == "key123"
    assert seen["user_id"] == "5"
    assert "skip" not in seen
    await c.close()


async def test_error_result_raises_with_message():
    def handler(request):
        return httpx.Response(200, json={"response": {
            "result": "error", "message": "Invalid apikey", "data": {}}})

    c = _client(handler)
    with pytest.raises(PermanentAPIError, match="Invalid apikey"):
        await c.call("get_users")
    await c.close()


async def test_unexpected_shape_raises():
    c = _client(lambda r: httpx.Response(200, json={"nope": 1}))
    with pytest.raises(PermanentAPIError):
        await c.call("get_users")
    await c.close()


async def test_ping_returns_tautulli_version():
    def handler(request):
        assert request.url.params["cmd"] == "get_tautulli_info"
        return httpx.Response(200, json=_ok({"tautulli_version": "v2.14.5"}))

    c = _client(handler)
    assert await c.ping() == "v2.14.5"
    await c.close()


async def test_post_command_uses_post_with_form_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["query"] = dict(request.url.params)
        seen["body"] = request.content.decode()
        return httpx.Response(200, json=_ok({}))

    c = _client(handler)
    await c.call("terminate_session", post=True, session_id="abc",
                 message="bye")
    assert seen["method"] == "POST"
    assert seen["query"]["cmd"] == "terminate_session"
    assert "session_id=abc" in seen["body"]
    assert "message=bye" in seen["body"]
    await c.close()


USERS = [
    {"user_id": 1, "username": "Alice", "email": "alice@example.com"},
    {"user_id": 2, "username": "bob", "email": "Bob@Example.com"},
]


def _users_client(counter=None) -> TautulliClient:
    def handler(request):
        if counter is not None:
            counter.append(1)
        return httpx.Response(200, json=_ok(USERS))
    return _client(handler)


async def test_find_user_matches_plex_username_in_other_case():
    c = _users_client()
    assert (await find_user_by_plex_username(c, "aLICE"))["user_id"] == 1
    await c.close()


async def test_find_user_returns_none_when_absent_or_blank():
    c = _users_client()
    assert await find_user_by_plex_username(c, "carol") is None
    assert await find_user_by_plex_username(c, "") is None
    assert await find_user_by_plex_username(c, None) is None
    await c.close()


async def test_share_matches_email_first_then_username():
    c = _users_client()
    assert (await find_user_for_share(c, "bob@example.com", "alice"))[
        "user_id"] == 2
    assert (await find_user_for_share(c, "x@example.com", "ALICE"))[
        "user_id"] == 1
    assert await find_user_for_share(c, "x@example.com", "zed") is None
    assert await find_user_for_share(c, "", "") is None
    await c.close()


async def test_users_are_cached_briefly():
    calls: list = []
    c = _users_client(calls)
    await find_user_by_plex_username(c, "alice")
    await find_user_by_plex_username(c, "bob")
    assert len(calls) == 1
    c._users_cache = (c._users_cache[0] - 120, c._users_cache[1])
    await find_user_by_plex_username(c, "bob")
    assert len(calls) == 2
    await c.close()


async def test_failed_post_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    c = _client(handler)
    with pytest.raises(Exception):
        await c.call("terminate_session", post=True, session_id="abc")
    assert len(calls) == 1
    await c.close()


async def test_get_history_returns_the_rows_and_get_stream_data_the_dict():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["cmd"] == "get_history":
            return httpx.Response(200, json=_ok(
                {"recordsTotal": 1, "data": [{"row_id": 4}, "junk"]}))
        assert request.url.params["row_id"] == "4"
        return httpx.Response(200, json=_ok({"transcode_decision": "copy"}))

    c = _client(handler)
    assert await c.get_history(user_id=1) == [{"row_id": 4}]
    assert await c.get_stream_data(4) == {"transcode_decision": "copy"}
    await c.close()
