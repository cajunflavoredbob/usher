"""The logged-in Settings page must not render stored secrets into HTML, a
blank secret on POST keeps the stored one, and a connection Test with a blank
secret field uses the stored one."""
from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import webui

SECRET = b"0123456789abcdef0123456789abcdef"
_SESSION = webui._make_session_cookie(SECRET, "admin")
CSRF = webui.generate_csrf_token(SECRET, _SESSION.rsplit(".", 1)[-1])
HINT = "Already set. Leave blank to keep it."

VALUES = {
    "telegram_bot_token": "111:STORED-bot-token",
    "seerr_api_key": "STORED-seerr-key",
    "radarr_api_key": "STORED-radarr-key",
    "sonarr_api_key": "STORED-sonarr-key",
    "sabnzbd_api_key": "STORED-sab-key",
    "tautulli_api_key": "STORED-tautulli-key",
    "webhook_secret": "STORED-webhook-secret",
}


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.delenv("USHER_WEBHOOK_SECRET", raising=False)
    path = tmp_path / "settings.json"
    store = webui.SettingsStore(path)
    st = store.settings
    st.admin.username = "admin"
    st.admin.password_hash = "x"
    st.admin_telegram_id = 4242
    st.seerr_url = "http://192.0.2.10:5056"
    st.radarr_url = "http://192.0.2.10:7878"
    st.sonarr_url = "http://192.0.2.10:8989"
    st.sabnzbd_url = "http://192.0.2.10:8080"
    st.tautulli_url = "http://192.0.2.10:8181"
    for k, v in VALUES.items():
        setattr(st, k, v)
    store.save()
    app = web.Application()
    webui.attach_webui(app, settings_store=store, session_secret=SECRET,
                       data_dir=tmp_path, settings_path=path,
                       db_path=tmp_path / "m.sqlite", http_port=8765)
    async with TestClient(TestServer(app)) as c:
        c.store = store
        yield c


def _cookies():
    return {webui.SESSION_COOKIE: _SESSION, "usher_csrf": CSRF}


async def _post(c, path, fields):
    data = {"csrf_token": CSRF, **fields}
    return await c.post(path, data=data, cookies=_cookies())


async def test_admin_page_renders_no_stored_secret(client):
    r = await client.get("/admin", cookies=_cookies())
    html = await r.text()
    for v in VALUES.values():
        assert v not in html
    assert html.count(HINT) >= len(VALUES)
    # Non-secret fields stay pre-filled.
    assert "http://192.0.2.10:5056" in html
    assert 'value="4242"' in html


async def test_save_response_page_renders_no_stored_secret(client):
    r = await _post(client, "/admin/seerr",
                    {"seerr_url": "http://192.0.2.10:5056",
                     "seerr_api_key": "BRAND-NEW-KEY"})
    html = await r.text()
    assert "BRAND-NEW-KEY" not in html
    for v in VALUES.values():
        assert v not in html


@pytest.mark.parametrize("path,extra,field", [
    ("/admin/telegram", {"admin_telegram_id": "4242"}, "telegram_bot_token"),
    ("/admin/seerr", {"seerr_url": "http://192.0.2.10:5056"}, "seerr_api_key"),
    ("/admin/autofix", {"daily_autofix_limit": "5"}, "radarr_api_key"),
    ("/admin/autofix", {"daily_autofix_limit": "5"}, "sonarr_api_key"),
    ("/admin/sabnzbd", {"sabnzbd_url": "http://192.0.2.10:8080"},
     "sabnzbd_api_key"),
    ("/admin/tautulli", {"tautulli_url": "http://192.0.2.10:8181",
                         "inactive_share_days": "60"}, "tautulli_api_key"),
    ("/admin/webhook", {}, "webhook_secret"),
])
async def test_blank_secret_post_keeps_stored_value(client, path, extra, field):
    r = await _post(client, path, {**extra, field: ""})
    assert r.status == 200
    assert getattr(client.store.settings, field) == VALUES[field]


