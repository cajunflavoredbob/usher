"""Async Sonarr v3 API client (just the bits we need for auto-fix)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


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

    async def get_series_by_tvdb(self, tvdb_id: int) -> Optional[SonarrSeries]:
        r = await self._client.get("/series", params={"tvdbId": tvdb_id})
        r.raise_for_status()
        items = r.json()
        if not items:
            return None
        s = items[0]
        return SonarrSeries(id=s["id"], title=s.get("title", "?"))

    async def get_episodes(self, series_id: int, season: int) -> list[SonarrEpisode]:
        r = await self._client.get(
            "/episode",
            params={"seriesId": series_id, "seasonNumber": season},
        )
        r.raise_for_status()
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
        r = await self._client.delete(f"/episodefile/{episode_file_id}")
        r.raise_for_status()

    async def trigger_episode_search(self, episode_ids: list[int]) -> None:
        r = await self._client.post(
            "/command",
            json={"name": "EpisodeSearch", "episodeIds": episode_ids},
        )
        r.raise_for_status()

    async def trigger_season_search(self, series_id: int, season: int) -> None:
        r = await self._client.post(
            "/command",
            json={"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": season},
        )
        r.raise_for_status()

    async def auto_fix_episode(
        self, tvdb_id: int, season: int, episode: int
    ) -> tuple[bool, str]:
        series = await self.get_series_by_tvdb(tvdb_id)
        if series is None:
            return False, "Series isn't in Sonarr."
        episodes = await self.get_episodes(series.id, season)
        match = next((e for e in episodes if e.episode == episode), None)
        if match is None:
            return False, f"S{season:02d}E{episode:02d} not found in Sonarr."
        if match.has_file and match.episode_file_id:
            try:
                await self.delete_episode_file(match.episode_file_id)
            except Exception as exc:
                return False, f"Couldn't delete file: {exc}"
        try:
            await self.trigger_episode_search([match.id])
        except Exception as exc:
            return False, f"Couldn't trigger search: {exc}"
        return True, (
            f"Deleted '{series.title}' S{season:02d}E{episode:02d} file "
            f"(if any) and triggered re-search."
        )

    async def auto_fix_season(
        self, tvdb_id: int, season: int
    ) -> tuple[bool, str]:
        series = await self.get_series_by_tvdb(tvdb_id)
        if series is None:
            return False, "Series isn't in Sonarr."
        episodes = await self.get_episodes(series.id, season)
        to_delete = [e.episode_file_id for e in episodes if e.has_file and e.episode_file_id]
        for fid in to_delete:
            try:
                await self.delete_episode_file(fid)
            except Exception as exc:
                logger.warning("failed to delete episode file %s: %s", fid, exc)
        try:
            await self.trigger_season_search(series.id, season)
        except Exception as exc:
            return False, f"Couldn't trigger search: {exc}"
        return True, (
            f"Deleted {len(to_delete)} file(s) in '{series.title}' Season {season} "
            f"and triggered a season-wide search."
        )
