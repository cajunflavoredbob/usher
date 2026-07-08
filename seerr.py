"""Async client for the Seerr REST API."""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

import httpx

from http_util import execute

logger = logging.getLogger(__name__)

_SERVICE = "Seerr"

# Per-Plex-token authenticated client cache. Reuses warm clients under a
# webhook comment flood instead of paying the TCP-handshake + /auth/plex
# cost on every call (audit CONC #11). LRU + TTL bounded so a token flood
# doesn't blow up FD count.
_USER_CLIENT_TTL_S = 300.0
_USER_CLIENT_MAX = 32


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
    name: str


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

    async def close(self) -> None:
        await self._client.aclose()
        for client, _ in list(self._user_clients.values()):
            try:
                await client.aclose()
            except Exception:
                logger.warning("aclose on cached user client failed", exc_info=True)
        self._user_clients.clear()

    async def ping(self) -> str:
        """Return Seerr's version string. Raises APIError on failure."""
        r = await execute(self._client, "GET", "/status", service=_SERVICE)
        return r.json().get("version", "?")

    async def login_with_plex(self, plex_token: str) -> tuple[int, str, httpx.Cookies]:
        """Authenticate to Seerr as a Plex user. Returns (seerr_user_id, display_name, cookies)."""
        r = await execute(self._client, "POST", "/auth/plex", service=_SERVICE,
                          json={"authToken": plex_token})
        data = r.json()
        return (
            int(data["id"]),
            data.get("displayName") or data.get("plexUsername") or data.get("username") or "?",
            r.cookies,
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
        """
        now = time.monotonic()
        entry = self._user_clients.get(plex_token)
        if entry is not None:
            client, expires = entry
            if now < expires:
                self._user_clients.move_to_end(plex_token)
                return client
            # Stale -- evict + close, then fall through to mint a new one.
            self._user_clients.pop(plex_token, None)
            try:
                await client.aclose()
            except Exception:
                logger.warning("aclose on expired user client failed", exc_info=True)

        new_client = httpx.AsyncClient(
            base_url=f"{self.base_url}/api/v1",
            headers={"Accept": "application/json"},
            timeout=15.0,
        )
        try:
            await execute(new_client, "POST", "/auth/plex", service=_SERVICE,
                          json={"authToken": plex_token})
        except Exception:
            await new_client.aclose()
            raise
        self._user_clients[plex_token] = (new_client, now + _USER_CLIENT_TTL_S)
        self._user_clients.move_to_end(plex_token)
        while len(self._user_clients) > _USER_CLIENT_MAX:
            _, (evict_client, _) = self._user_clients.popitem(last=False)
            try:
                await evict_client.aclose()
            except Exception:
                logger.warning("aclose on LRU-evicted user client failed", exc_info=True)
        return new_client

    async def search(self, query: str, limit: int = 5) -> list[MediaResult]:
        """Search Seerr for movies + TV shows matching the query."""
        r = await execute(self._client, "GET", "/search", service=_SERVICE,
                          params={"query": query})
        data = r.json()
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
        """Return (seasons, tvdb_id) for a TV show. Includes season 0
        (rendered as 'Specials') because anime movies / OVAs / tie-in
        specials often live there and users need to report issues on them."""
        r = await execute(self._client, "GET", f"/tv/{tmdb_id}", service=_SERVICE)
        data = r.json()
        seasons: list[TvSeason] = []
        for s in data.get("seasons", []):
            n = s.get("seasonNumber")
            if n is None:
                continue
            default_name = "Specials" if n == 0 else f"Season {n}"
            seasons.append(TvSeason(
                season_number=n,
                episode_count=s.get("episodeCount", 0),
                name=s.get("name") or default_name,
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
    ) -> list[IssueListItem]:
        """List issues. If as_plex_token is provided, authenticates as that
        user (gets their visible issues only). Else returns all (admin view)."""
        if as_plex_token:
            client = await self._as_user(as_plex_token)
            r = await execute(client, "GET", "/issue", service=_SERVICE,
                              params={"filter": filter, "take": take})
            data = r.json()
        else:
            r = await execute(self._client, "GET", "/issue", service=_SERVICE,
                              params={"filter": filter, "take": take})
            data = r.json()
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
        return out

    async def get_issue(
        self,
        issue_id: int,
        *,
        as_plex_token: Optional[str] = None,
    ) -> IssueListItem:
        """Fetch a single issue by id. Same shape as list_issues entries."""
        if as_plex_token:
            client = await self._as_user(as_plex_token)
            r = await execute(client, "GET", f"/issue/{issue_id}", service=_SERVICE)
            d = r.json()
        else:
            r = await execute(self._client, "GET", f"/issue/{issue_id}", service=_SERVICE)
            d = r.json()
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
        d = r.json()
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
        if as_plex_token:
            client = await self._as_user(as_plex_token)
            await execute(client, "POST", f"/issue/{issue_id}/comment",
                          service=_SERVICE, json={"message": message})
        else:
            await execute(self._client, "POST", f"/issue/{issue_id}/comment",
                          service=_SERVICE, json={"message": message})

    async def resolve_issue(
        self,
        issue_id: int,
        *,
        as_plex_token: Optional[str] = None,
    ) -> None:
        if as_plex_token:
            client = await self._as_user(as_plex_token)
            await execute(client, "POST", f"/issue/{issue_id}/resolved",
                          service=_SERVICE)
        else:
            await execute(self._client, "POST", f"/issue/{issue_id}/resolved",
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
        if as_plex_token:
            client = await self._as_user(as_plex_token)
            r = await execute(client, "POST", "/issue", service=_SERVICE,
                              json=payload)
            data = r.json()
        else:
            r = await execute(self._client, "POST", "/issue", service=_SERVICE,
                              json=payload)
            data = r.json()
        issue_id = data.get("id")
        url = f"{self.public_url}/issues/{issue_id}"
        return CreatedIssue(id=issue_id, url=url)