async def test_typed_secret_post_replaces_stored_value(client):
    await _post(client, "/admin/seerr",
                {"seerr_url": "http://192.0.2.10:5056", "seerr_api_key": "new"})
    assert client.store.settings.seerr_api_key == "new"


async def test_blank_webhook_secret_with_none_stored_is_refused(client):
    client.store.settings.webhook_secret = ""
    r = await _post(client, "/admin/webhook", {"webhook_secret": ""})
    assert r.status == 400


async def test_blank_bot_token_with_none_stored_is_refused(client):
    client.store.settings.telegram_bot_token = ""
    r = await _post(client, "/admin/telegram",
                    {"telegram_bot_token": "", "admin_telegram_id": "4242"})
    assert r.status == 400


class _Ping:
    seen: list = []

    def __init__(self, url, key, *a, **kw):
        _Ping.seen.append((url, key))

    async def ping(self):
        return "1"

    async def close(self):
        pass


async def test_blank_field_tests_use_stored_secrets(client, monkeypatch):
    _Ping.seen = []
    for name in ("SeerrClient", "RadarrClient", "SonarrClient"):
        monkeypatch.setattr(webui, name, _Ping)
    import sabnzbd
    monkeypatch.setattr(sabnzbd, "SabnzbdClient", _Ping)
    import tautulli
    monkeypatch.setattr(tautulli, "TautulliClient", _Ping)

    async def go(which, fields):
        r = await _post(client, f"/admin/test/{which}", fields)
        return json.loads(await r.text())

    assert (await go("seerr", {"seerr_url": "http://192.0.2.10:5056",
                               "seerr_api_key": ""}))["ok"]
    assert (await go("autofix", {"radarr_url": "http://192.0.2.10:7878",
                                 "radarr_api_key": "",
                                 "sonarr_url": "http://192.0.2.10:8989",
                                 "sonarr_api_key": ""}))["ok"]
    assert (await go("sabnzbd", {"sabnzbd_url": "http://192.0.2.10:8080",
                                 "sabnzbd_api_key": ""}))["ok"]
    assert (await go("tautulli", {"tautulli_url": "http://192.0.2.10:8181",
                                  "tautulli_api_key": ""}))["ok"]
    keys = [k for _, k in _Ping.seen]
    assert keys == [VALUES["seerr_api_key"], VALUES["radarr_api_key"],
                    VALUES["sonarr_api_key"], VALUES["sabnzbd_api_key"],
                    VALUES["tautulli_api_key"]]


async def test_blank_field_telegram_test_uses_stored_token(client, monkeypatch):
    seen = []

    class _H:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            seen.append(url)

            class R:
                status_code = 200

                def json(self):
                    return {"ok": True, "result": {"username": "b"}}
            return R()

    monkeypatch.setattr(webui.httpx, "AsyncClient", lambda *a, **k: _H())
    r = await _post(client, "/admin/test/telegram", {"telegram_bot_token": ""})
    assert (await r.json())["ok"] is True
    assert VALUES["telegram_bot_token"] in seen[0]


async def test_typed_telegram_and_webhook_secrets_replace_stored(client):
    await _post(client, "/admin/telegram",
                {"telegram_bot_token": "222:new", "admin_telegram_id": "4242"})
    await _post(client, "/admin/webhook", {"webhook_secret": "new-secret"})
    assert client.store.settings.telegram_bot_token == "222:new"
    assert client.store.settings.webhook_secret == "new-secret"


async def test_whitespace_only_key_in_autofix_test_uses_stored_key(
        client, monkeypatch):
    _Ping.seen = []
    monkeypatch.setattr(webui, "RadarrClient", _Ping)
    r = await _post(client, "/admin/test/autofix",
                    {"radarr_url": "http://192.0.2.10:7878",
                     "radarr_api_key": "   "})
    assert json.loads(await r.text())["ok"]
    assert _Ping.seen == [("http://192.0.2.10:7878",
                           VALUES["radarr_api_key"])]


# Clear control for optional keys

