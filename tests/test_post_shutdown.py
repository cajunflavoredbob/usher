"""_post_shutdown closes every API client the bot builds."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.app import _post_shutdown


async def test_post_shutdown_closes_sabnzbd_client():
    clients = {k: SimpleNamespace(close=AsyncMock())
               for k in ("seerr", "radarr", "sonarr", "sabnzbd", "plex")}
    app = SimpleNamespace(bot_data=dict(clients))
    await _post_shutdown(app)
    for key, client in clients.items():
        assert client.close.await_count == 1, key
