"""Wire tests for the Plex watchlist client: the TMDb to Discover ratingKey
lookup (a Discover title search, then each candidate's tmdb:// guids) and the
watchlist PUT. The lookup's calls are the ones docs/prior-art settled in the
#71 spike."""
from __future__ import annotations

import httpx
import pytest

from plex import PlexClient, PlexTokenRejectedError

SEARCH_PATH = "/library/search"


def _client(handler, tmp_path) -> PlexClient:
    c = PlexClient(client_id_path=tmp_path / "client_id")
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                headers={"Accept": "application/json"})
    return c


def _search(*items):
    return {"MediaContainer": {"SearchResults": [
        {"id": "external", "SearchResult": [{"Metadata": md} for md in items]},
    ]}}


def _md(key, title, year, kind="movie"):
    return {"ratingKey": key, "title": title, "year": year, "type": kind,
            "guid": f"plex://{kind}/{key}"}


def _guids(*ids):
    return {"MediaContainer": {"Metadata": [
        {"Guid": [{"id": i} for i in ids]}]}}


def _handler(search_body, guids_by_key, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        assert request.headers["X-Plex-Token"] == "tok"
        if request.url.path == SEARCH_PATH:
            return httpx.Response(200, json=search_body)
        key = request.url.path.rsplit("/", 1)[-1]
        body = guids_by_key.get(key)
        if isinstance(body, int):
            return httpx.Response(body, json={})
        return httpx.Response(200, json=body or _guids())
    return handler


async def test_lookup_picks_the_remake_whose_tmdb_guid_matches(tmp_path):
    calls = []
    search = _search(_md("aaa", "The Thing", 1982), _md("bbb", "The Thing", 2011))
    c = _client(_handler(search, {
        "aaa": _guids("imdb://tt0084787", "tmdb://1091", "tvdb://1"),
        "bbb": _guids("imdb://tt0905372", "tmdb://60935"),
    }, calls), tmp_path)
    assert await c.find_discover_key("tok", "movie", 60935, "The Thing", "2011") == "bbb"
    search_req = calls[0]
    assert search_req.url.host == "discover.provider.plex.tv"
    assert search_req.url.params["query"] == "The Thing"
    assert search_req.url.params["searchTypes"] == "movies"
    assert search_req.url.params["searchProviders"] == "discover"
    assert search_req.url.params["includeMetadata"] == "1"
    assert all(r.url.host == "metadata.provider.plex.tv" for r in calls[1:])
    await c.close()


async def test_lookup_skips_candidates_with_no_guids(tmp_path):
    search = _search(_md("aaa", "Dune", 2021), _md("bbb", "Dune", 2021))
    c = _client(_handler(search, {
        "aaa": _guids(),
        "bbb": _guids("tmdb://438631", "tmdb://999999"),
    }), tmp_path)
    assert await c.find_discover_key("tok", "movie", 438631, "Dune", "2021") == "bbb"
    await c.close()


async def test_lookup_uses_guids_already_in_the_search_result(tmp_path):
    calls = []
    md = _md("aaa", "Dune", 1984)
    md["Guid"] = [{"id": "tmdb://841"}]
    c = _client(_handler(_search(md), {}, calls), tmp_path)
    assert await c.find_discover_key("tok", "movie", 841, "Dune", "1984") == "aaa"
    assert [r.url.path for r in calls] == [SEARCH_PATH]
    await c.close()


async def test_lookup_for_a_show_searches_tv_and_keeps_shows(tmp_path):
    calls = []
    search = _search(_md("mmm", "Severance", 2022, kind="movie"),
                     _md("sss", "Severance", 2022, kind="show"))
    c = _client(_handler(search, {
        "mmm": _guids("tmdb://95396"),
        "sss": _guids("tmdb://95396", "tvdb://371980"),
    }, calls), tmp_path)
    assert await c.find_discover_key("tok", "tv", 95396, "Severance", "2022") == "sss"
    assert calls[0].url.params["searchTypes"] == "tv"
    await c.close()


async def test_lookup_returns_none_when_no_candidate_matches(tmp_path):
    search = _search(_md("aaa", "The Thing", 1982))
    c = _client(_handler(search, {"aaa": _guids("tmdb://1091")}), tmp_path)
    assert await c.find_discover_key("tok", "movie", 60935, "The Thing", "2011") is None
    await c.close()


async def test_lookup_returns_none_for_an_empty_search(tmp_path):
    c = _client(_handler({"MediaContainer": {}}, {}), tmp_path)
    assert await c.find_discover_key("tok", "movie", 1, "Nothing", "2000") is None
    await c.close()


async def test_lookup_skips_a_candidate_whose_metadata_fails(tmp_path):
    search = _search(_md("aaa", "Dune", 2021), _md("bbb", "Dune", 2021))
    c = _client(_handler(search, {"aaa": 404, "bbb": _guids("tmdb://438631")}),
                tmp_path)
    assert await c.find_discover_key("tok", "movie", 438631, "Dune", "2021") == "bbb"
    await c.close()


async def test_lookup_checks_close_years_first(tmp_path):
    calls = []
    search = _search(_md("old", "Dune", 1984), _md("new", "Dune", 2021))
    c = _client(_handler(search, {
        "old": _guids("tmdb://841"), "new": _guids("tmdb://438631"),
    }, calls), tmp_path)
    assert await c.find_discover_key("tok", "movie", 438631, "Dune", "2021") == "new"
    assert [r.url.path.rsplit("/", 1)[-1] for r in calls[1:]] == ["new"]
    await c.close()


async def test_lookup_checks_exact_titles_before_loose_matches(tmp_path):
    # A live search for "Battlestar Galactica" returned about 30 loosely
    # related shows (Galactica 1980, Galactik Football, ...) around the one.
    calls = []
    loose = [_md(f"x{i}", f"Galactic Thing {i}", 2004, kind="show")
             for i in range(12)]
    search = _search(*loose, _md("bsg", "Battlestar Galactica", 2004, kind="show"))
    c = _client(_handler(search, {"bsg": _guids("tmdb://1972")}, calls), tmp_path)
    assert await c.find_discover_key(
        "tok", "tv", 1972, "Battlestar Galactica", "2004") == "bsg"
    assert [r.url.path.rsplit("/", 1)[-1] for r in calls[1:]] == ["bsg"]
    await c.close()


@pytest.mark.parametrize("status", [401, 403])
async def test_lookup_raises_token_rejected_on_auth_failure(tmp_path, status):
    def handler(request):
        return httpx.Response(status, json={})
    c = _client(handler, tmp_path)
    with pytest.raises(PlexTokenRejectedError):
        await c.find_discover_key("tok", "movie", 1, "X", "2000")
    await c.close()


async def test_lookup_raises_token_rejected_when_metadata_refuses_it(tmp_path):
    c = _client(_handler(_search(_md("aaa", "X", 2000)), {"aaa": 401}), tmp_path)
    with pytest.raises(PlexTokenRejectedError):
        await c.find_discover_key("tok", "movie", 1, "X", "2000")
    await c.close()


async def test_add_to_watchlist_puts_the_rating_key_with_the_token(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})
    c = _client(handler, tmp_path)
    await c.add_to_watchlist("tok", "5d77682a151a60001f24b5c0")
    (req,) = calls
    assert req.method == "PUT"
    assert req.url.host == "discover.provider.plex.tv"
    assert req.url.path == "/actions/addToWatchlist"
    assert req.url.params["ratingKey"] == "5d77682a151a60001f24b5c0"
    assert req.headers["X-Plex-Token"] == "tok"
    await c.close()


async def test_add_to_watchlist_raises_token_rejected_on_401(tmp_path):
    def handler(request):
        return httpx.Response(401, json={})
    c = _client(handler, tmp_path)
    with pytest.raises(PlexTokenRejectedError):
        await c.add_to_watchlist("tok", "abc")
    await c.close()
