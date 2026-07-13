"""Async Sonarr v3 API client (just the bits we need for auto-fix)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from fix_result import FixResult
from http_util import APIError, execute

logger = logging.getLogger("hermes." + __name__)

_SERVICE = "Sonarr"

# Mark-Failed history scan: page until the grabbed event is found, capped so
# a pathological history can't spin forever (5 x 50 = 250 records deep).
_HISTORY_PAGE_SIZE = 50
_HISTORY_MAX_PAGES = 5


@dataclass
class SonarrSeries:
    id: int
    title: str


@dataclass
class SonarrEpisode:
    id: int
    season: int
    episode: int
    title: str
    has_file: bool
    episode_file_id: Optional[int]


class SonarrClient:
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
        """Return Sonarr's version string. Raises APIError on failure."""
        r = await execute(self._client, "GET", "/system/status", service=_SERVICE)
        return r.json().get("version", "?")

    async def get_series_by_tvdb(self, tvdb_id: int) -> Optional[SonarrSeries]:
        r = await execute(self._client, "GET", "/series", service=_SERVICE,
                          params={"tvdbId": tvdb_id})
        items = r.json()
        # Identity guard: same reasoning as radarr.get_movie_by_tmdb -- the
        # result feeds a delete workflow and Sonarr ignores unknown query
        # params, so scan for the requested ID instead of trusting items[0].
        s = next((it for it in items if it.get("tvdbId") == tvdb_id), None)
        if s is None:
            return None
        return SonarrSeries(id=s["id"], title=s.get("title", "?"))

    async def get_episodes(self, series_id: int, season: int) -> list[SonarrEpisode]:
        r = await execute(self._client, "GET", "/episode", service=_SERVICE,
                          params={"seriesId": series_id, "seasonNumber": season})
        out: list[SonarrEpisode] = []
        for e in r.json():
            out.append(SonarrEpisode(
                id=e["id"],
                season=e.get("seasonNumber"),
                episode=e.get("episodeNumber"),
                title=e.get("title", "?"),
                has_file=bool(e.get("hasFile")),
                episode_file_id=e.get("episodeFileId") if e.get("hasFile") else None,
            ))
        return out

    async def delete_episode_file(self, episode_file_id: int) -> None:
        await execute(self._client, "DELETE", f"/episodefile/{episode_file_id}",
                      service=_SERVICE)

    async def trigger_episode_search(self, episode_ids: list[int]) -> None:
        await execute(self._client, "POST", "/command", service=_SERVICE,
                      json={"name": "EpisodeSearch", "episodeIds": episode_ids})


    async def _run_episode_workflow(
        self, *, series: SonarrSeries, match: SonarrEpisode, blocklist: bool,
    ) -> FixResult:
        """Optional blocklist + delete + search workflow for a single episode.
        auto_fix_episode calls with blocklist=False; mark_failed_episode with
        blocklist=True. Status semantics match v0.11.3: ok if all steps
        succeed; partial if an early step succeeded but search failed;
        failed if nothing happened.
        """
        steps: list[str] = []
        poll_info = {"series_id": series.id, "episode_id": match.id}

        blocklisted = False
        if blocklist:
            try:
                # Page until the grabbed event is found: a
                # churn-heavy episode (repeated imports/upgrades) can push
                # the grab past the newest 20 records, and silently skipping
                # the blocklist re-grabs the exact release Mark Failed was
                # meant to bury.
                grab = None
                for page in range(1, _HISTORY_MAX_PAGES + 1):
                    r = await execute(
                        self._client, "GET", "/history", service=_SERVICE,
                        params={"episodeId": match.id, "page": page,
                                "pageSize": _HISTORY_PAGE_SIZE,
                                "sortKey": "date", "sortDirection": "descending"},
                    )
                    records = r.json().get("records") or []
                    grab = next((rec for rec in records if rec.get("eventType") == "grabbed"), None)
                    if grab is not None or len(records) < _HISTORY_PAGE_SIZE:
                        break
                if grab is not None:
                    await execute(self._client, "POST", f"/history/failed/{grab['id']}",
                                  service=_SERVICE)
                    steps.append("blocklist")
                    blocklisted = True
            except APIError as exc:
                return FixResult.failed(f"Couldn't blocklist release: {exc.user_message}")

        if match.has_file and match.episode_file_id:
            try:
                await self.delete_episode_file(match.episode_file_id)
                steps.append("delete")
            except APIError as exc:
                prefix = "Blocklisted release but " if blocklisted else ""
                return FixResult.partial(
                    f"{prefix}couldn't delete file: {exc.user_message}",
                    steps_done=steps, poll_info=poll_info,
                )

        try:
            await self.trigger_episode_search([match.id])
            steps.append("search")
        except APIError as exc:
            return FixResult.partial(
                f"Cleaned up but couldn't trigger search: {exc.user_message}",
                steps_done=steps, poll_info=poll_info,
            )

        if blocklist:
            prefix = "Blocklisted current release, " if blocklisted else "No prior grab to blocklist; "
            message = (
                f"{prefix}deleted '{series.title}' S{match.season:02d}E{match.episode:02d} "
                "file, and triggered re-search."
            )
        else:
            message = (
                f"Deleted '{series.title}' S{match.season:02d}E{match.episode:02d} file "
                "(if any) and triggered re-search."
            )
        return FixResult.success(message, steps_done=steps, poll_info=poll_info)

    async def _resolve_episode(
        self, tvdb_id: int, season: int, episode: int
    ) -> tuple[Optional[SonarrSeries], Optional[SonarrEpisode], Optional[FixResult]]:
        """Common prelude for episode-level workflows: locate series + match.
        Returns (series, match, error_result). Caller bails when error_result
        is not None."""
        try:
            series = await self.get_series_by_tvdb(tvdb_id)
        except APIError as exc:
            return None, None, FixResult.failed(f"Sonarr lookup failed: {exc.user_message}")
        if series is None:
            return None, None, FixResult.failed("Series isn't in Sonarr.")
        try:
            episodes = await self.get_episodes(series.id, season)
        except APIError as exc:
            return series, None, FixResult.failed(
                f"Sonarr episode lookup failed: {exc.user_message}"
            )
        match = next((e for e in episodes if e.episode == episode), None)
        if match is None:
            return series, None, FixResult.failed(
                f"S{season:02d}E{episode:02d} not found in Sonarr."
            )
        return series, match, None

    async def auto_fix_episode(
        self, tvdb_id: int, season: int, episode: int
    ) -> FixResult:
        """Delete current file (if any) and trigger a new search."""
        series, match, err = await self._resolve_episode(tvdb_id, season, episode)
        if err is not None:
            return err
        return await self._run_episode_workflow(series=series, match=match, blocklist=False)

    # NOTE: the whole-season workflow (auto_fix_season / SeasonSearch /
    # season_files_present) was removed in 0.12.0 as dead code -- no caller
    # ever produced a season-shaped poll_info. The DB columns
    # (sonarr_season, expected_episode_ids) are kept for when the feature
    # is actually wired up.

    async def mark_failed_episode(
        self, tvdb_id: int, season: int, episode: int
    ) -> FixResult:
        """Blocklist the most recent grab, delete the on-disk file, and trigger
        a new search. Falls back to delete+search if there's no grab in history.
        """
        series, match, err = await self._resolve_episode(tvdb_id, season, episode)
        if err is not None:
            return err
        return await self._run_episode_workflow(series=series, match=match, blocklist=True)

    async def episode_has_file(self, episode_id: int) -> bool:
        r = await execute(self._client, "GET", f"/episode/{episode_id}",
                          service=_SERVICE)
        return bool(r.json().get("hasFile"))

