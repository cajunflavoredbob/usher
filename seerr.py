"""Async client for the Seerr REST API."""
from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional, Union

import httpx

from const import CLIENT_CLOSE_GRACE_S, SEARCH_RESULT_LIMIT
from http_util import APIError, PermanentAPIError, execute, json_or_raise

logger = logging.getLogger("usher." + __name__)

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
    """Parse a 2xx body, raising AmbiguousResponseError (not a bare
    ValueError) on a non-JSON or wrong-shape body. Uses the shared
    http_util.json_or_raise with Seerr's write-safe exception so a create's
    side effect that returned garbage isn't blind-retried into a duplicate."""
    return json_or_raise(r, service=_SERVICE, what=what, expect=expect,
                         exc_factory=lambda msg: AmbiguousResponseError(
                             f"Seerr {msg}"))


class PlexTokenInvalidError(PermanentAPIError):
    """Seerr rejected the stored Plex token on /auth/plex (revoked or
    expired). Retrying can't help; the user must re-link. Subclasses
    PermanentAPIError so any surface without a dedicated re-link prompt
    still renders a truthful message instead of "try again in a minute"."""
    def __init__(self):
        super().__init__("your Plex sign-in is no longer valid",
                         status_code=500, service=_SERVICE)


class DuplicateRequestError(PermanentAPIError):
    """Seerr 409: an active request for this media already exists."""
    def __init__(self, detail: str):
        super().__init__(detail or "a request for this already exists",
                         status_code=409, service=_SERVICE)


class QuotaExceededError(PermanentAPIError):
    """Seerr 403 with a quota message: the user's movie/series request
    quota is used up. Distinct from RequestPermissionError so the flow can
    show the user's quota window instead of a permissions lecture."""
    def __init__(self, detail: str):
        super().__init__(detail or "request quota exceeded",
                         status_code=403, service=_SERVICE)


class RequestPermissionError(PermanentAPIError):
    """Seerr 403: the user lacks the REQUEST (or related) permission, or the
    media is blocklisted. Seerr is the authority on who may request; this is
    surfaced as-is rather than gated bot-side."""
    def __init__(self, detail: str):
        super().__init__(detail or "you don't have permission to request this",
                         status_code=403, service=_SERVICE)


class NothingToRequestError(PermanentAPIError):
    """Seerr answered a TV request with 202: every requested season is
    already available or covered by an existing request, so nothing new was
    created. Not a failure of the user's doing -- the flow renders it as
    "already covered", never as an error."""
    def __init__(self):
        super().__init__("every selected season is already available or requested",
                         status_code=202, service=_SERVICE)

# Per-Plex-token authenticated client cache. Reuses warm clients under a
# webhook comment flood instead of paying the TCP-handshake + /auth/plex
# cost on every call. LRU + TTL bounded so a token flood
# doesn't blow up FD count.
_USER_CLIENT_TTL_S = 300.0
_USER_CLIENT_MAX = 32
# Grace before an evicted/expired user client is actually closed; see
# const.CLIENT_CLOSE_GRACE_S (must outlive a full retry chain, shared with
# bot/app.py's hot-reload close so the two can't drift).
_USER_CLIENT_CLOSE_GRACE_S = CLIENT_CLOSE_GRACE_S


# Seerr's MediaStatus enum (availability of media, server/constants/media.ts).
MEDIA_STATUS_UNKNOWN = 1
MEDIA_STATUS_PENDING = 2
MEDIA_STATUS_PROCESSING = 3
MEDIA_STATUS_PARTIALLY_AVAILABLE = 4
MEDIA_STATUS_AVAILABLE = 5
MEDIA_STATUS_BLOCKLISTED = 6
MEDIA_STATUS_DELETED = 7

# Webhook notification-type bits the bot depends on (Seerr's Notification
# enum), with the labels Seerr's UI shows for each checkbox. Everything the
# dispatcher handles except ISSUE_REOPENED (deliberately unhandled) and
# MEDIA_AUTO_REQUESTED (deliberately silent). Used by the /status webhook
# check so a misconfigured agent is diagnosed instead of silently dropping
# DMs.
REQUIRED_WEBHOOK_TYPES: dict = {
    2: "Request Pending Approval",
    4: "Request Approved",
    8: "Request Available",
    16: "Request Processing Failed",
    64: "Request Declined",
    128: "Request Automatically Approved",
    256: "Issue Reported",
    512: "Issue Comment",
    1024: "Issue Resolved",
}

