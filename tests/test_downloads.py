"""The Admin Hub's Downloads view and the SABnzbd client calls behind it."""
from __future__ import annotations

import httpx

from bot.admin_hub import render_admin_hub
from bot.downloads import NO_MAX_MESSAGE, downloads
from sabnzbd import SabnzbdClient

from tests._handler_harness import make_ctx, make_update

ADMIN = 999


def _client(handler) -> SabnzbdClient:
    c = SabnzbdClient("http://sab.test:8080", "key123")
    c._client = httpx.AsyncClient(base_url="http://sab.test:8080",
                                  transport=httpx.MockTransport(handler))
    return c


def _recording_client(queue=None):
    calls = []
    queue = queue or {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        if request.url.params["mode"] == "queue":
            return httpx.Response(200, json={"queue": queue})
        return httpx.Response(200, json={"status": True})

    return _client(handler), calls


def _ctx(sab):
    return make_ctx(admin_id=ADMIN, bot_data_overrides={"sabnzbd": sab})


def _buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard
            for b in row]


async def _tap(sab, data, user_id=ADMIN):
    ctx = _ctx(sab)
    upd = make_update(callback_data=data, user_id=user_id)
    await downloads(upd, ctx)
    return upd


# --- client ----------------------------------------------------------------

async def test_queue_state_reads_the_global_fields():
    c, calls = _recording_client({
        "paused": True, "speed": "1.5 M", "speedlimit": "50",
        "speedlimit_abs": "5242880", "mbleft": "2048.0",
        "timeleft": "0:10:00", "pause_int": "0:29:00"})
    st = await c.get_queue_state()
    assert calls[0]["mode"] == "queue"
    assert st == {"paused": True, "speed": "1.5 M", "speedlimit": "50",
                  "speedlimit_abs": "5242880", "mbleft": "2048.0",
                  "timeleft": "0:10:00", "pause_int": "0:29:00"}


async def test_client_pause_resume_pause_for_and_limit_calls():
    c, calls = _recording_client()
    await c.pause()
    await c.resume()
    await c.pause_for(30)
    await c.set_speed_limit(50)
    await c.set_speed_limit(0)
    assert calls[0]["mode"] == "pause"
    assert calls[1]["mode"] == "resume"
    assert (calls[2]["mode"], calls[2]["name"], calls[2]["value"]) == \
        ("config", "set_pause", "30")
    assert (calls[3]["mode"], calls[3]["name"], calls[3]["value"]) == \
        ("config", "speedlimit", "50")
    assert calls[4]["value"] == "0"


# --- view ------------------------------------------------------------------

async def test_open_shows_state_and_all_buttons():
    sab, calls = _recording_client({
        "paused": False, "speed": "2.0 M", "speedlimit": "50",
        "speedlimit_abs": "5242880", "mbleft": "1024", "timeleft": "0:05:00"})
    upd = await _tap(sab, "dlv")
    edit = upd.callback_query.edits[-1]
    assert "Running" in edit["text"] and "2.0 M" in edit["text"]
    assert "5.0 MB/s" in edit["text"] and "1.0 GB" in edit["text"]
    assert [c["mode"] for c in calls] == ["queue"]
    assert _buttons(edit["reply_markup"]) == [
        ("⏸ Pause", "dlp"), ("⏱ Pause 30 minutes", "dlt"),
        ("25%", "dll:25"), ("50%", "dll:50"), ("75%", "dll:75"),
        ("No limit", "dll:0"), ("⬅️ Admin", "hubo:admin")]


async def test_paused_state_offers_resume():
    sab, _ = _recording_client({"paused": True, "pause_int": "0:12:00"})
    upd = await _tap(sab, "dlv")
    edit = upd.callback_query.edits[-1]
    assert "Paused" in edit["text"] and "0:12:00" in edit["text"]
    assert ("▶️ Resume", "dlr") in _buttons(edit["reply_markup"])


async def test_each_button_sends_its_call_then_rerenders():
    expected = {"dlp": ("pause", None, None),
                "dlt": ("config", "set_pause", "30"),
                "dlr": ("resume", None, None),
                "dll:75": ("config", "speedlimit", "75"),
                "dll:0": ("config", "speedlimit", "0")}
    for data, (mode, name, value) in expected.items():
        sab, calls = _recording_client({"speedlimit_abs": "1048576"})
        upd = await _tap(sab, data)
        assert calls[0]["mode"] == mode
        assert calls[0].get("name") == name
        assert calls[0].get("value") == value
        assert calls[-1]["mode"] == "queue"
        assert "<b>Downloads</b>" in upd.callback_query.edits[-1]["text"]


async def test_percent_without_maximum_shows_message():
    sab, _ = _recording_client({"speedlimit": "", "speedlimit_abs": ""})
    upd = await _tap(sab, "dll:50")
    assert NO_MAX_MESSAGE in upd.callback_query.edits[-1]["text"]


async def test_percent_with_maximum_has_no_message():
    sab, _ = _recording_client({"speedlimit": "50",
                                "speedlimit_abs": "5242880"})
    upd = await _tap(sab, "dll:50")
    assert "maximum" not in upd.callback_query.edits[-1]["text"]


async def test_no_limit_without_maximum_has_no_message():
    sab, _ = _recording_client({"speedlimit_abs": ""})
    upd = await _tap(sab, "dll:0")
    assert "maximum" not in upd.callback_query.edits[-1]["text"]


async def test_sabnzbd_failure_offers_try_again_and_back():
    def handler(request):
        return httpx.Response(200, json={"status": False, "error": "nope"})
    upd = await _tap(_client(handler), "dlp")
    edit = upd.callback_query.edits[-1]
    assert "Couldn't reach SABnzbd" in edit["text"]
    assert _buttons(edit["reply_markup"]) == [("🔁 Try again", "dlp"),
                                              ("⬅️ Admin", "hubo:admin")]


async def test_non_admin_is_refused_without_a_call():
    sab, calls = _recording_client()
    upd = await _tap(sab, "dlp", user_id=42)
    assert calls == []
    assert upd.callback_query.answers[-1][0] == "Admin only."


# --- hub button ------------------------------------------------------------

async def test_hub_shows_downloads_only_with_sabnzbd():
    sab, _ = _recording_client()
    with_sab = await render_admin_hub(None, _ctx(sab))
    without = await render_admin_hub(None, _ctx(None))
    assert ("⏯ Downloads", "dlv") in [(b.text, b.callback_data)
                                      for b in with_sab.buttons]
    assert all(b.callback_data != "dlv" for b in without.buttons)


async def test_admin_in_a_group_is_told_to_dm():
    sab, calls = _recording_client()
    ctx = _ctx(sab)
    upd = make_update(callback_data="dlp", user_id=ADMIN)
    upd.effective_chat.type = "group"
    await downloads(upd, ctx)
    assert calls == []
    assert upd.callback_query.answers[-1][0] == "DM me /admin"


async def test_tap_without_sabnzbd_says_not_linked():
    upd = await _tap(None, "dlv")
    edit = upd.callback_query.edits[-1]
    assert "isn't linked" in edit["text"]
    assert _buttons(edit["reply_markup"]) == [("⬅️ Admin", "hubo:admin")]
