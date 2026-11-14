"""Async client for Tautulli's API v2.

Every command goes to `/api/v2?apikey=..&cmd=..` and answers the envelope
`{"response": {"result", "message", "data"}}`. `call` unwraps it and raises
the project's API errors on an error result, so callers (the later features
use it for their own commands) only see `data`.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

import httpx

from http_util import PermanentAPIError, execute, json_or_raise

logger = logging.getLogger("usher." + __name__)

_SERVICE = "Tautulli"

# How long a get_users answer is reused by the mapping helpers below.
USERS_CACHE_TTL_S = 60.0


class TautulliClient:
    """Thin wrapper over Tautulli's single-endpoint API v2."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(base_url=self.base_url,
                                         timeout=timeout)
        self._users_cache: Optional[tuple[float, list]] = None

    async def close(self) -> None:
        await self._client.aclose()

    async def call(self, cmd: str, *, post: bool = False, retries: int = 3,
                   **params):
        """Run one API command and return the envelope's `data`.

        `post=True` sends the parameters as a form body (Tautulli's
        terminate_session accepts POST only); a POST is never retried
        blindly: it is sent once. Raises PermanentAPIError when the envelope's result is
        not "success"."""
        query = {"apikey": self._api_key, "cmd": cmd}
        params = {k: v for k, v in params.items() if v is not None}
        if post:
            r = await execute(self._client, "POST", "/api/v2",
                              service=_SERVICE, retries=0,
                              params=query, data=params)
        else:
            query.update(params)
            r = await execute(self._client, "GET", "/api/v2",
                              service=_SERVICE, retries=retries,
                              params=query)
        body = json_or_raise(r, service=_SERVICE, what=f"Tautulli {cmd}")
        resp = body.get("response")
        if not isinstance(resp, dict):
            raise PermanentAPIError(
                f"returned an unexpected response shape for Tautulli {cmd}",
                service=_SERVICE)
        if resp.get("result") != "success":
            raise PermanentAPIError(
                str(resp.get("message") or f"Tautulli rejected {cmd}"),
                service=_SERVICE)
        return resp.get("data")

    async def ping(self) -> str:
        data = await self.call("get_tautulli_info")
        if not isinstance(data, dict):
            return "?"
        return str(data.get("tautulli_version", "?"))

    async def get_users(self) -> list:
        """Tautulli's user list, cached for USERS_CACHE_TTL_S so a burst of
        mapping lookups costs one call."""
        now = time.monotonic()
        if (self._users_cache is not None
                and now - self._users_cache[0] < USERS_CACHE_TTL_S):
            return self._users_cache[1]
        data = await self.call("get_users")
        users = [u for u in (data or []) if isinstance(u, dict)]
        self._users_cache = (now, users)
        return users

    async def get_users_table(self, length: int = 1000) -> list:
        """Every user's row with `last_seen` (epoch seconds, empty when the
        person never watched), unwrapped from the `{"data": [...]}` block
        Tautulli wraps them in. `length` lifts Tautulli's page size of 25."""
        data = await self.call("get_users_table", length=length)
        rows = data.get("data") if isinstance(data, dict) else None
        return [r for r in (rows or []) if isinstance(r, dict)]

    async def get_user_watch_time_stats(self, user_id, days=(7, 30)) -> list:
        """A user's plays and watch time per window: a list of rows with
        `query_days`, `total_plays` and `total_time` (seconds)."""
        data = await self.call("get_user_watch_time_stats", user_id=user_id,
                               query_days=",".join(str(d) for d in days))
        return [r for r in (data or []) if isinstance(r, dict)]

    async def get_home_stats(self, **params) -> list:
        """Tautulli's home stats: a list of `{stat_id, rows}` blocks."""
        data = await self.call("get_home_stats", **params)
        return [b for b in (data or []) if isinstance(b, dict)]

    async def get_metadata(self, rating_key) -> dict:
        """One Plex item's metadata (its `guids` hold the TMDb id)."""
        data = await self.call("get_metadata", rating_key=rating_key)
        return data if isinstance(data, dict) else {}

    async def get_history(self, **params) -> list:
        """History rows (newest first by default), unwrapped from the
        `{"data": [...]}` block Tautulli wraps them in."""
        data = await self.call("get_history", **params)
        rows = data.get("data") if isinstance(data, dict) else None
        return [r for r in (rows or []) if isinstance(r, dict)]

    async def get_stream_data(self, row_id) -> dict:
        """How one history row was streamed (player, platform,
        transcode_decision, codecs)."""
        data = await self.call("get_stream_data", row_id=row_id)
        return data if isinstance(data, dict) else {}

    async def get_activity(self) -> dict:
        """What is playing now: a dict whose `sessions` list holds one row
        per stream (`session_id`, `friendly_name`, `full_title`, `player`,
        `transcode_decision`)."""
        data = await self.call("get_activity")
        return data if isinstance(data, dict) else {}

    async def get_server_status(self) -> Optional[bool]:
        """Whether Tautulli can reach its Plex server (`connected`); None
        when the reply has no boolean."""
        data = await self.call("server_status")
        conn = data.get("connected") if isinstance(data, dict) else None
        return conn if isinstance(conn, bool) else None

    async def terminate_session(self, session_id, message=None) -> None:
        """Stop one stream, optionally showing the viewer `message`. Tautulli
        accepts this command by POST only, and it is sent once."""
        await self.call("terminate_session", post=True,
                        session_id=session_id, message=message)


_TMDB_GUID = re.compile(
    r"^(?:tmdb://|com\.plexapp\.agents\.themoviedb://)(\d+)")


def tmdb_id_from_metadata(meta: dict) -> Optional[int]:
    """The TMDb id in a get_metadata answer's guids (new `tmdb://123` form
    or the legacy agent form), or None when it has none."""
    guids = list(meta.get("guids") or [])
    if meta.get("guid"):
        guids.append(meta["guid"])
    for g in guids:
        m = _TMDB_GUID.match(str(g))
        if m:
            return int(m.group(1))
    return None


def _same(a, b) -> bool:
    a = (a or "").strip().lower()
    return bool(a) and a == (b or "").strip().lower()


async def find_user_by_plex_username(client: TautulliClient,
                                     plex_username: Optional[str]
                                     ) -> Optional[dict]:
    """The Tautulli user row for a Linked user's Plex username (letter case
    ignored), or None. Tautulli's `user_id` is the Plex account id."""
    if not (plex_username or "").strip():
        return None
    for u in await client.get_users():
        if _same(u.get("username"), plex_username):
            return u
    return None


async def find_user_for_share(client: TautulliClient, email: Optional[str],
                              username: Optional[str]) -> Optional[dict]:
    """The Tautulli user row for a Share: matched by email first, then by
    username (letter case ignored), or None."""
    return match_share_user(await client.get_users(), email, username)


def match_share_user(users: list, email: Optional[str],
                     username: Optional[str]) -> Optional[dict]:
    """The row among `users` (get_users or get_users_table rows) that
    belongs to a Share: email first, then username, letter case ignored."""
    for u in users:
        if _same(u.get("email"), email):
            return u
    for u in users:
        if _same(u.get("username"), username):
            return u
    return None
