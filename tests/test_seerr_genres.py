"""Wire-level tests for Seerr's genre list and genre discover paths."""
from __future__ import annotations

import httpx
import pytest

import seerr as seerr_mod
from seerr import SeerrClient


def _client(monkeypatch, handler) -> SeerrClient:
    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(seerr_mod.httpx, "AsyncClient", patched)
    return SeerrClient("http://seerr.test", "key")


async def test_genres_returns_id_name_pairs_and_skips_bad_rows(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/genres/tv")
        return httpx.Response(200, json=[
            {"id": 10759, "name": "Action & Adventure"}, {"id": "x", "name": "?"},
            {"id": 36}, {"id": 35, "name": "Comedy"}])

    client = _client(monkeypatch, handler)
    assert await client.genres("tv") == [(10759, "Action & Adventure"),
                                         (35, "Comedy")]
    with pytest.raises(ValueError):
        await client.genres("person")


async def test_discover_genre_uses_type_path_and_tags_media_type(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"results": [
            {"id": 5, "title": "A Film", "releaseDate": "2020-01-01"}]})

    client = _client(monkeypatch, handler)
    out = await client.discover_genre("movie", 28)
    assert seen["path"].endswith("/discover/movies/genre/28")
    assert [(r.media_type, r.tmdb_id, r.title) for r in out] == [
        ("movie", 5, "A Film")]
    await client.discover_genre("tv", 18)
    assert seen["path"].endswith("/discover/tv/genre/18")


async def test_similar_titles_prefers_recommendations(monkeypatch):
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"results": [
            {"id": 5, "title": "Rec", "releaseDate": "2020-01-01"}]})

    out = await _client(monkeypatch, handler).similar_titles("movie", 11)
    assert [p.split("/api/v1")[-1] for p in paths] == ["/movie/11/recommendations"]
    assert [(r.media_type, r.tmdb_id) for r in out] == [("movie", 5)]


async def test_similar_titles_falls_back_to_similar_then_empty(monkeypatch):
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path.split("/api/v1")[-1])
        if request.url.path.endswith("/similar"):
            return httpx.Response(200, json={"results": [
                {"id": 8, "name": "Sim", "firstAirDate": "2019-01-01"}]})
        return httpx.Response(200, json={"results": []})

    client = _client(monkeypatch, handler)
    out = await client.similar_titles("tv", 3)
    assert paths == ["/tv/3/recommendations", "/tv/3/similar"]
    assert [(r.media_type, r.tmdb_id) for r in out] == [("tv", 8)]

    with pytest.raises(ValueError):
        await client.similar_titles("person", 3)


async def test_similar_titles_both_empty_is_empty(monkeypatch):
    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    assert await _client(monkeypatch, empty).similar_titles("tv", 3) == []
