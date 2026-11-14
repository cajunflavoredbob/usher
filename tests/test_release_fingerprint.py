"""Release fingerprint (#45): the pure comparison, the arr clients reading a
before-fingerprint around a Fix, the schema 4 migration, and the poller's
repeat wording."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from bot import autofix_poll
from bot.autofix_poll import (_notify_complete, _notify_timeout,
                              poll_pending_autofixes)
from fix_result import (ReleaseFingerprint, build_fingerprint,
                        compare_fingerprints)
from radarr import RadarrClient
from sonarr import SonarrClient
from store import PendingAutofix, UserStore
from tests._handler_harness import make_ctx, make_message

FP = ReleaseFingerprint


# --- the pure comparison ----------------------------------------------------

@pytest.mark.parametrize("before, after, expected", [
    # names known and equal, ignoring case
    (FP(release_name="Movie.2020.1080p-GRP"), FP(release_name="movie.2020.1080p-grp"), "same"),
    # equal names win even when other fields differ
    (FP(release_name="A", group="X", size=1), FP(release_name="a", group="Y", size=2), "same"),
    # names differ
    (FP(release_name="A"), FP(release_name="B"), "different"),
    # no names: group, quality and size all known and equal
    (FP(group="GRP", quality="WEBDL-1080p", size=100),
     FP(group="grp", quality="WEBDL-1080p", size=100), "same"),
    # size differs
    (FP(group="GRP", quality="WEBDL-1080p", size=100),
     FP(group="GRP", quality="WEBDL-1080p", size=101), "different"),
    # one of the three is missing on a side: equal known fields prove nothing
    (FP(group="GRP", quality="WEBDL-1080p"),
     FP(group="GRP", quality="WEBDL-1080p", size=100), "unknown"),
    # a field known on both sides that differs beats the missing ones
    (FP(group="GRP"), FP(group="OTHER", size=5), "different"),
    # names differ but group, quality and size all match: the second rule
    # applies before any difference (the two sides name one file differently)
    (FP(release_name="A", group="G", quality="Q", size=1),
     FP(release_name="B", group="G", quality="Q", size=1), "same"),
    # nothing comparable
    (FP(group="GRP"), FP(quality="Q"), "unknown"),
    (FP(), FP(), "unknown"),
    # one side empty
    (FP(), FP(release_name="A", group="G", quality="Q", size=1), "unknown"),
    (FP(release_name="A", group="G", quality="Q", size=1), FP(), "unknown"),
    # the downloadId is never compared
    (FP(release_name="A", download_id="1"), FP(release_name="A", download_id="2"), "same"),
    (FP(group="G", download_id="1"), FP(group="G", download_id="1", size=3), "unknown"),
])
def test_compare_fingerprints(before, after, expected):
    assert compare_fingerprints(before, after) == expected


def test_fingerprint_json_round_trips_and_empty_reads_empty():
    fp = FP(release_name="A", group="G", quality="Q", size=9, download_id="d")
    assert FP.from_json(fp.to_json()) == fp
    assert FP().to_json() == ""
    for raw in ("", None, "not json", "[1]", '{"size": "big"}'):
        assert not FP.from_json(raw).known


def test_build_fingerprint_uses_file_then_the_grab():
    history = [
        {"id": 2, "eventType": "downloadFolderImported", "downloadId": "D"},
        {"id": 1, "eventType": "grabbed", "downloadId": "D",
         "sourceTitle": "Grab.Title-GRP", "data": {"releaseGroup": "GRP"},
         "quality": {"quality": {"name": "HDTV-720p"}}},
    ]
    fp = build_fingerprint({"size": 50, "sceneName": "Scene.Name"}, history)
    assert fp == FP(release_name="Scene.Name", group="GRP", quality="HDTV-720p",
                    size=50, download_id="D")
    fp = build_fingerprint({"size": 50}, history)
    assert fp.release_name == "Grab.Title-GRP"
    assert build_fingerprint(None, []) == FP()


# --- Radarr and Sonarr workflows --------------------------------------------

MOVIE_FILE = {"id": 9, "sceneName": "Movie.2020.1080p-GRP", "releaseGroup": "GRP",
              "size": 4000, "quality": {"quality": {"name": "WEBDL-1080p"}}}
MOVIE = {"id": 5, "tmdbId": 42, "title": "Movie", "hasFile": True,
         "movieFile": MOVIE_FILE}
MOVIE_HISTORY = [
    {"id": 200, "eventType": "downloadFolderImported", "downloadId": "A"},
    {"id": 123, "eventType": "grabbed", "downloadId": "A",
     "sourceTitle": "Movie.2020.1080p-GRP"},
]


def _radarr(history_status=200):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/api/v3/movie" and request.method == "GET":
            return httpx.Response(200, json=[MOVIE])
        if path == "/api/v3/movie/5":
            return httpx.Response(200, json=MOVIE)
        if path == "/api/v3/history/movie":
            if history_status != 200:
                return httpx.Response(history_status, json={})
            return httpx.Response(200, json=MOVIE_HISTORY)
        if path.startswith("/api/v3/history/failed/") and request.method == "POST":
            return httpx.Response(200, json={})
        if path == "/api/v3/moviefile/9" and request.method == "DELETE":
            return httpx.Response(200, json={})
        if path == "/api/v3/command" and request.method == "POST":
            return httpx.Response(201, json={})
        return httpx.Response(404, json={})

    client = RadarrClient("http://radarr.test", "key")
    client._client = httpx.AsyncClient(base_url="http://radarr.test/api/v3",
                                       transport=httpx.MockTransport(handler))
    return client, requests


async def test_radarr_auto_fix_returns_the_before_fingerprint():
    client, _ = _radarr()
    result = await client.auto_fix(42)
    await client.close()
    assert result.status == "ok"
    assert result.before == FP(release_name="Movie.2020.1080p-GRP", group="GRP",
                               quality="WEBDL-1080p", size=4000, download_id="A")


async def test_radarr_still_deletes_and_searches_when_the_read_fails():
    client, requests = _radarr(history_status=500)
    result = await client.auto_fix(42)
    await client.close()
    assert result.steps_done == ["delete", "search"]
    assert not result.before.known
    assert any(r.method == "DELETE" for r in requests)


async def test_radarr_read_fingerprint_reads_the_file_now_on_disk():
    client, _ = _radarr()
    fp = await client.read_fingerprint(5)
    await client.close()
    assert fp.release_name == "Movie.2020.1080p-GRP" and fp.size == 4000


def _sonarr(file_status=200):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 8, "tvdbId": 42, "title": "Show"}])
        if path == "/api/v3/episode":
            return httpx.Response(200, json=[
                {"id": 501, "seasonNumber": 1, "episodeNumber": 5, "title": "Ep",
                 "hasFile": True, "episodeFileId": 9}])
        if path == "/api/v3/episode/501":
            return httpx.Response(200, json={"id": 501, "hasFile": True,
                                             "episodeFileId": 9})
        if path == "/api/v3/history":
            return httpx.Response(200, json={"records": [
                {"id": 2, "eventType": "downloadFolderImported", "downloadId": "A"},
                {"id": 1, "eventType": "grabbed", "downloadId": "A",
                 "sourceTitle": "Show.S01E05-GRP"}]})
        if path == "/api/v3/episodefile/9" and request.method == "GET":
            if file_status != 200:
                return httpx.Response(file_status, json={})
            return httpx.Response(200, json={
                "size": 700, "releaseGroup": "GRP",
                "quality": {"quality": {"name": "HDTV-720p"}}})
        if path == "/api/v3/episodefile/9" and request.method == "DELETE":
            return httpx.Response(200, json={})
        if path == "/api/v3/command" and request.method == "POST":
            return httpx.Response(201, json={})
        return httpx.Response(404, json={})

    client = SonarrClient("http://sonarr.test", "key")
    client._client = httpx.AsyncClient(base_url="http://sonarr.test/api/v3",
                                       transport=httpx.MockTransport(handler))
    return client, requests


async def test_sonarr_auto_fix_returns_the_before_fingerprint():
    client, _ = _sonarr()
    result = await client.auto_fix_episode(42, 1, 5)
    await client.close()
    assert result.status == "ok"
    assert result.before == FP(release_name="Show.S01E05-GRP", group="GRP",
                               quality="HDTV-720p", size=700, download_id="A")


async def test_sonarr_still_deletes_and_searches_when_the_read_fails():
    client, requests = _sonarr(file_status=500)
    result = await client.auto_fix_episode(42, 1, 5)
    await client.close()
    assert result.steps_done == ["delete", "search"]
    assert not result.before.known
    assert any(r.method == "DELETE" for r in requests)


async def test_sonarr_read_fingerprint_reads_the_file_now_on_disk():
    client, _ = _sonarr()
    fp = await client.read_fingerprint(501)
    await client.close()
    assert fp.group == "GRP" and fp.size == 700


# --- store: schema 3 to 4 ----------------------------------------------------

async def test_schema_3_database_migrates_and_old_row_reads_empty(
        tmp_db_path: Path, fresh_token_crypto):
    with sqlite3.connect(tmp_db_path) as c:
        c.execute(
            """CREATE TABLE pending_autofixes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL, media_type TEXT NOT NULL,
                radarr_movie_id INTEGER, sonarr_series_id INTEGER,
                sonarr_episode_id INTEGER, sonarr_season INTEGER,
                expected_episode_ids TEXT NOT NULL DEFAULT '[]',
                label TEXT NOT NULL, issue_id INTEGER NOT NULL,
                issue_url TEXT NOT NULL, started_at TEXT NOT NULL DEFAULT (datetime('now')),
                timeout_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                message_id INTEGER, last_progress TEXT NOT NULL DEFAULT '',
                bumped INTEGER NOT NULL DEFAULT 0,
                reply_markup TEXT NOT NULL DEFAULT '',
                action_name TEXT NOT NULL DEFAULT '')""")
        c.execute(
            "INSERT INTO pending_autofixes (chat_id, user_id, media_type, "
            "radarr_movie_id, label, issue_id, issue_url, timeout_at) "
            "VALUES (1, 2, 'movie', 5, 'Old', 7, 'u', datetime('now', '+1 hours'))")
        c.execute("PRAGMA user_version = 3")
    store = UserStore(tmp_db_path, crypto=fresh_token_crypto)
    with sqlite3.connect(tmp_db_path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 4
    [old] = await store.list_pending_autofixes()
    assert old.before_fingerprint == FP()
    await store.add_pending_autofix(
        chat_id=1, user_id=2, media_type="movie", label="New", ticket_id=8,
        ticket_url="u", radarr_movie_id=6,
        before_fingerprint=FP(release_name="X", size=3))
    new = (await store.list_pending_autofixes())[1]
    assert new.before_fingerprint == FP(release_name="X", size=3)


# --- poller ------------------------------------------------------------------

def _fix(before: ReleaseFingerprint) -> PendingAutofix:
    now = datetime.now(timezone.utc)
    return PendingAutofix(
        id=1, chat_id=100, user_id=42, media_type="movie", radarr_movie_id=555,
        sonarr_series_id=None, sonarr_episode_id=None, sonarr_season=None,
        expected_episode_ids=[], label="Test Movie", ticket_id=7,
        ticket_url="http://seerr.example/issues/7",
        started_at=now.strftime("%Y-%m-%d %H:%M:%S"),
        timeout_at=(now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
        message_id=300, before_fingerprint=before)


@pytest.fixture(autouse=True)
def _clear_inflight():
    autofix_poll._inflight.clear()
    yield
    autofix_poll._inflight.clear()


async def _tick(before, after, monkeypatch):
    # Another test file swaps these module attributes for fakes for good;
    # put back the real ones (bound at import) for the poller under test.
    monkeypatch.setattr(autofix_poll, "_notify_complete", _notify_complete)
    monkeypatch.setattr(autofix_poll, "_notify_timeout", _notify_timeout)
    fix = _fix(before)
    fix.is_complete = AsyncMock(return_value=(True, ""))  # type: ignore[method-assign]
    ctx = make_ctx()
    ctx.bot_data["radarr"].read_fingerprint = (
        after if isinstance(after, AsyncMock) else AsyncMock(return_value=after))
    ctx.bot_data["radarr"].get_queue_progress = AsyncMock(return_value=None)
    store = ctx.bot_data["store"]
    store.list_pending_autofixes = AsyncMock(return_value=[fix])
    store.mark_autofix_status = AsyncMock()
    store.set_autofix_progress = AsyncMock()
    ctx.bot.send_message = AsyncMock(return_value=make_message(message_id=900))
    ctx.bot.edit_message_text = AsyncMock()
    await poll_pending_autofixes(ctx)
    return ctx


def _buttons(ctx):
    markup = ctx.bot.send_message.call_args.kwargs["reply_markup"]
    return [b.text for row in markup.inline_keyboard for b in row]


async def test_repeat_paints_the_card_and_sends_the_repeat_dm(monkeypatch):
    fp = FP(release_name="Movie.2020-GRP", group="GRP", quality="WEBDL-1080p", size=9)
    ctx = await _tick(fp, fp, monkeypatch)
    card = ctx.bot.edit_message_text.call_args.kwargs["text"]
    assert "looks like the same release (GRP, WEBDL-1080p)" in card
    assert "probably didn't fix the problem" in card
    dm = ctx.bot.send_message.call_args.kwargs["text"]
    assert "looks like the same release (GRP, WEBDL-1080p)" in dm
    assert _buttons(ctx) == ["✅ Yes, close it", "💬 No, add a comment",
                       "🙋 Ask the Admin to try a different release"]


async def test_unknown_comparison_keeps_todays_wording(monkeypatch):
    ctx = await _tick(FP(group="GRP"), FP(quality="Q"), monkeypatch)
    assert "Replacement downloaded." in ctx.bot.edit_message_text.call_args.kwargs["text"]
    assert "same release" not in ctx.bot.send_message.call_args.kwargs["text"]


async def test_different_release_keeps_todays_wording(monkeypatch):
    ctx = await _tick(FP(release_name="A"), FP(release_name="B"), monkeypatch)
    assert "Replacement downloaded." in ctx.bot.edit_message_text.call_args.kwargs["text"]


async def test_failed_after_read_keeps_todays_wording_and_still_notifies(monkeypatch):
    ctx = await _tick(FP(release_name="A"),
                      AsyncMock(side_effect=RuntimeError("boom")), monkeypatch)
    assert "Replacement downloaded." in ctx.bot.edit_message_text.call_args.kwargs["text"]
    ctx.bot.send_message.assert_awaited_once()


async def test_empty_before_fingerprint_never_reads_the_file(monkeypatch):
    after = AsyncMock(return_value=FP(release_name="A"))
    await _tick(FP(), after, monkeypatch)
    after.assert_not_awaited()


async def test_radarr_mark_failed_also_returns_the_before_fingerprint():
    client, _ = _radarr()
    result = await client.mark_failed(42)
    await client.close()
    assert result.steps_done == ["blocklist", "delete", "search"]
    assert result.before.release_name == "Movie.2020.1080p-GRP"