# Seerr's MediaRequestStatus enum (state of a request, not the media).
REQUEST_STATUS_PENDING = 1
REQUEST_STATUS_APPROVED = 2
REQUEST_STATUS_DECLINED = 3
REQUEST_STATUS_FAILED = 4
REQUEST_STATUS_COMPLETED = 5

# Seerr permission bits (server/lib/permissions.ts). Only the ones the bot
# reads; ADMIN short-circuits every check server-side.
PERMISSION_ADMIN = 2
PERMISSION_REQUEST_4K = 1024
PERMISSION_REQUEST_4K_MOVIE = 2048
PERMISSION_REQUEST_4K_TV = 4096


def can_request_4k(permissions: int, media_type: str) -> bool:
    """Whether a Seerr permission bitmask allows 4K requests for the given
    media type. Mirrors Seerr's own check: ADMIN or the blanket REQUEST_4K
    or the type-specific bit."""
    if permissions & PERMISSION_ADMIN or permissions & PERMISSION_REQUEST_4K:
        return True
    if media_type == "movie":
        return bool(permissions & PERMISSION_REQUEST_4K_MOVIE)
    return bool(permissions & PERMISSION_REQUEST_4K_TV)


@dataclass
class MediaResult:
    """One search hit from Seerr."""
    media_type: str       # "movie" or "tv"
    tmdb_id: int          # TMDb ID (used for details lookup + Radarr/Sonarr auto-fix)
    title: str
    year: str             # may be empty string
    seerr_media_id: Optional[int]  # Seerr's internal media.id (used as `mediaId` for issue creation).
                                   # None means this media isn't yet in Seerr's library.
    status: Optional[int] = None   # MediaStatus (MEDIA_STATUS_*) from mediaInfo;
                                   # None when Seerr has no record of this media.
    status_4k: Optional[int] = None  # MediaStatus of the 4K track, when known.
    overview: str = ""             # TMDB synopsis (may be empty)
    poster_path: str = ""          # TMDB poster path ("/abc.jpg"), "" when none
    vote_average: float = 0.0      # TMDB user score, 0 when unrated


@dataclass
class CreatedIssue:
    id: int
    url: str


@dataclass
class CreatedRequest:
    """Outcome of a successful POST /request."""
    id: int
    status: int  # REQUEST_STATUS_*: auto-approved users get APPROVED immediately
    seasons: list = field(default_factory=list)  # season numbers Seerr actually
                                                 # granted (it silently drops
                                                 # already-covered ones)


@dataclass
class SeasonAvailability:
    """One season of a show, with whether a new standard-tier request for it
    would stick. Mirrors Seerr's own drop rules on the standard track: a
    season is NOT requestable when its media entity status is anything but
    UNKNOWN/DELETED, or when it appears in an existing standard (non-4K)
    request that is neither DECLINED nor COMPLETED (FAILED still blocks,
    matching the server)."""
    season_number: int
    requestable: bool


@dataclass
class RequestListItem:
    """One entry from GET /request (or a single GET /request/{id})."""
    id: int
    status: int              # REQUEST_STATUS_*
    media_type: str          # "movie" or "tv"
    tmdb_id: int
    seasons: list = field(default_factory=list)  # season numbers (TV only)
    created_at: str = ""     # ISO 8601
    requested_by: str = "?"  # displayName
    requested_by_id: Optional[int] = None  # Seerr user id of the requester


@dataclass
class QuotaBucket:
    days: int
    limit: int       # 0 = unlimited
    used: int
    remaining: Optional[int]  # None when unlimited
    restricted: bool          # True = the next request would be rejected


