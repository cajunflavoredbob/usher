"""Wire tests for the Plex sharing client methods: owned-server lookup,
the v1 XML section-id map, the v2 JSON invite POST, share listing, and
removal."""
from __future__ import annotations

import json

import httpx
import pytest

from plex import PlexClient


def _client(handler, tmp_path) -> PlexClient:
    c = PlexClient(client_id_path=tmp_path / "client_id")
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                headers={"Accept": "application/json"})
    return c


async def test_get_owned_server_filters_unowned(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Plex-Token"] == "tok"
        return httpx.Response(200, json=[
            {"provides": "server", "owned": 0, "clientIdentifier": "not-mine"},
            {"provides": "client", "owned": 1, "clientIdentifier": "a-client"},
            {"provides": "server", "owned": 1, "clientIdentifier": "mine",
             "name": "server1"},
        ])

    c = _client(handler, tmp_path)
    assert await c.get_owned_server("tok") == ("mine", "server1")
    await c.close()


async def test_get_owned_server_raises_without_server(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    c = _client(handler, tmp_path)
    with pytest.raises(LookupError):
        await c.get_owned_server("tok")
    await c.close()


async def test_get_library_sections_parses_global_ids(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/servers/mid-1")
        return httpx.Response(200, text="""<?xml version="1.0"?>
<MediaContainer>
  <Server name="server1">
    <Section id="112968621" key="1" type="movie" title="Movies"/>
    <Section id="112968662" key="3" type="show" title="TV Shows"/>
    <Section id="garbage" key="9" type="movie" title="Broken"/>
  </Server>
</MediaContainer>""")

    c = _client(handler, tmp_path)
    sections = await c.get_library_sections("tok", "mid-1")
    assert [(s.id, s.title) for s in sections] == [
        (112968621, "Movies"), (112968662, "TV Shows")]
    await c.close()


async def test_invite_posts_v2_shared_servers(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 1})

    c = _client(handler, tmp_path)
    await c.invite_to_server("tok", "mid-1", "friend@example.com",
                             [112968621, 112968662])
    assert captured["path"].endswith("/api/v2/shared_servers")
    assert captured["body"] == {
        "machineIdentifier": "mid-1",
        "invitedEmail": "friend@example.com",
        "librarySectionIds": [112968621, 112968662],
        "settings": {},
    }
    await c.close()


async def test_list_shares_parses_pending_and_accepted(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="""<?xml version="1.0"?>
<MediaContainer>
  <SharedServer id="71" username="friend" email="friend@example.com"
                acceptedAt="1700000000" allLibraries="1"/>
  <SharedServer id="72" username="" email="new@example.com"
                acceptedAt="" allLibraries="0"/>
</MediaContainer>""")

    c = _client(handler, tmp_path)
    shares = await c.list_shares("tok", "mid-1")
    assert [(s.id, s.accepted, s.all_libraries) for s in shares] == [
        (71, True, True), (72, False, False)]
    await c.close()


async def test_remove_share_deletes_v2(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(204)

    c = _client(handler, tmp_path)
    await c.remove_share("tok", 71)
    assert captured["method"] == "DELETE"
    assert captured["path"].endswith("/shared_servers/71")
    await c.close()
