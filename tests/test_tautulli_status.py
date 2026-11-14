"""/status line and hot-reload wiring for the Tautulli client, and the new
settings fields."""
from __future__ import annotations

from types import SimpleNamespace

from bot import app as app_mod
from http_util import PermanentAPIError
from settings import DEFAULT_INACTIVE_SHARE_DAYS, Settings
from tautulli import TautulliClient


class _Ping:
    def __init__(self, result):
        self.result = result

    async def ping(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _app(**clients):
    data = {k: None for k in ("seerr", "radarr", "sonarr", "sabnzbd",
                              "tautulli")}
    data.update(clients)
    return SimpleNamespace(bot_data=data)


async def test_status_shows_tautulli_version():
    out = await app_mod._check_connections(
        _app(tautulli=_Ping("v2.14.5")))
    assert out["Tautulli"] == "✅ v2.14.5"


async def test_status_shows_tautulli_error():
    err = PermanentAPIError("Invalid apikey", service="Tautulli")
    out = await app_mod._check_connections(_app(tautulli=_Ping(err)))
    assert out["Tautulli"].startswith("❌")
    assert "Invalid apikey" in out["Tautulli"]


async def test_status_says_not_configured_without_tautulli():
    out = await app_mod._check_connections(_app())
    assert out["Tautulli"] == "not configured"


async def test_rebuild_makes_client_only_when_url_and_key_set():
    s = Settings(tautulli_url="http://192.0.2.10:8181",
                 tautulli_api_key="k")
    app = _app()
    app.bot_data.update(admin_id=1,
                        settings_store=SimpleNamespace(settings=s))
    app_mod._build_clients_from_settings(app)
    assert isinstance(app.bot_data["tautulli"], TautulliClient)
    await app.bot_data["tautulli"].close()
    s.tautulli_api_key = ""
    app_mod._build_clients_from_settings(app)
    assert app.bot_data["tautulli"] is None


def test_settings_defaults_and_round_trip():
    s = Settings()
    assert s.tautulli_url == "" and s.tautulli_api_key == ""
    assert s.inactive_share_days == DEFAULT_INACTIVE_SHARE_DAYS == 60
    s.tautulli_url = "http://192.0.2.10:8181"
    s.tautulli_api_key = "k"
    s.inactive_share_days = 30
    s2 = Settings.from_dict(s.to_dict())
    assert (s2.tautulli_url, s2.tautulli_api_key,
            s2.inactive_share_days) == ("http://192.0.2.10:8181", "k", 30)


def test_settings_bad_inactive_days_fall_back():
    for bad in ("many", -5, 0, None):
        assert Settings.from_dict(
            {"inactive_share_days": bad}).inactive_share_days == 60
