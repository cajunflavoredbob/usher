"""Async client for the Seerr REST API."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional, Union

import httpx

from http_util import APIError, PermanentAPIError, execute

logger = logging.getLogger("hermes." + __name__)

_SERVICE = "Seerr"


class AmbiguousResponseError(PermanentAPIError):
    """A 2xx from Seerr with an unusable body (empty, HTML from a proxy,
    missing required fields). For writes the side effect may have LANDED,
    so callers must not blind-retry (a retried create_issue
    files a duplicate)."""
    def __init__(self, detail: str):
        super().__init__(detail, status_code=None, service=_SERVICE)


def _json_or_raise(r: httpx.Response, *, what: str,
                   expect: type = dict) -> Union[dict, list]:
    """Parse a 2xx body; raise a clean AmbiguousResponseError instead of a
    bare ValueError when the body isn't JSON (or isn't the expected shape).
    execute() only guarantees the status code, not the body."""
    try:
        data = r.json()
    except Exception as exc:
        raise AmbiguousResponseError(
            f"Seerr returned an unreadable response for {what}") from exc
    if expect is not None and not isinstance(data, expect):
        raise AmbiguousResponseError(
            f"Seerr returned an unexpected response shape for {what}")
    return data


class PlexTokenInvalidError(PermanentAPIError):
    """Seerr rejected the stored Plex token on /auth/plex (revoked or
    expired). Retrying can't help; the user must re-link. Subclasses
    PermanentAPIError so any surface without a dedicated re-link prompt
    still renders a truthful message instead of "try again in a minute"."""
    def __init__(self):
        super().__init__("your Plex sign-in is no longer valid",
                         status_code=500, service=_SERVICE)

# Per-Plex-token authenticated client cache. Reuses warm clients under a
# webhook comment flood instead of paying the TCP-handshake + /auth/plex
# cost on every call. LRU + TTL bounded so a token flood
# doesn't blow up FD count.
_USER_CLIENT_TTL_S = 300.0
_USER_CLIENT_MAX = 32
# Grace before an evicted/expired user client is actually closed; must
# outlive the 15s per-request timeout so no in-flight request is killed.
_USER_CLIENT_CLOSE_GRACE_S = 60.0


@dataclass
class MediaResult:
    """One search hit from Seerr."""
    media_type: str       # "movie" or "tv"
    tmdb_id: int          # TMDb ID (used for details lookup + Radarr/Sonarr auto-fix)
    title: str
    year: str             # may be empty string
    seerr_media_id: Optional[int]  # Seerr's internal media.id (used as `mediaId` for issue creation).
                                   # None means this media isn't yet in Seerr's library.


@dataclass
class CreatedIssue:
    id: int
    url: str


@dataclass
class TvSeason:
    season_number: int
    episode_count: int


@dataclass
class IssueComment:
    """One reply in an issue's comment thread (after the original report)."""
    author: str                # displayName / plexUsername of the commenter
    message: str
    created_at: str = ""       # ISO 8601


@dataclass
class IssueListItem:
    id: int
    issue_type: int            # 1=Video, 2=Audio, 3=Subtitle, 4=Other
    status: int                # 1=open, 2=resolved
    created_at: str            # ISO 8601
    tmdb_id: int
    media_type: str            # "movie" or "tv"
    problem_season: Optional[int]
    problem_episode: Optional[int]
    created_by: str            # displayName
    description: str = ""      # Original issue text; populated by get_issue (Seerr stores it
                               # as the first entry in the issue's comments array). May be
                               # empty for IssueListItems returned by list_issues, which
                               # doesn't include comments.
    comments: list = field(default_factory=list)  # Reply thread AFTER the original report
                               # (list of IssueComment); populated only by get_issue, empty
                               # for list_issues entries.


