"""Plex OAuth (PIN flow) and user info."""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode
from xml.etree import ElementTree

import httpx

from _version import __version__ as USHER_VERSION
from fsutil import atomic_write_text
from http_util import execute, json_or_raise

logger = logging.getLogger("usher." + __name__)

_SERVICE = "Plex"

PRODUCT_NAME = "Usher"
DEVICE_NAME = "Telegram Bot"
PLATFORM = "Linux"
PLEX_API_BASE = "https://plex.tv/api/v2"
PLEX_AUTH_URL_BASE = "https://app.plex.tv/auth"
# plex.tv's sharing surface spans two API generations: the section-id map
# and share listing live on the v1 XML API (/api/servers/...), while invite
# creation and removal use the v2 JSON API (/api/v2/shared_servers).
PLEX_API_V1_BASE = "https://plex.tv/api"


@dataclass
class PlexPin:
    id: int
    code: str
    auth_url: str


@dataclass
class PlexLibrarySection:
    id: int          # GLOBAL section id (what shared_servers wants, not the
                     # server-local library key)
    title: str
    type: str        # movie / show / artist / photo


@dataclass
class PlexShare:
    id: int
    email: str
    username: str
    accepted: bool   # False = invite still pending
    all_libraries: bool


@dataclass
class PlexUser:
    """Only the fields Usher stores. id and email were previously fetched
    too -- a gratuitous PII pull nothing read; dropped in 0.12.0."""
    uuid: str
    username: str


class PlexClient:
    def __init__(self, client_id_path: str | Path = "/data/client_id"):
        self.client_id_path = Path(client_id_path)
        self.client_id = self._load_or_create_client_id()
        self._http = httpx.AsyncClient(
            timeout=15.0,
            headers={
                "Accept": "application/json",
                "X-Plex-Client-Identifier": self.client_id,
                "X-Plex-Product": PRODUCT_NAME,
                "X-Plex-Device": "Server",
                "X-Plex-Device-Name": DEVICE_NAME,
                "X-Plex-Platform": PLATFORM,
                # The real app version (was frozen at "0.4.0").
                "X-Plex-Version": USHER_VERSION,
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
        atomic_write_text(self.client_id_path, cid, chmod=None)
        logger.info("Generated new Plex client identifier")
        return cid

    async def request_pin(self, strong: bool = True) -> PlexPin:
        # strong=True returns a long opaque code suitable for the auth URL
        # deeplink (~30 min lifetime). strong=False returns a 4-char
        # human-friendly code that works at plex.tv/link (~15 min lifetime).
        r = await execute(self._http, "POST", f"{PLEX_API_BASE}/pins",
                          service=_SERVICE,
                          params={"strong": "true" if strong else "false"})
        d = json_or_raise(r, service=_SERVICE, what="PIN request")
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
        return json_or_raise(r, service=_SERVICE, what="PIN poll").get("authToken")

    # --- Server sharing (admin /invite, /uninvite) -------------------------

    async def get_owned_server(self, token: str) -> tuple[str, str]:
        """(machine_identifier, name) of the account's owned server. Raises
        LookupError when the token owns no server."""
        r = await execute(self._http, "GET", f"{PLEX_API_BASE}/resources",
                          service=_SERVICE,
                          params={"includeHttps": "1"},
                          headers={"X-Plex-Token": token})
        resources = json_or_raise(r, service=_SERVICE, what="resources",
                                  expect=list)
        for res in resources or []:
            res = res or {}
            if "server" in (res.get("provides") or "") and res.get("owned"):
                return res.get("clientIdentifier"), res.get("name") or "Plex"
        raise LookupError("this Plex account owns no server")

    async def get_library_sections(self, token: str,
                                   machine_id: str) -> list[PlexLibrarySection]:
        """The owned server's library sections with their GLOBAL ids (the
        invite POST wants these, not the local library keys)."""
        r = await execute(self._http, "GET",
                          f"{PLEX_API_V1_BASE}/servers/{machine_id}",
                          service=_SERVICE,
                          headers={"X-Plex-Token": token,
                                   "Accept": "application/xml"})
        out: list[PlexLibrarySection] = []
        for sec in ElementTree.fromstring(r.text).findall(".//Section"):
            try:
                out.append(PlexLibrarySection(
                    id=int(sec.get("id")),
                    title=sec.get("title") or "?",
                    type=sec.get("type") or "?",
                ))
            except (TypeError, ValueError):
                continue
        return out

    async def invite_to_server(self, token: str, machine_id: str, email: str,
                               section_ids: list) -> None:
        """Share the server with an email/username. Plex sends the invite
        mail; the share sits pending until accepted."""
        await execute(self._http, "POST", f"{PLEX_API_BASE}/shared_servers",
                      service=_SERVICE,
                      headers={"X-Plex-Token": token},
                      json={
                          "machineIdentifier": machine_id,
                          "invitedEmail": email,
                          "librarySectionIds": list(section_ids),
                          "settings": {},
                      })

    async def list_shares(self, token: str,
                          machine_id: str) -> list[PlexShare]:
        """Current shares (accepted + pending) on the owned server."""
        r = await execute(self._http, "GET",
                          f"{PLEX_API_V1_BASE}/servers/{machine_id}/shared_servers",
                          service=_SERVICE,
                          headers={"X-Plex-Token": token,
                                   "Accept": "application/xml"})
        out: list[PlexShare] = []
        for share in ElementTree.fromstring(r.text).findall(".//SharedServer"):
            try:
                out.append(PlexShare(
                    id=int(share.get("id")),
                    email=share.get("email") or "",
                    username=share.get("username") or "",
                    accepted=bool((share.get("acceptedAt") or "").strip()),
                    all_libraries=share.get("allLibraries") == "1",
                ))
            except (TypeError, ValueError):
                continue
        return out

    async def remove_share(self, token: str, share_id: int) -> None:
        """Revoke a share (or cancel a pending invite)."""
        await execute(self._http, "DELETE",
                      f"{PLEX_API_BASE}/shared_servers/{share_id}",
                      service=_SERVICE,
                      headers={"X-Plex-Token": token})

    async def get_user(self, auth_token: str) -> PlexUser:
        r = await execute(self._http, "GET", f"{PLEX_API_BASE}/user",
                          service=_SERVICE,
                          headers={"X-Plex-Token": auth_token})
        d = json_or_raise(r, service=_SERVICE, what="user lookup")
        return PlexUser(
            uuid=d.get("uuid", ""),
            username=d.get("username", "") or d.get("title", ""),
        )