@dataclass
class Quota:
    movie: QuotaBucket
    tv: QuotaBucket  # counted in SEASONS, not shows


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
        # Guards _user_clients / _user_client_creation_locks mutation.
        # Never held across a network call (see _as_user).
        self._user_clients_lock = asyncio.Lock()
        # Per-token creation locks: two concurrent misses for one token
        # would otherwise both POST /auth/plex and orphan the loser's
        # client. Entries live only for the duration of a creation.
        self._user_client_creation_locks: dict[str, asyncio.Lock] = {}
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
        # Snapshot the retired set BEFORE cancelling the deferred-close tasks:
        # cancelling lands in each task's sleep, whose finally discards the
        # client from _retired_user_clients, so reading the set afterwards
        # would find it empty and never close those clients.
        retired = list(self._retired_user_clients)
        for task in list(self._deferred_close_tasks):
            task.cancel()
        if self._deferred_close_tasks:
            # Let cancellations settle so no task outlives the event loop.
            await asyncio.gather(*self._deferred_close_tasks, return_exceptions=True)
        for client in retired:
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

        Locking is two-level: _user_clients_lock guards dict mutation only
        (never held across a network call), and a per-token creation lock
        serializes the POST /auth/plex for one token. Previously the global
        lock was held across the auth call, so a single cold miss with Seerr
        degraded (retry chain can run 60s+) stalled every other user's cache
        HITS too.
        """
        async with self._user_clients_lock:
            now = time.monotonic()
            entry = self._user_clients.get(plex_token)
            if entry is not None and now < entry[1]:
                self._user_clients.move_to_end(plex_token)
                return entry[0]
            # Cold or expired: take (or create) this token's creation lock.
            # The dict only holds tokens mid-creation (popped in finally).
            creation_lock = self._user_client_creation_locks.setdefault(
                plex_token, asyncio.Lock())

        async with creation_lock:
            try:
                async with self._user_clients_lock:
                    now = time.monotonic()
                    entry = self._user_clients.get(plex_token)
                    if entry is not None:
                        client, expires = entry
                        if now < expires:
                            # Another waiter on this lock already minted it.
                            self._user_clients.move_to_end(plex_token)
                            return client
                        # Stale -- evict + retire (deferred close: a request
                        # may still be running on it), then mint a new one.
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
                except BaseException:
                    # BaseException, not Exception: also close on cancellation
                    # (shutdown/task teardown mid-auth) so new_client, which is
                    # in no collection yet, isn't leaked.
                    await new_client.aclose()
                    raise
                async with self._user_clients_lock:
                    # Retire any entry another concurrent creator inserted
                    # while we were authenticating, so its authenticated client
                    # is closed rather than silently dropped by this overwrite.
                    prior = self._user_clients.pop(plex_token, None)
                    if prior is not None:
                        self._close_later(prior[0])
                    self._user_clients[plex_token] = (
                        new_client, time.monotonic() + _USER_CLIENT_TTL_S)
                    self._user_clients.move_to_end(plex_token)
                    while len(self._user_clients) > _USER_CLIENT_MAX:
                        _, (evict_client, _) = self._user_clients.popitem(last=False)
                        self._close_later(evict_client)
                return new_client
            finally:
                # Safe to drop even with waiters queued: they hold their own
                # reference to the same Lock object; a later cold miss just
                # mints a fresh one.
                async with self._user_clients_lock:
                    self._user_client_creation_locks.pop(plex_token, None)

    async def _client_for(self, as_plex_token: Optional[str]) -> httpx.AsyncClient:
        """The per-user client for token-attributed calls, else the admin-key
        client. Collapses the if/else previously pasted across five methods
       ."""
        if as_plex_token:
            return await self._as_user(as_plex_token)
        return self._client

    @staticmethod
    def _parse_media_results(items: list, limit: int) -> list[MediaResult]:
        """Shared result parser for /search and /discover/* (same shapes)."""
        out: list[MediaResult] = []
        for item in items or []:
            item = item or {}
            mt = item.get("mediaType")
            if mt not in ("movie", "tv"):
                continue  # skip "person" and anything else
            # A result without a usable TMDb id can't be selected (its
            # callback would carry "None" and die on int()); skip it instead
            # of poisoning one button.
            if not isinstance(item.get("id"), int):
                continue
            title = item.get("title") or item.get("name") or "?"
            release = item.get("releaseDate") or item.get("firstAirDate") or ""
            year = release[:4] if release else ""
            media_info = item.get("mediaInfo") or {}
            try:
                vote = float(item.get("voteAverage") or 0)
            except (TypeError, ValueError):
                vote = 0.0
            out.append(MediaResult(
                media_type=mt,
                tmdb_id=item.get("id"),
                title=title,
                year=year,
                seerr_media_id=media_info.get("id"),
                status=media_info.get("status"),
                status_4k=media_info.get("status4k"),
                overview=item.get("overview") or "",
                poster_path=item.get("posterPath") or "",
                vote_average=vote,
            ))
            if len(out) >= limit:
                break
        return out

    async def search(self, query: str,
                     limit: int = SEARCH_RESULT_LIMIT) -> list[MediaResult]:
        """Search Seerr for movies + TV shows matching the query.

        The query is percent-encoded with NO safe characters: Seerr 400s on
        RFC-3986 reserved characters that httpx legitimately leaves raw in
        query strings (a verbatim "Goodbye, Lara" search died on its
        comma)."""
        encoded = urllib.parse.quote(query, safe="")
        r = await execute(self._client, "GET", f"/search?query={encoded}",
                          service=_SERVICE)
        data = _json_or_raise(r, what="search")
        return self._parse_media_results(data.get("results"), limit)

    # /discover categories the bot exposes -> Seerr endpoint paths. Only
    # TMDB-driven lists: nothing here surfaces other users' requests or
    # activity (deliberate privacy constraint on the browse feature).
    DISCOVER_PATHS: dict = {
        "trending": "/discover/trending",
        "movies": "/discover/movies",
        "tv": "/discover/tv",
        "upmovies": "/discover/movies/upcoming",
        "uptv": "/discover/tv/upcoming",
    }

    async def discover(self, category: str,
                       limit: int = SEARCH_RESULT_LIMIT) -> list[MediaResult]:
        """One page of a TMDB discovery list (trending/popular/upcoming),
        same result shape as search."""
        path = self.DISCOVER_PATHS.get(category)
        if path is None:
            raise ValueError(f"unknown discover category: {category}")
        r = await execute(self._client, "GET", path, service=_SERVICE)
        data = _json_or_raise(r, what="discover")
        return self._parse_media_results(data.get("results"), limit)

    async def get_tv_seasons(self, tmdb_id: int) -> tuple[list[TvSeason], Optional[int]]:
        """Return (seasons, tvdb_id) for a TV show. Includes season 0 because
        anime movies / OVAs / tie-in specials often live there and users need
        to report issues on them. (Season NAMES are not fetched: the picker
        renders S<number> buttons only.)"""
        r = await execute(self._client, "GET", f"/tv/{tmdb_id}", service=_SERVICE)
        data = _json_or_raise(r, what="TV seasons")
        seasons: list[TvSeason] = []
        for s in data.get("seasons") or []:
            n = (s or {}).get("seasonNumber")
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
        can exceed len(items) when the list is truncated at `take` (the cap
        was previously silent and issues past it were invisible)."""
        client = await self._client_for(as_plex_token)
        r = await execute(client, "GET", "/issue", service=_SERVICE,
                          params={"filter": filter, "take": take})
        data = _json_or_raise(r, what="issue list")
        out: list[IssueListItem] = []
        for item in data.get("results", []):
            media = item.get("media") or {}
            created_by = item.get("createdBy") or {}
            # One malformed row must not hide the other 24 tickets.
            if not isinstance(item.get("id"), int):
                logger.warning("Skipping issue-list entry without an id: %r", item)
                continue
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
        if not isinstance(d.get("id"), int):
            raise AmbiguousResponseError(
                f"Seerr returned an id-less body for issue #{issue_id}")
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

    # --- Requests ------------------------------------------------------------

    async def get_my_permissions(
        self, *, as_plex_token: Optional[str] = None
    ) -> int:
        """The session user's Seerr permission bitmask (the admin-key client
        reports the key owner's). Used to decide whether to OFFER 4K in the
        flow; Seerr re-checks on submit either way."""
        client = await self._client_for(as_plex_token)
        r = await execute(client, "GET", "/auth/me", service=_SERVICE)
        data = _json_or_raise(r, what="own user profile")
        perms = data.get("permissions")
        return perms if isinstance(perms, int) else 0

    async def get_webhook_notification_settings(self) -> tuple[bool, int]:
        """Seerr's webhook notification-agent settings (admin key):
        (enabled, types bitmask). Backs the /status check that catches the
        silent-notification failure mode -- an install whose webhook agent
        is off or missing event types never delivers issue/request DMs and
        nothing else ever surfaces that."""
        r = await execute(self._client, "GET", "/settings/notifications/webhook",
                          service=_SERVICE)
        data = _json_or_raise(r, what="webhook notification settings")
        types = data.get("types")
        return bool(data.get("enabled")), types if isinstance(types, int) else 0

    async def get_admin_user_id(self) -> Optional[int]:
        """The Seerr user id the admin API key acts as. Cached after the
        first lookup (it can't change without a key rotation, which restarts
        the client). Used to recognize the admin's own key-attributed
        requests in webhook events; None when the lookup fails."""
        if getattr(self, "_admin_user_id", None) is not None:
            return self._admin_user_id
        try:
            r = await execute(self._client, "GET", "/auth/me", service=_SERVICE)
            data = _json_or_raise(r, what="admin user profile")
        except Exception:
            logger.debug("admin user-id lookup failed", exc_info=True)
            return None
        uid = data.get("id")
        if isinstance(uid, int):
            self._admin_user_id = uid
            return uid
        return None

    async def create_request(
        self,
        *,
        media_type: str,
        tmdb_id: int,
        seasons: Union[list, str, None] = None,
        is4k: bool = False,
        as_plex_token: Optional[str] = None,
    ) -> CreatedRequest:
        """Create a media request. Unlike create_issue, `mediaId` here IS the
        TMDb id (Seerr creates its media record on demand).

        With `as_plex_token`, the request is submitted as that user, so
        Seerr's own permissions, quotas, and auto-approve rules apply to
        them. Without a token the API-key client attributes it to the admin.

        `seasons` (TV only): a list of season numbers, or the literal "all"
        (Seerr expands it to every non-special season). Seerr silently drops
        seasons that are already available or covered by an active request
        and creates the request for the remainder; when NOTHING remains it
        answers 202 -> NothingToRequestError.

        Raises DuplicateRequestError (409), QuotaExceededError /
        RequestPermissionError (403), NothingToRequestError (202), or
        AmbiguousResponseError (2xx with an unusable body -- the request may
        have been created; never blind-retry)."""
        payload: dict = {"mediaType": media_type, "mediaId": tmdb_id}
        if seasons is not None:
            payload["seasons"] = seasons
        if is4k:
            payload["is4k"] = True
        client = await self._client_for(as_plex_token)
        try:
            r = await execute(client, "POST", "/request", service=_SERVICE,
                              json=payload)
        except PermanentAPIError as exc:
            detail = str(exc)
            if exc.status_code == 409:
                raise DuplicateRequestError(detail) from exc
            if exc.status_code == 403:
                # Seerr's 403 bodies: "Movie Quota exceeded." / "Series Quota
                # exceeded." vs permission/blocklist messages.
                if "quota exceeded" in detail.lower():
                    raise QuotaExceededError(detail) from exc
                raise RequestPermissionError(detail) from exc
            raise
        # 202 = NoSeasonsAvailableError: a "success" status whose body is an
        # error envelope, not a MediaRequest. Nothing was created.
        if r.status_code == 202:
            raise NothingToRequestError()
        data = _json_or_raise(r, what="request creation")
        request_id = data.get("id")
        if not isinstance(request_id, int):
            raise AmbiguousResponseError(
                "Seerr accepted the request but didn't return an id; "
                "check Seerr before retrying to avoid a duplicate")
        granted = []
        for s in data.get("seasons") or []:
            n = (s or {}).get("seasonNumber")
            if n is not None:
                granted.append(n)
        return CreatedRequest(id=request_id,
                              status=data.get("status", REQUEST_STATUS_PENDING),
                              seasons=granted)

    @staticmethod
    def _parse_request_item(item: dict) -> Optional[RequestListItem]:
        if not isinstance(item.get("id"), int):
            return None
        media = item.get("media") or {}
        requested_by = item.get("requestedBy") or {}
        seasons = []
        for s in item.get("seasons") or []:
            n = (s or {}).get("seasonNumber")
            if n is not None:
                seasons.append(n)
        requester_id = requested_by.get("id")
        return RequestListItem(
            id=item["id"],
            status=item.get("status", 0),
            media_type=media.get("mediaType", ""),
            tmdb_id=media.get("tmdbId", 0),
            seasons=seasons,
            # `or ""`: a JSON null createdAt must not smuggle None into
            # format_age.
            created_at=item.get("createdAt") or "",
            requested_by=(requested_by.get("displayName")
                          or requested_by.get("plexUsername") or "?"),
            requested_by_id=requester_id if isinstance(requester_id, int) else None,
        )

    async def list_requests(
        self,
        *,
        filter: str = "all",
        take: int = 25,
        as_plex_token: Optional[str] = None,
    ) -> tuple[list[RequestListItem], int]:
        """List requests. With as_plex_token, Seerr returns only that user's
        requests; the admin-key client sees everyone's. Returns (items,
        total) like list_issues -- total can exceed len(items) at `take`."""
        client = await self._client_for(as_plex_token)
        r = await execute(client, "GET", "/request", service=_SERVICE,
                          params={"filter": filter, "take": take,
                                  "sort": "added"})
        data = _json_or_raise(r, what="request list")
        out: list[RequestListItem] = []
        for item in data.get("results", []):
            parsed = self._parse_request_item(item or {})
            if parsed is None:
                logger.warning("Skipping request-list entry without an id: %r", item)
                continue
            out.append(parsed)
        page_info = data.get("pageInfo") or {}
        total = page_info.get("results")
        if not isinstance(total, int) or total < len(out):
            total = len(out)
        return out, total

    async def get_request(
        self,
        request_id: int,
        *,
        as_plex_token: Optional[str] = None,
    ) -> RequestListItem:
        """Fetch one request. 404 -> NotFoundAPIError; someone else's request
        without permission -> PermanentAPIError (403)."""
        client = await self._client_for(as_plex_token)
        r = await execute(client, "GET", f"/request/{request_id}",
                          service=_SERVICE)
        data = _json_or_raise(r, what=f"request #{request_id}")
        parsed = self._parse_request_item(data)
        if parsed is None:
            raise AmbiguousResponseError(
                f"Seerr returned an id-less body for request #{request_id}")
        return parsed

    async def delete_request(
        self,
        request_id: int,
        *,
        as_plex_token: Optional[str] = None,
    ) -> None:
        """Cancel a request. Regular users may only delete their own request
        while it is still PENDING (Seerr answers 401 otherwise, surfaced as
        a PermanentAPIError with Seerr's message)."""
        client = await self._client_for(as_plex_token)
        await execute(client, "DELETE", f"/request/{request_id}",
                      service=_SERVICE)

    async def get_tv_season_availability(self, tmdb_id: int) -> list[SeasonAvailability]:
        """Seasons of a show annotated with STANDARD-tier requestability, for
        the /request season picker. Availability is global, so the admin-key
        client is fine here.

        Mirrors Seerr's server-side drop rules for a standard (non-4K)
        request: a season is blocked when its Season entity status is
        anything but UNKNOWN/DELETED, or when it appears in an existing
        SAME-TIER (non-4K) request that is neither DECLINED nor COMPLETED --
        FAILED still blocks, matching the server. 4K-only requests don't
        block the standard track (the server's rule is per-is4k). Season 0
        (specials) is omitted entirely -- Seerr excludes specials from
        requests unless its enableSpecialEpisodes setting is on, and
        offering a button the server ignores reads as a bug."""
        r = await execute(self._client, "GET", f"/tv/{tmdb_id}", service=_SERVICE)
        data = _json_or_raise(r, what="TV availability")
        media_info = data.get("mediaInfo") or {}
        entity_status: dict[int, int] = {}
        for s in media_info.get("seasons") or []:
            n = (s or {}).get("seasonNumber")
            if n is not None:
                entity_status[n] = (s or {}).get("status", MEDIA_STATUS_UNKNOWN)
        actively_requested: set[int] = set()
        for req in media_info.get("requests") or []:
            req = req or {}
            # The server's drop rule is per-tier: only a standard-tier
            # request blocks a standard-tier request. FAILED is NOT skipped:
            # the server still drops seasons held by a FAILED request.
            if req.get("is4k"):
                continue
            if req.get("status") in (REQUEST_STATUS_DECLINED,
                                     REQUEST_STATUS_COMPLETED):
                continue
            for s in req.get("seasons") or []:
                n = (s or {}).get("seasonNumber")
                if n is not None:
                    actively_requested.add(n)
        out: list[SeasonAvailability] = []
        for s in data.get("seasons") or []:
            n = (s or {}).get("seasonNumber")
            if n is None or n == 0:
                continue
            blocked = (entity_status.get(n, MEDIA_STATUS_UNKNOWN)
                       not in (MEDIA_STATUS_UNKNOWN, MEDIA_STATUS_DELETED)
                       or n in actively_requested)
            out.append(SeasonAvailability(
                season_number=n,
                requestable=not blocked,
            ))
        return out

    async def get_quota(
        self,
        seerr_user_id: int,
        *,
        as_plex_token: Optional[str] = None,
    ) -> Quota:
        """Request quota for a user. A user may always read their own; the
        admin-key client may read anyone's."""
        client = await self._client_for(as_plex_token)
        r = await execute(client, "GET", f"/user/{seerr_user_id}/quota",
                          service=_SERVICE)
        data = _json_or_raise(r, what="request quota")

        def bucket(d: Optional[dict]) -> QuotaBucket:
            d = d or {}
            limit = d.get("limit") or 0
            return QuotaBucket(
                days=d.get("days") or 0,
                limit=limit,
                used=d.get("used") or 0,
                remaining=d.get("remaining") if limit else None,
                restricted=bool(d.get("restricted")),
            )

        return Quota(movie=bucket(data.get("movie")), tv=bucket(data.get("tv")))