@pytest.mark.parametrize("path,extra,field", [
    ("/admin/autofix", {"daily_autofix_limit": "5"}, "radarr_api_key"),
    ("/admin/autofix", {"daily_autofix_limit": "5"}, "sonarr_api_key"),
    ("/admin/sabnzbd", {"sabnzbd_url": "http://192.0.2.10:8080"},
     "sabnzbd_api_key"),
    ("/admin/tautulli", {"tautulli_url": "http://192.0.2.10:8181",
                         "inactive_share_days": "60"}, "tautulli_api_key"),
])
async def test_ticked_remove_box_clears_optional_key_even_if_typed(
        client, path, extra, field):
    r = await _post(client, path,
                    {**extra, field: "typed-anyway", f"clear_{field}": "on"})
    assert r.status == 200
    assert getattr(client.store.settings, field) == ""


async def test_remove_box_shown_only_for_optional_keys_with_a_stored_value(
        client):
    html = await (await client.get("/admin", cookies=_cookies())).text()
    for field in ("radarr_api_key", "sonarr_api_key", "sabnzbd_api_key",
                  "tautulli_api_key"):
        assert f'name="clear_{field}"' in html
    for field in ("telegram_bot_token", "seerr_api_key", "webhook_secret"):
        assert f"clear_{field}" not in html
    client.store.settings.radarr_api_key = ""
    html = await (await client.get("/admin", cookies=_cookies())).text()
    assert 'name="clear_radarr_api_key"' not in html


async def test_remove_box_on_required_secret_is_ignored(client):
    await _post(client, "/admin/seerr",
                {"seerr_url": "http://192.0.2.10:5056", "seerr_api_key": "",
                 "clear_seerr_api_key": "on"})
    assert client.store.settings.seerr_api_key == VALUES["seerr_api_key"]


# Test with a blank key must not send the stored key to a different URL

def _patch_clients(monkeypatch):
    _Ping.seen = []
    for name in ("SeerrClient", "RadarrClient", "SonarrClient"):
        monkeypatch.setattr(webui, name, _Ping)
    import sabnzbd
    monkeypatch.setattr(sabnzbd, "SabnzbdClient", _Ping)
    import tautulli
    monkeypatch.setattr(tautulli, "TautulliClient", _Ping)


@pytest.mark.parametrize("which,urlfield,keyfield", [
    ("seerr", "seerr_url", "seerr_api_key"),
    ("sabnzbd", "sabnzbd_url", "sabnzbd_api_key"),
    ("tautulli", "tautulli_url", "tautulli_api_key"),
    ("autofix", "radarr_url", "radarr_api_key"),
    ("autofix", "sonarr_url", "sonarr_api_key"),
])
async def test_blank_key_test_on_changed_url_is_refused(
        client, monkeypatch, which, urlfield, keyfield):
    _patch_clients(monkeypatch)
    r = await _post(client, f"/admin/test/{which}",
                    {urlfield: "http://198.51.100.7:1234", keyfield: ""})
    body = json.loads(await r.text())
    assert body["ok"] is False
    assert "type the key again" in body["detail"].lower()
    assert _Ping.seen == []


async def test_blank_key_test_accepts_stored_url_with_trailing_slash(
        client, monkeypatch):
    _patch_clients(monkeypatch)
    r = await _post(client, "/admin/test/seerr",
                    {"seerr_url": " http://192.0.2.10:5056/ ",
                     "seerr_api_key": ""})
    assert json.loads(await r.text())["ok"]
    assert _Ping.seen[0][1] == VALUES["seerr_api_key"]


async def test_typed_key_test_on_changed_url_still_runs(client, monkeypatch):
    _patch_clients(monkeypatch)
    r = await _post(client, "/admin/test/seerr",
                    {"seerr_url": "http://198.51.100.7:1234",
                     "seerr_api_key": "typed"})
    assert json.loads(await r.text())["ok"]
    assert _Ping.seen == [("http://198.51.100.7:1234", "typed")]