class SeerrClient:
    """Thin wrapper around the Seerr v1 API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 10.0,
        public_url: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        # public_url is used only for user-facing links (e.g. the "View:" URL
        # sent in Telegram). API calls always use base_url. Falls back to
        # base_url when not set so existing setups keep working.
        self.public_url = (public_url.rstrip("/") if public_url else self.base_url)
        self._client = httpx.AsyncClient(
            base_url=f"{self.base_url}/api/v1",
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
            timeout=timeout,
        )
        # Per-Plex-token authenticated client cache. Value is
        # (httpx.AsyncClient, expires_at_monotonic). Cache owns aclose().
        self._user_clients: "OrderedDict[str, tuple[httpx.AsyncClient, float]]" = OrderedDict()
        # Serializes cache get-or-create: two concurrent misses
        # for one token would otherwise both POST /auth/plex and orphan the
        # loser's client (never closed).
        self._user_clients_lock = asyncio.Lock()
        # Clients evicted while possibly mid-request; closed after a grace
        # period instead of immediately.
        self._retired_user_clients: set[httpx.AsyncClient] = set()
        self._deferred_close_tasks: set[asyncio.Task] = set()

    def _close_later(self, client: httpx.AsyncClient) -> None:
        """Retire an evicted/expired user client. An immediate aclose() would
        kill any request another coroutine is mid-flight on; the grace period
        comfortably outlives the 15s request timeout."""
        self._retired_user_clients.add(client)

        async def _close() -> None:
            try:
                await asyncio.sleep(_USER_CLIENT_CLOSE_GRACE_S)
                await client.aclose()
            except Exception:
                logger.warning("deferred aclose on retired user client failed",
                               exc_info=True)
            finally:
                self._retired_user_clients.discard(client)

        task = asyncio.get_running_loop().create_task(_close())
        self._deferred_close_tasks.add(task)
        task.add_done_callback(self._deferred_close_tasks.discard)

    async def close(self) -> None:
        await self._client.aclose()
        for task in list(self._deferred_close_tasks):
            task.cancel()
        if self._deferred_close_tasks:
            # Let cancellations settle so no task outlives the event loop.
            await asyncio.gather(*self._deferred_close_tasks, return_exceptions=True)
        for client in list(self._retired_user_clients):
            try:
                await client.aclose()
            except Exception:
                logger.warning("aclose on retired user client failed", exc_info=True)
        self._retired_user_clients.clear()
        for client, _ in list(self._user_clients.values()):
            try:
                await client.aclose()
            except Exception:
                logger.warning("aclose on cached user client failed", exc_info=True)
        self._user_clients.clear()

    async def get_main_settings(self) -> dict:
        """Seerr's main settings (admin API key; read-only). Backs the admin
        panel's New-Plex-Sign-In warning. Raises APIError on failure."""
        r = await execute(self._client, "GET", "/settings/main", service=_SERVICE)
        return _json_or_raise(r, what="main settings")

    async def ping(self) -> str:
        """Return Seerr's version string. Raises APIError on failure."""
        r = await execute(self._client, "GET", "/status", service=_SERVICE)
        data = _json_or_raise(r, what="status")
        return data.get("version", "?")

    async def login_with_plex(self, plex_token: str) -> tuple[int, str]:
        """Authenticate to Seerr as a Plex user. Returns (seerr_user_id,
        display_name). (The response cookies were previously returned too;
        dropped in 0.12.0 -- the cookie-transfer approach was abandoned and
        the only caller discarded them.)"""
        r = await execute(self._client, "POST", "/auth/plex", service=_SERVICE,
                          json={"authToken": plex_token})
        data = _json_or_raise(r, what="sign-in")
        if "id" not in data:
            raise AmbiguousResponseError("Seerr's sign-in response was missing the user id")
        return (
            int(data["id"]),
            data.get("displayName") or data.get("plexUsername") or data.get("username") or "?",
        )

    async def _as_user(self, plex_token: str) -> httpx.AsyncClient:
        """Return an authenticated user client, reusing a warm one if cached.

        Auth and subsequent calls happen on the SAME client so the session
        cookie jar persists naturally (transferring cookies across clients
        was unreliable).

        Cache key is the Plex token. Entries expire after _USER_CLIENT_TTL_S
        or LRU eviction at _USER_CLIENT_MAX. The cache owns each client's
        lifecycle -- callers MUST NOT aclose() the returned client. Drained
        by SeerrClient.close() at shutdown.

        The whole get-or-create runs under _user_clients_lock:
        without it, two concurrent misses for one token both POST /auth/plex
        and the losing client leaks. Contention is negligible -- the lock is
        only held across the network call on a cold or expired entry.
        """
        async with self._user_clients_lock:
            now = time.monotonic()
            entry = self._user_clients.get(plex_token)
            if entry is not None:
                client, expires = entry
                if now < expires:
                    self._user_clients.move_to_end(plex_token)
                    return client
                # Stale -- evict + retire (deferred close: a request may
                # still be running on it), then mint a new one.
                self._user_clients.pop(plex_token, None)
                self._close_later(client)

            new_client = httpx.AsyncClient(
                base_url=f"{self.base_url}/api/v1",
                headers={"Accept": "application/json"},
                timeout=15.0,
            )
            try:
                await execute(new_client, "POST", "/auth/plex", service=_SERVICE,
                              json={"authToken": plex_token})
            except APIError as exc:
                await new_client.aclose()
                # Seerr reports a revoked/expired Plex token as a 500 "Unable to
                # authenticate." -- which classify_response reads as transient.
                # It isn't: plex.tv rejected the token upstream (422) and only a
                # re-link fixes it. 401/403 (or a revoked Seerr membership)
                # deserve the same re-link path.
                if ("unable to authenticate" in str(exc).lower()
                        or exc.status_code in (401, 403)):
                    raise PlexTokenInvalidError() from exc
                raise
            except Exception:
                await new_client.aclose()
                raise
            self._user_clients[plex_token] = (new_client, now + _USER_CLIENT_TTL_S)
            self._user_clients.move_to_end(plex_token)
            while len(self._user_clients) > _USER_CLIENT_MAX:
                _, (evict_client, _) = self._user_clients.popitem(last=False)
                self._close_later(evict_client)
            return new_client

    async def _client_for(self, as_plex_token: Optional[str]) -> httpx.AsyncClient:
        """The per-user client for token-attributed calls, else the admin-key
        client. Collapses the if/else previously pasted across five methods
       ."""
        if as_plex_token:
            return await self._as_user(as_plex_token)
        return self._client

    async def search(self, query: str, limit: int = 5) -> list[MediaResult]:
        """Search Seerr for movies + TV shows matching the query."""
        r = await execute(self._client, "GET", "/search", service=_SERVICE,
                          params={"query": query})
        data = _json_or_raise(r, what="search")
        out: list[MediaResult] = []
        for item in data.get("results", []):
            mt = item.get("mediaType")
            if mt not in ("movie", "tv"):
                continue  # skip "person" and anything else
            title = item.get("title") or item.get("name") or "?"
            release = item.get("releaseDate") or item.get("firstAirDate") or ""
            year = release[:4] if release else ""
            media_info = item.get("mediaInfo") or {}
            out.append(MediaResult(
                media_type=mt,
                tmdb_id=item.get("id"),
                title=title,
                year=year,
                seerr_media_id=media_info.get("id"),
            ))
            if len(out) >= limit:
                break
        return out

    async def get_tv_seasons(self, tmdb_id: int) -> tuple[list[TvSeason], Optional[int]]:
        """Return (seasons, tvdb_id) for a TV show. Includes season 0 because
        anime movies / OVAs / tie-in specials often live there and users need
        to report issues on them. (Season NAMES are not fetched: the picker
        renders S<number> buttons only.)"""
        r = await execute(self._client, "GET", f"/tv/{tmdb_id}", service=_SERVICE)
        data = _json_or_raise(r, what="TV seasons")
        seasons: list[TvSeason] = []
        for s in data.get("seasons", []):
            n = s.get("seasonNumber")
            if n is None:
                continue
            seasons.append(TvSeason(
                season_number=n,
                episode_count=s.get("episodeCount", 0),
            ))
        external = data.get("externalIds") or {}
        tvdb_id = external.get("tvdbId")
        return seasons, tvdb_id

    async def list_issues(
        self,
        *,
        filter: str = "open",
        take: int = 25,
        as_plex_token: Optional[str] = None,
    ) -> tuple[list[IssueListItem], int]:
        """List issues. If as_plex_token is provided, authenticates as that
        user (gets their visible issues only). Else returns all (admin view).
        Returns (items, total): total is Seerr's full matching count, which
        can exceed len(items) when the list is truncated at `take` (audit
        P2-3: the cap was silent and issues 26+ were invisible)."""
        client = await self._client_for(as_plex_token)
        r = await execute(client, "GET", "/issue", service=_SERVICE,
                          params={"filter": filter, "take": take})
        data = _json_or_raise(r, what="issue list")
        out: list[IssueListItem] = []
        for item in data.get("results", []):
            media = item.get("media") or {}
            created_by = item.get("createdBy") or {}
            out.append(IssueListItem(
                id=item["id"],
                issue_type=item.get("issueType", 4),
                status=item.get("status", 0),
                created_at=item.get("createdAt", ""),
                tmdb_id=media.get("tmdbId", 0),
                media_type=media.get("mediaType", ""),
                problem_season=item.get("problemSeason"),
                problem_episode=item.get("problemEpisode"),
                created_by=created_by.get("displayName") or created_by.get("plexUsername") or "?",
            ))
        page_info = data.get("pageInfo") or {}
        total = page_info.get("results")
        if not isinstance(total, int) or total < len(out):
            total = len(out)
        return out, total

    async def get_issue(
        self,
        issue_id: int,
        *,
        as_plex_token: Optional[str] = None,
    ) -> IssueListItem:
        """Fetch a single issue by id. Same shape as list_issues entries."""
        client = await self._client_for(as_plex_token)
        r = await execute(client, "GET", f"/issue/{issue_id}", service=_SERVICE)
        d = _json_or_raise(r, what=f"issue #{issue_id}")
        media = d.get("media") or {}
        created_by = d.get("createdBy") or {}
        # Seerr posts the original report as comments[0] at creation; everything
        # after it is the reply thread.
        description = ""
        thread: list = []
        for idx, c in enumerate(d.get("comments") or []):
            c = c or {}
            msg = (c.get("message") or "").strip()
            if idx == 0:
                description = msg
                continue
            if not msg:
                continue
            user = c.get("user") or {}
            thread.append(IssueComment(
                author=user.get("displayName") or user.get("plexUsername") or "?",
                message=msg,
                created_at=c.get("createdAt", ""),
            ))
        return IssueListItem(
            id=d["id"],
            issue_type=d.get("issueType", 4),
            status=d.get("status", 0),
            created_at=d.get("createdAt", ""),
            tmdb_id=media.get("tmdbId", 0),
            media_type=media.get("mediaType", ""),
            problem_season=d.get("problemSeason"),
            problem_episode=d.get("problemEpisode"),
            created_by=created_by.get("displayName") or created_by.get("plexUsername") or "?",
            description=description,
            comments=thread,
        )

    async def get_media_title(self, media_type: str, tmdb_id: int) -> tuple[str, str]:
        """Returns (title, year). Year may be empty string."""
        endpoint = "movie" if media_type == "movie" else "tv"
        r = await execute(self._client, "GET", f"/{endpoint}/{tmdb_id}", service=_SERVICE)
        d = _json_or_raise(r, what="media title")
        title = d.get("title") or d.get("name") or "Unknown"
        release = d.get("releaseDate") or d.get("firstAirDate") or ""
        year = release[:4] if release else ""
        return title, year

    async def add_issue_comment(
        self,
        issue_id: int,
        message: str,
        *,
        as_plex_token: Optional[str] = None,
    ) -> None:
        client = await self._client_for(as_plex_token)
        await execute(client, "POST", f"/issue/{issue_id}/comment",
                      service=_SERVICE, json={"message": message})

    async def resolve_issue(
        self,
        issue_id: int,
        *,
        as_plex_token: Optional[str] = None,
    ) -> None:
        client = await self._client_for(as_plex_token)
        await execute(client, "POST", f"/issue/{issue_id}/resolved",
                      service=_SERVICE)

    async def create_issue(
        self,
        *,
        issue_type: int,
        message: str,
        seerr_media_id: int,
        media_type: str,
        problem_season: Optional[int] = None,
        problem_episode: Optional[int] = None,
        as_plex_token: Optional[str] = None,
    ) -> CreatedIssue:
        """Create an issue. issue_type: 1=Video, 2=Audio, 3=Subtitle, 4=Other.

        When `as_plex_token` is given, the request is made on an authenticated
        per-user client, so Seerr attributes the issue to that user (the real
        reporter) -- no message prefixing needed. Without a token it falls back
        to the API-key client, which attributes the issue to the key's owner.

        NOTE: `mediaId` is Seerr's INTERNAL media.id, NOT a TMDb ID. Pass the
        `seerr_media_id` field from a MediaResult. If the media isn't in
        Seerr's library yet (no MediaInfo), the caller must handle that first.
        """
        payload = {
            "issueType": issue_type,
            "message": message,
            "mediaId": seerr_media_id,
            "mediaType": media_type,
        }
        if problem_season is not None:
            payload["problemSeason"] = problem_season
        if problem_episode is not None:
            payload["problemEpisode"] = problem_episode
        client = await self._client_for(as_plex_token)
        r = await execute(client, "POST", "/issue", service=_SERVICE,
                          json=payload)
        # Ambiguous-success guard: the issue is CREATED by now.
        # A garbage 2xx body used to raise a retryable-looking error (user
        # retries -> duplicate issue) or yield CreatedIssue(id=None) (a
        # .../issues/None URL + NOT NULL violation on the poller insert).
        data = _json_or_raise(r, what="issue creation")
        issue_id = data.get("id")
        if not isinstance(issue_id, int):
            raise AmbiguousResponseError(
                "Seerr accepted the report but didn't return an issue id; "
                "check Seerr before retrying to avoid a duplicate")
        url = f"{self.public_url}/issues/{issue_id}"
        return CreatedIssue(id=issue_id, url=url)
