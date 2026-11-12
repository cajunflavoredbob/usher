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
    keys = [k for _, k in _Ping.seen]
    assert keys == [VALUES["seerr_api_key"], VALUES["radarr_api_key"],
                    VALUES["sonarr_api_key"], VALUES["sabnzbd_api_key"]]


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
