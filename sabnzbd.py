"""Async client for the SABnzbd JSON API.

Used for exactly one product feature: optionally boosting the queue
priority of downloads that originate from this bot (requests and
auto-fixes), per the admin's SABnzbd settings. The arrs hand us the
download client's item id (SABnzbd's nzo_id) via their queue records.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from http_util import execute, json_or_raise

logger = logging.getLogger("usher." + __name__)

_SERVICE = "SABnzbd"

# SABnzbd priority values for the boost option. "Force" starts the job
# immediately even when the queue is paused or limited; "High" jumps the
# queue but respects global state.
PRIORITY_VALUES = {"high": 1, "force": 2}


class SabnzbdClient:
    """Thin wrapper over SABnzbd's single-endpoint API."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, mode: str, retries: int = 3, **params) -> dict:
        query = {"mode": mode, "output": "json", "apikey": self._api_key}
        query.update(params)
        r = await execute(self._client, "GET", "/api", service=_SERVICE,
                          retries=retries, params=query)
        data = json_or_raise(r, service=_SERVICE, what=f"SABnzbd {mode}")
        # SABnzbd reports API-level failures as 200 + {"status": false,
        # "error": "..."} -- surface them instead of pretending success.
        if data.get("status") is False:
            from http_util import PermanentAPIError
            raise PermanentAPIError(
                data.get("error") or f"SABnzbd rejected {mode}",
                service=_SERVICE)
        return data

    async def ping(self) -> str:
        data = await self._call("version")
        return str(data.get("version", "?"))

    async def set_priority(self, nzo_id: str, level: str) -> bool:
        """Boost one queue item. `level` is "high" or "force" (the admin
        setting); unknown levels no-op. Returns True when SABnzbd accepted
        the change (False typically means the item already left the
        queue -- benign)."""
        value = PRIORITY_VALUES.get(level)
        if value is None or not nzo_id:
            return False
        data = await self._call("queue", name="priority",
                                value=nzo_id, value2=value)
        # Priority change answers {"status": true, "position": N} on
        # success; a vanished nzo answers {"status": false, ...} which
        # _call already raised on -- but some versions answer position -1.
        return data.get("status") is not False

    async def get_items_status(self, nzo_ids: list) -> dict:
        """Live status for a batch of jobs in TWO calls total (queue then
        history, both filtered by nzo_ids). Returns {nzo_id: (stage,
        percent, extra)} where stage is "downloading" (extra=timeleft),
        "postproc" (extra=SAB status name), "completed", or "failed"; ids
        SABnzbd has never seen (torrent client, pruned history) are absent.
        Uses retries=0: this feeds a 20s poll tick where a stale answer is
        cheaper than a retry chain against a wedged SABnzbd."""
        # Dedupe while preserving order: a season pack yields one downloadId
        # across many arr queue records.
        ids = list(dict.fromkeys(i for i in nzo_ids if i))
        if not ids:
            return {}
        joined = ",".join(ids)
        out: dict = {}
        data = await self._call("queue", retries=0, nzo_ids=joined)
        for slot in ((data.get("queue") or {}).get("slots") or []):
            nid = (slot or {}).get("nzo_id")
            if nid not in ids:
                continue
            try:
                percent = int(float(slot.get("percentage") or 0))
            except (TypeError, ValueError):
                percent = 0
            out[nid] = ("downloading", max(0, min(100, percent)),
                        str(slot.get("timeleft") or ""))
        missing = [i for i in ids if i not in out]
        if missing:
            data = await self._call("history", retries=0,
                                    nzo_ids=",".join(missing))
            for slot in ((data.get("history") or {}).get("slots") or []):
                nid = (slot or {}).get("nzo_id")
                if nid not in ids:
                    continue
                status = str(slot.get("status") or "")
                if status == "Completed":
                    out[nid] = ("completed", 100, "")
                elif status == "Failed":
                    out[nid] = ("failed", 100, "")
                else:
                    # Verifying / Repairing / Extracting / Moving / Queued /
                    # Running (post-processing stages)
                    out[nid] = ("postproc", 100, status)
        return out
