"""A typed /cancel while an /invite, /request or /issue submit is running says
it is already sending (the submit completes), and a typed /cancel after a
Broadcast's Send returned says the broadcast can't be cancelled. Driven
through the real Application like tests/test_flows_across_chats.py."""
from __future__ import annotations

import pytest

from tests.test_flows_across_chats import DM_ID, GROUP_ID, USER_ID, _Rig, rig  # noqa: F401

# (command that opens the flow, the in-flight marker its submit sets)
FLOWS = [
    ("/issue", "submitting_ticket"),
    ("/request movie x", "rq_submitting"),
    ("/invite", "inv_submitting"),
]


def _gen(rig) -> int:
    return rig.user_data.get("flow_gen") or 0


@pytest.mark.parametrize("opener,marker", FLOWS)
async def test_typed_cancel_during_a_submit_says_already_sending(rig, opener, marker):
    await rig.say(DM_ID, opener)
    rig.user_data[marker] = _gen(rig)
    before = len(rig.sent)
    await rig.say(DM_ID, "/cancel")
    texts = [t for _c, t in rig.sent[before:]]
    assert any("Already sending" in t for t in texts), texts
    assert not any(t.startswith("Cancelled") for t in texts)
    # The running submit's marker is left for it to pop.
    assert rig.user_data[marker] == _gen(rig)


@pytest.mark.parametrize("opener,marker", FLOWS)
async def test_a_command_in_a_group_mid_submit_still_says_already_sending(
        rig, opener, marker):
    """A command in another chat is no new flow here: the DM's own submit is
    still the one running."""
    await rig.say(DM_ID, opener)
    rig.user_data[marker] = _gen(rig)
    await rig.say(GROUP_ID, "/weather")
    before = len(rig.sent)
    await rig.say(DM_ID, "/cancel")
    texts = [t for _c, t in rig.sent[before:]]
    assert any("Already sending" in t for t in texts), texts


@pytest.mark.parametrize("opener,marker", FLOWS)
async def test_typed_cancel_without_a_submit_still_cancels(rig, opener, marker):
    await rig.say(DM_ID, opener)
    before = len(rig.sent)
    await rig.say(DM_ID, "/cancel")
    texts = [t for _c, t in rig.sent[before:]]
    assert any(t.startswith("Cancelled") for t in texts), texts
    assert not any("Already sending" in t for t in texts)


@pytest.mark.parametrize("opener,marker", FLOWS)
async def test_an_older_submit_still_running_does_not_block_a_newer_cancel(
        rig, opener, marker):
    await rig.say(DM_ID, opener)
    # A marker from an earlier flow generation: that submit is still finishing
    # while this newer flow is open.
    rig.user_data[marker] = _gen(rig) - 1
    before = len(rig.sent)
    await rig.say(DM_ID, "/cancel")
    texts = [t for _c, t in rig.sent[before:]]
    assert any(t.startswith("Cancelled") for t in texts), texts
    # The older submit's marker is not popped by this cancel.
    assert rig.user_data[marker] == _gen(rig) - 1


async def test_typed_cancel_after_a_broadcast_send_says_it_cannot_be_cancelled(rig):
    rig.app.bot_data["broadcasts_running"] = {7}
    before = len(rig.sent)
    await rig.say(DM_ID, "/cancel")
    texts = [t for _c, t in rig.sent[before:]]
    assert texts == ["The broadcast is sending and can't be cancelled."]


async def test_typed_cancel_with_no_broadcast_running_stays_silent(rig):
    rig.app.bot_data["broadcasts_running"] = set()
    before = len(rig.sent)
    await rig.say(DM_ID, "/cancel")
    assert rig.sent[before:] == []


async def test_a_cancel_a_flow_handles_is_not_also_answered_as_a_broadcast(rig):
    rig.app.bot_data["broadcasts_running"] = {7}
    await rig.say(DM_ID, "/issue")
    before = len(rig.sent)
    await rig.say(DM_ID, "/cancel")
    texts = [t for _c, t in rig.sent[before:]]
    assert any(t.startswith("Cancelled") for t in texts), texts
    assert not any("broadcast" in t for t in texts)
