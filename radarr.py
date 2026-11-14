"""Async Radarr v3 API client (just the bits we need for auto-fix)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from fix_result import (FixResult, ReleaseFingerprint, build_fingerprint,
                        find_grab_for_import, parse_queue_records)
from http_util import APIError, execute, json_or_raise

logger = logging.getLogger("usher." + __name__)

_SERVICE = "Radarr"


@dataclass
class RadarrMovie:
    id: int
    title: str
    has_file: bool
    movie_file_id: Optional[int]
    file: Optional[dict] = None   # the movie's file record, as Radarr sent it


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
        return json_or_raise(r, service=_SERVICE, what="system status").get("version", "?")

    async def get_movie_by_tmdb(self, tmdb_id: int) -> Optional[RadarrMovie]:
        """Find a movie in Radarr's library by TMDb ID. None if not present."""
        r = await execute(self._client, "GET", "/movie", service=_SERVICE,
                          params={"tmdbId": tmdb_id})
        items = json_or_raise(r, service=_SERVICE, what="movie lookup", expect=list)
        # Identity guard: this result feeds a blocklist+delete workflow, and
        # Radarr silently ignores query params it doesn't recognize (the
        # /history movieId bug all over again). Never trust items[0] -- scan
        # for the requested ID so an unfiltered response can't select an
        # arbitrary movie for deletion.
        m = next((it for it in items if it.get("tmdbId") == tmdb_id), None)
        if m is None:
            return None
        mf = m.get("movieFile") or {}
        return RadarrMovie(
            id=m["id"],
            title=m.get("title", "?"),
            has_file=bool(m.get("hasFile")),
            movie_file_id=mf.get("id"),
            file=mf or None,
        )

    async def delete_movie_file(self, movie_file_id: int) -> None:
        await execute(self._client, "DELETE", f"/moviefile/{movie_file_id}",
                      service=_SERVICE)

    async def trigger_search(self, movie_id: int) -> None:
        await execute(self._client, "POST", "/command", service=_SERVICE,
                      json={"name": "MoviesSearch", "movieIds": [movie_id]})

    async def refresh_monitored_downloads(self) -> None:
        """Ask the arr to sync with its download clients NOW. The arrs only
        poll their clients on their own schedule, so queue records (and the
        final import step) can lag a minute-plus behind reality; firing
        this before reading the queue keeps the Progress cards honest."""
        await execute(self._client, "POST", "/command", service=_SERVICE,
                      json={"name": "RefreshMonitoredDownloads"})

    async def get_queue_progress(self, movie_id: int):
        """Download progress for a movie's queue records, or None when the
        queue holds nothing for it."""
        r = await execute(self._client, "GET", "/queue/details",
                          service=_SERVICE, params={"movieId": movie_id})
        records = json_or_raise(r, service=_SERVICE, what="queue details",
                                expect=list)
        return parse_queue_records(records)

    async def movie_has_file(self, movie_id: int) -> bool:
        r = await execute(self._client, "GET", f"/movie/{movie_id}",
                          service=_SERVICE)
        return bool(json_or_raise(r, service=_SERVICE, what="movie detail").get("hasFile"))

    async def _file_is_gone(self, exc: APIError, has_file) -> bool:
        """After a failed delete: True when a re-fetch shows the file is
        already gone (usually the delete landed and only its reply was lost)."""
        try:
            return not await has_file()
        except APIError as recheck_exc:
            logger.warning("%s: delete failed (%s) and the re-check failed too (%s)",
                           _SERVICE, exc.user_message, recheck_exc.user_message)
            return False

    async def _movie_history(self, movie_id: int) -> list:
        # /history/movie is the per-movie endpoint (bare array, newest
        # first). The paginated /history endpoint has no singular
        # `movieId` filter -- Radarr silently ignores unknown params,
        # which made this query return library-wide history and could
        # blocklist a different movie's grab.
        r = await execute(
            self._client, "GET", "/history/movie", service=_SERVICE,
            params={"movieId": movie_id},
        )
        return json_or_raise(r, service=_SERVICE, what="movie history", expect=list) or []

    async def read_fingerprint(self, movie_id: int) -> ReleaseFingerprint:
        """Fingerprint of the movie's file now on disk (the poller reads it
        once a replacement lands). Empty when there is no file; raises
        APIError when Radarr can't be read."""
        r = await execute(self._client, "GET", f"/movie/{movie_id}",
                          service=_SERVICE)
        movie = json_or_raise(r, service=_SERVICE, what="movie detail")
        mf = movie.get("movieFile")
        if not isinstance(mf, dict) or not mf:
            return ReleaseFingerprint()
        return build_fingerprint(mf, await self._movie_history(movie_id))

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

        # One history read serves both the before-fingerprint and the
        # blocklist. A failed read only fails a Mark Failed (below); an
        # Auto-fix or Redownload carries on with an empty fingerprint.
        records: Optional[list] = None
        history_exc: Optional[APIError] = None
        if blocklist or movie.has_file:
            try:
                records = await self._movie_history(movie.id)
            except APIError as exc:
                history_exc = exc
                logger.warning("%s: couldn't read history before the fix: %s",
                               _SERVICE, exc.user_message)
        before = ReleaseFingerprint()
        if movie.has_file and records is not None:
            try:
                before = build_fingerprint(movie.file, records)
            except Exception:
                logger.warning("%s: couldn't build the release fingerprint",
                               _SERVICE, exc_info=True)

        blocklisted = False
        if blocklist:
            try:
                if history_exc is not None:
                    raise history_exc
                grab = find_grab_for_import(records or [])
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
                # DELETE is retried after a timeout; when the first attempt
                # landed, the retry gets a 404. Re-check before giving up.
                if await self._file_is_gone(exc, lambda: self.movie_has_file(movie.id)):
                    steps.append("delete")
                else:
                    prefix = "Blocklisted release but " if blocklisted else ""
                    return FixResult.partial(
                        f"{prefix}couldn't delete file: {exc.user_message}",
                        steps_done=steps, poll_info=poll_info, before=before,
                    )

        try:
            await self.trigger_search(movie.id)
            steps.append("search")
        except APIError as exc:
            return FixResult.partial(
                f"Cleaned up but couldn't trigger search: {exc.user_message}",
                steps_done=steps, poll_info=poll_info, before=before,
            )

        if blocklist:
            prefix = ("Blocklisted current release, " if blocklisted
                      else "Couldn't identify the release, so it wasn't blocklisted; ")
            message = f"{prefix}deleted '{movie.title}' file, and triggered re-search."
        else:
            message = f"Deleted file (if any) and triggered re-search for '{movie.title}'."
        return FixResult.success(message, steps_done=steps, poll_info=poll_info,
                                 before=before)

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
        """Blocklist the grab that produced the on-disk file (the "grabbed"
        record sharing the newest import's downloadId), delete the file, and
        trigger a new search. Skips the blocklist if that grab can't be found.
        """
        try:
            movie = await self.get_movie_by_tmdb(tmdb_id)
        except APIError as exc:
            return FixResult.failed(f"Radarr lookup failed: {exc.user_message}")
        if movie is None:
            return FixResult.failed("Movie isn't in Radarr (not monitored).")
        return await self._run_movie_workflow(movie=movie, blocklist=True)
