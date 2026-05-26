"""Async client for the Seerr REST API."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


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
class SeerrUser:
    id: int
    username: Optional[str]
    plex_username: Optional[str]
    display_name: str


@dataclass
class CreatedIssue:
    id: int
    url: str


@dataclass
class TvSeason:
    season_number: int
    episode_count: int
    name: str


class SeerrClient:
    """Thin wrapper around the Seerr v1 API."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=f"{self.base_url}/api/v1",
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
            timeout=timeout,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def search(self, query: str, limit: int = 5) -> list[MediaResult]:
        """Search Seerr for movies + TV shows matching the query."""
        r = await self._client.get("/search", params={"query": query})
        r.raise_for_status()
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

    async def find_user(self, query: str) -> Optional[SeerrUser]:
        """Find a Seerr user by username, plexUsername, or displayName (case-insensitive).

        Iterates pages until found or exhausted (Seerr default page size = 10).
        """
        q = query.strip().lower()
        skip = 0
        page_size = 50
        while True:
            r = await self._client.get("/user", params={"take": page_size, "skip": skip})
            r.raise_for_status()
            data = r.json()
            for u in data.get("results", []):
                candidates = [
                    (u.get("username") or "").lower(),
                    (u.get("plexUsername") or "").lower(),
                    (u.get("jellyfinUsername") or "").lower(),
                    (u.get("displayName") or "").lower(),
                ]
                if q in candidates:
                    return SeerrUser(
                        id=u["id"],
                        username=u.get("username"),
                        plex_username=u.get("plexUsername"),
                        display_name=u.get("displayName") or u.get("plexUsername") or u.get("username") or "?",
                    )
            page = data.get("pageInfo", {})
            if page.get("page", 1) >= page.get("pages", 1):
                return None
            skip += page_size

    async def get_tv_seasons(self, tmdb_id: int) -> tuple[list[TvSeason], Optional[int]]:
        """Return (seasons, tvdb_id) for a TV show. Excludes season 0 (specials)."""
        r = await self._client.get(f"/tv/{tmdb_id}")
        r.raise_for_status()
        data = r.json()
        seasons: list[TvSeason] = []
        for s in data.get("seasons", []):
            n = s.get("seasonNumber")
            if n is None or n == 0:
                continue  # skip Specials by default
            seasons.append(TvSeason(
                season_number=n,
                episode_count=s.get("episodeCount", 0),
                name=s.get("name") or f"Season {n}",
            ))
        external = data.get("externalIds") or {}
        tvdb_id = external.get("tvdbId")
        return seasons, tvdb_id

    async def add_issue_comment(self, issue_id: int, message: str) -> None:
        r = await self._client.post(f"/issue/{issue_id}/comment", json={"message": message})
        r.raise_for_status()

    async def resolve_issue(self, issue_id: int) -> None:
        r = await self._client.post(f"/issue/{issue_id}/resolved")
        r.raise_for_status()

    async def create_issue(
        self,
        *,
        issue_type: int,
        message: str,
        seerr_media_id: int,
        media_type: str,
        problem_season: Optional[int] = None,
        problem_episode: Optional[int] = None,
    ) -> CreatedIssue:
        """Create an issue. issue_type: 1=Video, 2=Audio, 3=Subtitle, 4=Other.

        Seerr attributes the issue to the API key's owner; we can't override.
        Caller should prefix `message` with reporter identity for visibility.

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
        r = await self._client.post("/issue", json=payload)
        r.raise_for_status()
        data = r.json()
        issue_id = data.get("id")
        url = f"{self.base_url}/issues/{issue_id}"
        return CreatedIssue(id=issue_id, url=url)
