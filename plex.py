"""Plex OAuth (PIN flow) and user info."""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx

from http_util import execute

logger = logging.getLogger(__name__)

_SERVICE = "Plex"

PRODUCT_NAME = "Hermes"
DEVICE_NAME = "Telegram Bot"
PLATFORM = "Linux"
PLEX_API_BASE = "https://plex.tv/api/v2"
PLEX_AUTH_URL_BASE = "https://app.plex.tv/auth"


@dataclass
class PlexPin:
    id: int
    code: str
    auth_url: str


@dataclass
class PlexUser:
    id: int
    uuid: str
    username: str
    email: str


class PlexClient:
    def __init__(self, client_id_path: str | Path = "/data/client_id", version: str = "0.4.0"):
        self.client_id_path = Path(client_id_path)
        self.client_id = self._load_or_create_client_id()
        self.version = version
        self._http = httpx.AsyncClient(
            timeout=15.0,
            headers={
                "Accept": "application/json",
                "X-Plex-Client-Identifier": self.client_id,
                "X-Plex-Product": PRODUCT_NAME,
                "X-Plex-Device": "Server",
                "X-Plex-Device-Name": DEVICE_NAME,
                "X-Plex-Platform": PLATFORM,
                "X-Plex-Version": version,
            },
        )

    async def close(self) -> None:
        await self._http.aclose()

    def _load_or_create_client_id(self) -> str:
        try:
            existing = self.client_id_path.read_text().strip()
            if existing:
                return existing
        except FileNotFoundError:
            pass
        cid = str(uuid.uuid4())
        self.client_id_path.parent.mkdir(parents=True, exist_ok=True)
        self.client_id_path.write_text(cid)
        logger.info("Generated new Plex client identifier")
        return cid

    async def request_pin(self, strong: bool = True) -> PlexPin:
        # strong=True returns a long opaque code suitable for the auth URL
        # deeplink (~30 min lifetime). strong=False returns a 4-char
        # human-friendly code that works at plex.tv/link (~15 min lifetime).
        r = await execute(self._http, "POST", f"{PLEX_API_BASE}/pins",
                          service=_SERVICE,
                          params={"strong": "true" if strong else "false"})
        d = r.json()
        pin_id = d["id"]
        code = d["code"]
        params = {
            "clientID": self.client_id,
            "code": code,
            "context[device][product]": PRODUCT_NAME,
            "context[device][platform]": PLATFORM,
            "context[device][device]": DEVICE_NAME,
        }
        auth_url = f"{PLEX_AUTH_URL_BASE}#?{urlencode(params)}"
        return PlexPin(id=pin_id, code=code, auth_url=auth_url)

    async def poll_pin(self, pin_id: int) -> Optional[str]:
        """Return auth token once user has authorized, else None."""
        r = await execute(self._http, "GET", f"{PLEX_API_BASE}/pins/{pin_id}",
                          service=_SERVICE)
        return r.json().get("authToken")

    async def get_user(self, auth_token: str) -> PlexUser:
        r = await execute(self._http, "GET", f"{PLEX_API_BASE}/user",
                          service=_SERVICE,
                          headers={"X-Plex-Token": auth_token})
        d = r.json()
        return PlexUser(
            id=d.get("id", 0),
            uuid=d.get("uuid", ""),
            username=d.get("username", "") or d.get("title", ""),
            email=d.get("email", ""),
        )
