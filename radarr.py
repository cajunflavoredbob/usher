"""Async Radarr v3 API client (just the bits we need for auto-fix)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class RadarrMovie:
    id: int
    title: str
    has_file: bool
    movie_file_id: Optional[int]


class RadarrClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=f"{self.base_url}/api/v3",
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
            timeout=timeout,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get_movie_by_tmdb(self, tmdb_id: int) -> Optional[RadarrMovie]:
        """Find a movie in Radarr's library by TMDb ID. None if not present."""
        r = await self._client.get("/movie", params={"tmdbId": tmdb_id})
        r.raise_for_status()
        items = r.json()
        if not items:
            return None
        m = items[0]
        mf = m.get("movieFile") or {}
        return RadarrMovie(
            id=m["id"],
            title=m.get("title", "?"),
            has_file=bool(m.get("hasFile")),
            movie_file_id=mf.get("id"),
        )

    async def delete_movie_file(self, movie_file_id: int) -> None:
        r = await self._client.delete(f"/moviefile/{movie_file_id}")
        r.raise_for_status()

    async def trigger_search(self, movie_id: int) -> None:
        r = await self._client.post(
            "/command",
            json={"name": "MoviesSearch", "movieIds": [movie_id]},
        )
        r.raise_for_status()

    async def auto_fix(self, tmdb_id: int) -> tuple[bool, str, Optional[int]]:
        """Delete current file (if any) and trigger search.

        Returns (ok, message, radarr_movie_id). The ID is included on success
        so the caller can poll for completion.
        """
        movie = await self.get_movie_by_tmdb(tmdb_id)
        if movie is None:
            return False, "Movie isn't in Radarr (not monitored).", None
        if movie.has_file and movie.movie_file_id:
            try:
                await self.delete_movie_file(movie.movie_file_id)
            except Exception as exc:
                return False, f"Couldn't delete file: {exc}", None
        try:
            await self.trigger_search(movie.id)
        except Exception as exc:
            return False, f"Couldn't trigger search: {exc}", None
        return True, f"Deleted file (if any) and triggered re-search for '{movie.title}'.", movie.id

    async def movie_has_file(self, movie_id: int) -> bool:
        r = await self._client.get(f"/movie/{movie_id}")
        r.raise_for_status()
        return bool(r.json().get("hasFile"))
