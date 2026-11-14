"""A malformed service URL is refused at save, and one already saved never
stops the bot (and its Admin page) from starting."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot import app as app_mod
from settings import Settings, validate_public_url


@pytest.mark.parametrize("url", [
    "http://tautulli:port",   # a pasted placeholder port
    "http://[::1",            # unclosed IPv6 bracket
    "http://",                # no host
])
def test_urls_the_clients_would_refuse_are_rejected(url):
    assert validate_public_url(url) is not None


@pytest.mark.parametrize("url", [
    "", "http://192.0.2.10:8181", "https://media.example.com/tautulli",
])
def test_ordinary_urls_still_pass(url):
    assert validate_public_url(url) is None


async def test_one_bad_saved_url_leaves_only_that_service_off():
    s = Settings(tautulli_url="http://tautulli:port", tautulli_api_key="k",
                 sabnzbd_url="http://192.0.2.10:8080", sabnzbd_api_key="k")
    data = {k: None for k in ("seerr", "radarr", "sonarr", "sabnzbd",
                              "tautulli")}
    data.update(admin_id=1, settings_store=SimpleNamespace(settings=s))
    app = SimpleNamespace(bot_data=data)
    app_mod._build_clients_from_settings(app)
    assert app.bot_data["tautulli"] is None
    assert app.bot_data["sabnzbd"] is not None
    await app.bot_data["sabnzbd"].close()
