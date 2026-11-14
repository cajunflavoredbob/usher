"""Service alarms: Arr health and disk clients, the alarm job and /status."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from bot import app as app_mod
from bot.alarms import check_alarms, status_section
from radarr import RadarrClient
from sonarr import SonarrClient

from tests._handler_harness import make_ctx, make_update

GB = 1024 ** 3
ADMIN = 999


# --- clients -----------------------------------------------------------------

def _mock(cls, url, payload_by_path):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path in payload_by_path:
            return httpx.Response(200, json=payload_by_path[request.url.path])
        return httpx.Response(404, json={"message": "nope"})

    c = cls(url, "key")
    c._client = httpx.AsyncClient(base_url=f"{url}/api/v3",
                                  transport=httpx.MockTransport(handler))
    return c, seen


@pytest.mark.parametrize("cls,url", [(RadarrClient, "http://radarr.test"),
                                     (SonarrClient, "http://sonarr.test")])
async def test_arr_clients_read_health_and_disk_space(cls, url):
    health = [{"source": "IndexerCheck", "type": "warning", "message": "m",
               "wikiUrl": "https://wiki.test/x"}]
    disks = [{"path": "/data", "label": "", "freeSpace": 5, "totalSpace": 50}]
    c, seen = _mock(cls, url, {"/api/v3/health": health,
                               "/api/v3/diskspace": disks})
    assert await c.get_health() == health
    assert await c.get_disk_space() == disks
    assert [r.url.path for r in seen] == ["/api/v3/health", "/api/v3/diskspace"]
    await c.close()


# --- the job -----------------------------------------------------------------

def _arr(health=None, disks=None):
    return SimpleNamespace(
        get_health=AsyncMock(side_effect=health if isinstance(health, Exception)
                             else None, return_value=health or []),
        get_disk_space=AsyncMock(side_effect=disks if isinstance(disks, Exception)
                                 else None, return_value=disks or []),
        ping=AsyncMock(return_value="1"))


def _ctx(fresh_store, radarr=None, sonarr=None, **settings):
    ctx = make_ctx(admin_id=ADMIN, bot_data_overrides={
        "store": fresh_store, "radarr": radarr, "sonarr": sonarr})
    cfg = ctx.bot_data["settings_store"].settings
    cfg.alarm_health = settings.get("alarm_health", True)
    cfg.alarm_disk = settings.get("alarm_disk", True)
    cfg.low_disk_percent = settings.get("low_disk_percent", 10)
    cfg.low_disk_gb = settings.get("low_disk_gb", 50)
    return ctx


def _sent(ctx):
    return [c.kwargs["text"] for c in ctx.bot.send_message.await_args_list]


def _health(type_="warning", source="IndexerStatusCheck", msg="Indexers down",
            wiki="https://wiki.test/idx"):
    return {"source": source, "type": type_, "message": msg, "wikiUrl": wiki}


def _disk(path="/data", free_gb=40, total_gb=500):
    return {"path": path, "label": "", "freeSpace": int(free_gb * GB),
            "totalSpace": int(total_gb * GB)}


async def test_new_warning_dms_once_naming_service_source_message_and_wiki(fresh_store):
    r = _arr(health=[_health()])
    ctx = _ctx(fresh_store, radarr=r)
    await check_alarms(ctx)
    await check_alarms(ctx)
    await check_alarms(ctx)
    sent = _sent(ctx)
    assert len(sent) == 1
    for word in ("Radarr", "IndexerStatusCheck", "Indexers down",
                 "https://wiki.test/idx"):
        assert word in sent[0]
    assert ctx.bot.send_message.await_args.kwargs["chat_id"] == ADMIN


async def test_clearing_dms_once(fresh_store):
    r = _arr(health=[_health()])
    ctx = _ctx(fresh_store, radarr=r)
    await check_alarms(ctx)
    r.get_health.return_value = []
    await check_alarms(ctx)
    await check_alarms(ctx)
    sent = _sent(ctx)
    assert len(sent) == 2
    assert sent[1].startswith("✅ Cleared") and "Indexers down" in sent[1]
    assert await fresh_store.list_alarms() == []


async def test_notice_and_ok_items_send_nothing(fresh_store):
    r = _arr(health=[_health("notice"), _health("ok", msg="fine")])
    ctx = _ctx(fresh_store, radarr=r)
    await check_alarms(ctx)
    assert _sent(ctx) == []


async def test_wiki_object_form_is_read(fresh_store):
    item = _health()
    item["wikiUrl"] = {"fullUri": "https://wiki.test/obj"}
    ctx = _ctx(fresh_store, radarr=_arr(health=[item]))
    await check_alarms(ctx)
    assert "https://wiki.test/obj" in _sent(ctx)[0]


async def test_same_message_on_two_services_is_two_alarms(fresh_store):
    ctx = _ctx(fresh_store, radarr=_arr(health=[_health()]),
               sonarr=_arr(health=[_health()]))
    await check_alarms(ctx)
    assert len(_sent(ctx)) == 2


async def test_disk_at_8_percent_alarms_once_for_both_apps_and_recovers_once(fresh_store):
    full = [_disk(free_gb=40, total_gb=500)]
    r, s = _arr(disks=full), _arr(disks=full)
    ctx = _ctx(fresh_store, radarr=r, sonarr=s, low_disk_gb=10)
    await check_alarms(ctx)
    await check_alarms(ctx)
    sent = _sent(ctx)
    assert len(sent) == 1 and "/data" in sent[0] and "8% free" in sent[0]
    ok = [_disk(free_gb=200, total_gb=500)]
    r.get_disk_space.return_value = ok
    s.get_disk_space.return_value = ok
    await check_alarms(ctx)
    await check_alarms(ctx)
    sent = _sent(ctx)
    assert len(sent) == 2 and sent[1].startswith("✅ Cleared")


async def test_disk_below_gb_threshold_alarms_even_when_percent_is_fine(fresh_store):
    ctx = _ctx(fresh_store, radarr=_arr(disks=[_disk(free_gb=30, total_gb=100)]))
    await check_alarms(ctx)
    assert len(_sent(ctx)) == 1


async def test_disk_above_both_thresholds_sends_nothing(fresh_store):
    ctx = _ctx(fresh_store, radarr=_arr(disks=[_disk(free_gb=300, total_gb=1000)]))
    await check_alarms(ctx)
    assert _sent(ctx) == []


async def test_health_switch_off_sends_none_for_health_but_disk_still_alarms(fresh_store):
    ctx = _ctx(fresh_store, alarm_health=False,
               radarr=_arr(health=[_health()], disks=[_disk()]))
    await check_alarms(ctx)
    sent = _sent(ctx)
    assert len(sent) == 1 and "Low disk" in sent[0]


async def test_disk_switch_off_sends_none_for_disk_but_health_still_alarms(fresh_store):
    ctx = _ctx(fresh_store, alarm_disk=False,
               radarr=_arr(health=[_health()], disks=[_disk()]))
    await check_alarms(ctx)
    sent = _sent(ctx)
    assert len(sent) == 1 and "Indexers down" in sent[0]


async def test_an_app_that_cannot_be_asked_does_not_clear_its_alarms(fresh_store):
    r = _arr(health=[_health()])
    ctx = _ctx(fresh_store, radarr=r)
    await check_alarms(ctx)
    r.get_health.side_effect = RuntimeError("down")
    await check_alarms(ctx)
    assert len(_sent(ctx)) == 1
    assert len(await fresh_store.list_alarms()) == 1


async def test_failed_dm_is_retried_next_check(fresh_store):
    ctx = _ctx(fresh_store, radarr=_arr(health=[_health()]))
    ctx.bot.send_message.side_effect = [RuntimeError("blocked"), None]
    await check_alarms(ctx)
    await check_alarms(ctx)
    assert ctx.bot.send_message.await_count == 2
    assert [n for _k, _t, n in await fresh_store.list_alarms()] == [True]


# --- /status -----------------------------------------------------------------

async def test_status_shows_health_and_free_space_lines(fresh_store):
    ctx = _ctx(fresh_store,
               radarr=_arr(health=[_health()], disks=[_disk()]),
               sonarr=_arr(disks=[_disk()]))
    text = await status_section(ctx)
    assert "<b>Health:</b>" in text and "Indexers down" in text
    assert "<b>Sonarr</b>: no problems" in text
    assert "<b>Free space:</b>" in text and "/data: 8% free" in text
    assert text.count("/data") == 1


async def test_status_has_no_section_without_arr_apps(fresh_store):
    assert await status_section(_ctx(fresh_store)) == ""


async def test_cmd_status_includes_the_section(fresh_store, monkeypatch):
    monkeypatch.setattr(app_mod, "_check_connections",
                        AsyncMock(return_value={"Seerr": "ok"}))
    ctx = _ctx(fresh_store, radarr=_arr(disks=[_disk()]))
    ctx.bot_data["tautulli"] = None
    upd = make_update(text="/status", user_id=ADMIN)
    await app_mod.cmd_status(upd, ctx)
    assert "Free space" in upd.effective_message.reply_calls[0]["text"]


async def test_problem_that_clears_before_its_dm_got_through_clears_silently(fresh_store):
    r = _arr(health=[_health()])
    ctx = _ctx(fresh_store, radarr=r)
    ctx.bot.send_message.side_effect = RuntimeError("blocked")
    await check_alarms(ctx)
    ctx.bot.send_message.side_effect = None
    r.get_health.return_value = []
    await check_alarms(ctx)
    assert ctx.bot.send_message.await_count == 1
    assert await fresh_store.list_alarms() == []
