"""Plex server offline alarm: the plex.tv presence and Tautulli status
clients, the two-strike job rule, recovery, the switch, and /status."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.alarms import check_alarms, status_section
from plex import PlexClient
from tautulli import TautulliClient

from tests._handler_harness import make_ctx, make_link

ADMIN = 999


# --- clients -----------------------------------------------------------------

async def test_plex_client_reads_presence_of_the_owned_server(tmp_path):
    def handler(request):
        assert request.headers["X-Plex-Token"] == "tok"
        return httpx.Response(200, json=[
            {"provides": "server", "owned": 0, "presence": True},
            {"provides": "server", "owned": 1, "presence": False,
             "lastSeenAt": "2026-11-14T00:00:00Z"},
        ])

    c = PlexClient(client_id_path=tmp_path / "client_id")
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await c.get_owned_server_online("tok") is False
    await c.close()


async def test_tautulli_client_reads_connected():
    def handler(request):
        assert request.url.params["cmd"] == "server_status"
        return httpx.Response(200, json={"response": {
            "result": "success", "data": {"connected": False}}})

    c = TautulliClient("http://tau.test:8181", "k")
    c._client = httpx.AsyncClient(base_url="http://tau.test:8181",
                                  transport=httpx.MockTransport(handler))
    assert await c.get_server_status() is False
    await c.close()


# --- the job -----------------------------------------------------------------

def _plex(*states):
    """get_owned_server_online answers in turn; an Exception is raised."""
    return SimpleNamespace(get_owned_server_online=AsyncMock(
        side_effect=list(states)))


def _tautulli(*states):
    return SimpleNamespace(get_server_status=AsyncMock(
        side_effect=list(states)))


def _ctx(fresh_store, plex=None, tautulli=None, token="tok", alarm_plex=True):
    ctx = make_ctx(admin_id=ADMIN, bot_data_overrides={
        "store": fresh_store, "radarr": None, "sonarr": None,
        "plex": plex, "tautulli": tautulli})
    fresh_store.get = AsyncMock(
        return_value=make_link(telegram_id=ADMIN, plex_token=token)
        if token else None)
    ctx.bot_data["settings_store"].settings.alarm_plex = alarm_plex
    return ctx


def _sent(ctx):
    return [c.kwargs["text"] for c in ctx.bot.send_message.await_args_list]


async def test_one_offline_check_sends_nothing(fresh_store):
    ctx = _ctx(fresh_store, plex=_plex(False))
    await check_alarms(ctx)
    assert _sent(ctx) == []


async def test_two_offline_checks_in_a_row_send_one_dm_and_recovery_sends_one(fresh_store):
    ctx = _ctx(fresh_store, plex=_plex(False, False, False, True, True))
    for _ in range(3):
        await check_alarms(ctx)
    sent = _sent(ctx)
    assert len(sent) == 1 and "Plex" in sent[0] and "offline" in sent[0]
    await check_alarms(ctx)
    await check_alarms(ctx)
    sent = _sent(ctx)
    assert len(sent) == 2 and sent[1].startswith("✅ Cleared")
    assert await fresh_store.list_alarms() == []


async def test_a_blip_between_offline_checks_resets_the_count(fresh_store):
    ctx = _ctx(fresh_store, plex=_plex(False, True, False))
    for _ in range(3):
        await check_alarms(ctx)
    assert _sent(ctx) == []


async def test_a_check_that_cannot_run_is_neither_a_strike_nor_a_recovery(fresh_store):
    ctx = _ctx(fresh_store,
               plex=_plex(False, RuntimeError("plex.tv down"), False, RuntimeError("x")))
    for _ in range(3):
        await check_alarms(ctx)
    assert len(_sent(ctx)) == 1     # strikes 1, skip, 2
    await check_alarms(ctx)
    assert len(await fresh_store.list_alarms()) == 1   # not cleared


async def test_tautulli_not_connected_alone_counts_as_offline(fresh_store):
    ctx = _ctx(fresh_store, plex=_plex(True, True),
               tautulli=_tautulli(False, False))
    await check_alarms(ctx)
    await check_alarms(ctx)
    assert len(_sent(ctx)) == 1


async def test_tautulli_connected_with_plex_tv_offline_still_counts_offline(fresh_store):
    ctx = _ctx(fresh_store, plex=_plex(False, False),
               tautulli=_tautulli(True, True))
    await check_alarms(ctx)
    await check_alarms(ctx)
    assert len(_sent(ctx)) == 1


async def test_no_admin_plex_token_skips_the_check(fresh_store):
    plex = _plex(False, False)
    ctx = _ctx(fresh_store, plex=plex, token=None)
    await check_alarms(ctx)
    await check_alarms(ctx)
    assert _sent(ctx) == []
    plex.get_owned_server_online.assert_not_awaited()
    assert "skipped" in await status_section(ctx)


async def test_switch_off_sends_none_and_forgets_an_active_alarm(fresh_store):
    ctx = _ctx(fresh_store, plex=_plex(False, False, False, False))
    await check_alarms(ctx)
    await check_alarms(ctx)
    assert len(_sent(ctx)) == 1
    ctx.bot_data["settings_store"].settings.alarm_plex = False
    await check_alarms(ctx)
    await check_alarms(ctx)
    assert len(_sent(ctx)) == 1
    assert await fresh_store.list_alarms() == []


@pytest.mark.parametrize("state,word", [(True, "online"), (False, "offline")])
async def test_status_shows_plex_online_or_offline(fresh_store, state, word):
    ctx = _ctx(fresh_store, plex=_plex(state))
    text = await status_section(ctx)
    assert f"<b>Plex server:</b> {word}" in text


async def test_failed_offline_dm_is_retried_next_check(fresh_store):
    ctx = _ctx(fresh_store, plex=_plex(False, False, False))
    ctx.bot.send_message.side_effect = [RuntimeError("blocked"), None]
    for _ in range(3):
        await check_alarms(ctx)
    assert ctx.bot.send_message.await_count == 2