async def test_autofix_test_refuses_changed_radarr_but_pings_unchanged_sonarr(
        client, monkeypatch):
    _patch_clients(monkeypatch)
    r = await _post(client, "/admin/test/autofix",
                    {"radarr_url": "http://198.51.100.7:7878",
                     "radarr_api_key": "",
                     "sonarr_url": "http://192.0.2.10:8989",
                     "sonarr_api_key": ""})
    body = json.loads(await r.text())
    assert body["ok"] is False
    assert "Radarr" in body["detail"]
    assert _Ping.seen == [("http://192.0.2.10:8989",
                           VALUES["sonarr_api_key"])]


# Tautulli tab: inactive days, test failure, and the status of "not linked"

async def test_tautulli_save_stores_url_and_inactive_days(client):
    r = await _post(client, "/admin/tautulli",
                    {"tautulli_url": "http://192.0.2.10:8181",
                     "tautulli_api_key": "", "inactive_share_days": "90"})
    assert r.status == 200
    assert client.store.settings.inactive_share_days == 90
    assert client.store.settings.tautulli_url == "http://192.0.2.10:8181"


@pytest.mark.parametrize("bad", ["", "0", "-3", "abc"])
async def test_tautulli_save_refuses_bad_inactive_days(client, bad):
    r = await _post(client, "/admin/tautulli",
                    {"tautulli_url": "http://192.0.2.10:8181",
                     "inactive_share_days": bad})
    assert r.status == 400
    assert client.store.settings.inactive_share_days == 60


async def test_tautulli_test_reports_version_and_error(client, monkeypatch):
    import tautulli
    from http_util import PermanentAPIError

    class _T:
        fail = False

        def __init__(self, *a, **k):
            pass

        async def ping(self):
            if _T.fail:
                raise PermanentAPIError("Invalid apikey", service="Tautulli")
            return "v2.14.5"

        async def close(self):
            pass

    monkeypatch.setattr(tautulli, "TautulliClient", _T)
    fields = {"tautulli_url": "http://192.0.2.10:8181",
              "tautulli_api_key": "typed"}
    body = json.loads(await (await _post(
        client, "/admin/test/tautulli", fields)).text())
    assert body["ok"] and "v2.14.5" in body["detail"]
    _T.fail = True
    body = json.loads(await (await _post(
        client, "/admin/test/tautulli", fields)).text())
    assert not body["ok"] and "Invalid apikey" in body["detail"]


# Alarms block on the Telegram tab

async def test_telegram_save_stores_alarm_switches_and_thresholds(client):
    r = await _post(client, "/admin/telegram",
                    {"admin_telegram_id": "4242", "alarm_disk": "on",
                     "low_disk_percent": "15", "low_disk_gb": "100"})
    assert r.status == 200
    s = client.store.settings
    assert (s.alarm_health, s.alarm_disk) == (False, True)
    assert (s.low_disk_percent, s.low_disk_gb) == (15, 100)


@pytest.mark.parametrize("field,bad", [("low_disk_percent", "101"),
                                       ("low_disk_percent", "x"),
                                       ("low_disk_gb", "-1")])
async def test_telegram_save_refuses_bad_disk_thresholds(client, field, bad):
    r = await _post(client, "/admin/telegram",
                    {"admin_telegram_id": "4242", field: bad})
    assert r.status == 400
    assert client.store.settings.low_disk_percent == 10
    assert client.store.settings.low_disk_gb == 0


def test_alarm_settings_default_on_with_10_percent_and_gb_limit_off():
    from settings import Settings
    s = Settings.from_dict({})
    assert (s.alarm_health, s.alarm_disk, s.low_disk_percent, s.low_disk_gb) \
        == (True, True, 10, 0)


async def test_telegram_save_stores_the_plex_alarm_switch(client):
    r = await _post(client, "/admin/telegram",
                    {"admin_telegram_id": "4242", "alarm_plex": "on"})
    assert r.status == 200
    assert client.store.settings.alarm_plex is True
    r = await _post(client, "/admin/telegram", {"admin_telegram_id": "4242"})
    assert client.store.settings.alarm_plex is False


def test_plex_alarm_defaults_on():
    from settings import Settings
    assert Settings.from_dict({}).alarm_plex is True
