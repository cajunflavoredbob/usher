"""Async Radarr v3 API client (just the bits we need for auto-fix)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from fix_result import FixResult
from http_util import APIError, execute

logger = logging.getLogger(__name__)

_SERVICE = "Radarr"


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

    async def ping(self) -> str:
        """Return Radarr's version string. Raises APIError on failure."""
        r = await execute(self._client, "GET", "/system/status", service=_SERVICE)
        return r.json().get("version", "?")

    async def get_movie_by_tmdb(self, tmdb_id: int) -> Optional[RadarrMovie]:
        """Find a movie in Radarr's library by TMDb ID. None if not present."""
        r = await execute(self._client, "GET", "/movie", service=_SERVICE,
                          params={"tmdbId": tmdb_id})
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
        await execute(self._client, "DELETE", f"/moviefile/{movie_file_id}",
                      service=_SERVICE)

    async def trigger_search(self, movie_id: int) -> None:
        await execute(self._client, "POST", "/command", service=_SERVICE,
                      json={"name": "MoviesSearch", "movieIds": [movie_id]})

    async def movie_has_file(self, movie_id: int) -> bool:
        r = await execute(self._client, "GET", f"/movie/{movie_id}",
                          service=_SERVICE)
        return bool(r.json().get("hasFile"))

    async def _run_movie_workflow(
        self, *, movie: RadarrMovie, blocklist: bool,
    ) -> FixResult:
        """Optional blocklist + delete + search workflow for a single movie.
        auto_fix calls with blocklist=False; mark_failed with blocklist=True.
        Status semantics match v0.11.3: ok if all steps succeed; partial if
        an early step succeeded but a later one failed (search is what makes
        the poller worth running); failed if nothing happened.
        """
        steps: list[str] = []
        poll_info = {"movie_id": movie.id}

        blocklisted = False
        if blocklist:
            try:
                r = await execute(
                    self._client, "GET", "/history", service=_SERVICE,
                    params={"movieId": movie.id, "page": 1, "pageSize": 20,
                            "sortKey": "date", "sortDirection": "descending"},
                )
                records = r.json().get("records") or []
                grab = next((rec for rec in records if rec.get("eventType") == "grabbed"), None)
                if grab is not None:
                    await execute(self._client, "POST", f"/history/failed/{grab['id']}",
                                  service=_SERVICE)
                    steps.append("blocklist")
                    blocklisted = True
            except APIError as exc:
                return FixResult.failed(f"Couldn't blocklist release: {exc.user_message}")

        if movie.has_file and movie.movie_file_id:
            try:
                await self.delete_movie_file(movie.movie_file_id)
                steps.append("delete")
            except APIError as exc:
                prefix = "Blocklisted release but " if blocklisted else ""
                return FixResult.partial(
                    f"{prefix}couldn't delete file: {exc.user_message}",
                    steps_done=steps, poll_info=poll_info,
                )

        try:
            await self.trigger_search(movie.id)
            steps.append("search")
        except APIError as exc:
            return FixResult.partial(
                f"Cleaned up but couldn't trigger search: {exc.user_message}",
                steps_done=steps, poll_info=poll_info,
            )

        if blocklist:
            prefix = "Blocklisted current release, " if blocklisted else "No prior grab to blocklist; "
            message = f"{prefix}deleted '{movie.title}' file, and triggered re-search."
        else:
            message = f"Deleted file (if any) and triggered re-search for '{movie.title}'."
        return FixResult.success(message, steps_done=steps, poll_info=poll_info)

    async def auto_fix(self, tmdb_id: int) -> FixResult:
        """Delete current file (if any) and trigger search."""
        try:
            movie = await self.get_movie_by_tmdb(tmdb_id)
        except APIError as exc:
            return FixResult.failed(f"Radarr lookup failed: {exc.user_message}")
        if movie is None:
            return FixResult.failed("Movie isn't in Radarr (not monitored).")
        return await self._run_movie_workflow(movie=movie, blocklist=False)

    async def mark_failed(self, tmdb_id: int) -> FixResult:
        """Blocklist the most recent grab, delete the on-disk file, and trigger
        a new search. Falls back to delete+search if there's no grab in history.
        """
        try:
            movie = await self.get_movie_by_tmdb(tmdb_id)
        except APIError as exc:
            return FixResult.failed(f"Radarr lookup failed: {exc.user_message}")
        if movie is None:
            return FixResult.failed("Movie isn't in Radarr (not monitored).")
        return await self._run_movie_workflow(movie=movie, blocklist=True)
