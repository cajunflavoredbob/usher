"""Tests for SeerrClient.get_issue comment-thread parsing (v0.11.23).

Seerr stores the original report as comments[0] and the reply thread as the
rest. get_issue must split them: comments[0] -> description, the rest ->
the `comments` list (author/message/timestamp). Previously the whole thread
after comments[0] was discarded, so the ticket view never showed replies.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import seerr
from seerr import SeerrClient


def _client(monkeypatch, payload: dict) -> SeerrClient:
    """SeerrClient whose execute() returns `payload` as the issue JSON."""
    c = SeerrClient("http://seerr.example:5056", "api-key", timeout=10.0)

    async def fake_execute(*args, **kwargs):
        return SimpleNamespace(json=lambda: payload)

    monkeypatch.setattr(seerr, "execute", fake_execute)
    return c


def _payload(comments):
    return {
        "id": 42,
        "issueType": 1,
        "status": 1,
        "createdAt": "2026-06-16T00:00:00.000Z",
        "media": {"tmdbId": 123, "mediaType": "movie"},
        "createdBy": {"displayName": "Nathan", "plexUsername": "nathan"},
        "comments": comments,
    }


async def test_first_comment_is_description_rest_is_thread(monkeypatch):
    c = _client(monkeypatch, _payload([
        {"message": "Michael won't play", "user": {"displayName": "Nathan"},
         "createdAt": "2026-06-16T00:00:00.000Z"},
        {"message": "looking into it", "user": {"displayName": "Kenny"},
         "createdAt": "2026-06-16T01:00:00.000Z"},
        {"message": "any update?", "user": {"displayName": "Nathan"},
         "createdAt": "2026-06-16T02:00:00.000Z"},
    ]))
    issue = await c.get_issue(42)
    assert issue.description == "Michael won't play"
    assert [(x.author, x.message) for x in issue.comments] == [
        ("Kenny", "looking into it"),
        ("Nathan", "any update?"),
    ]
    # Timestamps preserved, oldest-first (Seerr order).
    assert issue.comments[0].created_at == "2026-06-16T01:00:00.000Z"


async def test_plex_username_fallback_for_author(monkeypatch):
    c = _client(monkeypatch, _payload([
        {"message": "report", "user": {"displayName": "Nathan"}},
        {"message": "reply", "user": {"plexUsername": "kenny_plex"}},
    ]))
    issue = await c.get_issue(42)
    assert issue.comments[0].author == "kenny_plex"


async def test_missing_user_yields_question_mark(monkeypatch):
    c = _client(monkeypatch, _payload([
        {"message": "report"},
        {"message": "reply with no user"},
    ]))
    issue = await c.get_issue(42)
    assert issue.comments[0].author == "?"


async def test_empty_thread_comments_skipped(monkeypatch):
    c = _client(monkeypatch, _payload([
        {"message": "report", "user": {"displayName": "Nathan"}},
        {"message": "   ", "user": {"displayName": "Kenny"}},   # whitespace-only
        {"message": "real reply", "user": {"displayName": "Kenny"}},
    ]))
    issue = await c.get_issue(42)
    assert [x.message for x in issue.comments] == ["real reply"]


async def test_no_comments_means_empty(monkeypatch):
    c = _client(monkeypatch, _payload([]))
    issue = await c.get_issue(42)
    assert issue.description == ""
    assert issue.comments == []


async def test_only_original_report_no_replies(monkeypatch):
    c = _client(monkeypatch, _payload([
        {"message": "just the report", "user": {"displayName": "Nathan"}},
    ]))
    issue = await c.get_issue(42)
    assert issue.description == "just the report"
    assert issue.comments == []
