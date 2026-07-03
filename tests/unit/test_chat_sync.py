"""Tests for ChatSyncer — the cross-machine chat pub/sub core.

Driven with fake peers: each attached peer records the frames sent to it, so we
assert on the wire (chat_subscribe / chat_unsubscribe / chat_event) and on the
subscriber queues without any real WebSocket / event loop plumbing. Mirrors the
style of test_event_syncer.py.
"""
from __future__ import annotations

import asyncio

import pytest

from boxagent.cluster.chat_sync import ChatSyncer


def _make(local: str, route):
    """Return (syncer, sent, attach). `sent[peer]` is the frame list for a peer;
    `attach(peer)` registers a recording send_frame for that peer_key."""
    sent: dict[str, list[dict]] = {}
    syncer = ChatSyncer(local_machine=local, route=route)

    def attach(peer: str):
        async def send(frame):
            sent.setdefault(peer, []).append(frame)
        syncer.attach_peer(peer, send)

    return syncer, sent, attach


def _frames(sent, peer, kind=None):
    got = sent.get(peer, [])
    return [f for f in got if kind is None or f.get("type") == kind]


async def _drain(q: asyncio.Queue) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# ── owner side: a remote peer watches one of MY local bots ──

async def test_owner_forwards_local_publish_to_subscribed_peer():
    syncer, sent, attach = _make("host", route=lambda target: None)
    attach("guestA")
    await syncer.handle_frame("guestA", {
        "type": "chat_subscribe", "target_machine": "host", "bot": "b", "chat_id": "c",
    })

    await syncer.on_local_publish("b", "c", {"type": "message", "text": "hi"})

    events = _frames(sent, "guestA", "chat_event")
    assert len(events) == 1
    assert events[0]["origin_machine"] == "host"
    assert events[0]["bot"] == "b" and events[0]["chat_id"] == "c"
    assert events[0]["event"] == {"type": "message", "text": "hi"}


async def test_owner_publish_to_unwatched_chat_sends_nothing():
    syncer, sent, attach = _make("host", route=lambda target: None)
    attach("guestA")
    await syncer.handle_frame("guestA", {
        "type": "chat_subscribe", "target_machine": "host", "bot": "b", "chat_id": "c",
    })
    await syncer.on_local_publish("b", "OTHER", {"type": "message"})
    assert _frames(sent, "guestA", "chat_event") == []


async def test_owner_unsubscribe_stops_delivery():
    syncer, sent, attach = _make("host", route=lambda target: None)
    attach("guestA")
    await syncer.handle_frame("guestA", {
        "type": "chat_subscribe", "target_machine": "host", "bot": "b", "chat_id": "c",
    })
    await syncer.handle_frame("guestA", {
        "type": "chat_unsubscribe", "target_machine": "host", "bot": "b", "chat_id": "c",
    })
    await syncer.on_local_publish("b", "c", {"type": "message"})
    assert _frames(sent, "guestA", "chat_event") == []


async def test_owner_ignores_subscribe_with_missing_fields():
    syncer, sent, attach = _make("host", route=lambda target: None)
    attach("guestA")
    await syncer.handle_frame("guestA", {
        "type": "chat_subscribe", "target_machine": "host", "bot": "", "chat_id": "c",
    })
    await syncer.on_local_publish("", "c", {"type": "message"})
    assert _frames(sent, "guestA", "chat_event") == []


# ── subscriber side: MY browser watches a remote bot ──

async def test_subscriber_sends_upstream_and_enqueues_events():
    syncer, sent, attach = _make("guestA", route=lambda target: "host")
    attach("host")
    q = await syncer.remote_subscribe("host_m", "b", "c")

    subs = _frames(sent, "host", "chat_subscribe")
    assert subs == [{
        "type": "chat_subscribe", "target_machine": "host_m", "bot": "b", "chat_id": "c",
    }]

    await syncer.handle_frame("host", {
        "type": "chat_event", "origin_machine": "host_m",
        "bot": "b", "chat_id": "c", "event": {"type": "message", "text": "yo"},
    })
    assert await _drain(q) == [{"type": "message", "text": "yo"}]


async def test_subscriber_event_for_other_key_not_delivered():
    syncer, sent, attach = _make("guestA", route=lambda target: "host")
    attach("host")
    q = await syncer.remote_subscribe("host_m", "b", "c")
    await syncer.handle_frame("host", {
        "type": "chat_event", "origin_machine": "host_m",
        "bot": "b", "chat_id": "OTHER", "event": {"type": "message"},
    })
    assert await _drain(q) == []


async def test_subscriber_refcount_single_upstream_sub():
    syncer, sent, attach = _make("guestA", route=lambda target: "host")
    attach("host")
    q1 = await syncer.remote_subscribe("host_m", "b", "c")
    q2 = await syncer.remote_subscribe("host_m", "b", "c")
    # Only ONE upstream chat_subscribe despite two local browsers.
    assert len(_frames(sent, "host", "chat_subscribe")) == 1

    # Both queues receive the event.
    await syncer.handle_frame("host", {
        "type": "chat_event", "origin_machine": "host_m",
        "bot": "b", "chat_id": "c", "event": {"n": 1},
    })
    assert await _drain(q1) == [{"n": 1}]
    assert await _drain(q2) == [{"n": 1}]

    # First unsubscribe: no upstream chat_unsubscribe yet.
    await syncer.remote_unsubscribe("host_m", "b", "c", q1)
    assert _frames(sent, "host", "chat_unsubscribe") == []
    # Last unsubscribe: release upstream.
    await syncer.remote_unsubscribe("host_m", "b", "c", q2)
    assert len(_frames(sent, "host", "chat_unsubscribe")) == 1


async def test_subscriber_reconnect_resends_subscribe():
    syncer, sent, attach = _make("guestA", route=lambda target: "host")
    attach("host")
    await syncer.remote_subscribe("host_m", "b", "c")
    assert len(_frames(sent, "host", "chat_subscribe")) == 1

    # Drop + reattach the host peer (WS reconnect), then resubscribe.
    await syncer.detach_peer("host")
    attach("host")
    await syncer.resubscribe("host")
    assert len(_frames(sent, "host", "chat_subscribe")) == 2


# ── host relay: guest A watches guest B, host in the middle ──

async def test_host_relays_subscribe_and_events_between_guests():
    # On the host, the peer_key for a guest IS its machine id.
    syncer, sent, attach = _make("host", route=lambda target: target)
    attach("gA")
    attach("gB")

    # gA subscribes to gB's bot.
    await syncer.handle_frame("gA", {
        "type": "chat_subscribe", "target_machine": "gB", "bot": "b", "chat_id": "c",
    })
    # Host forwards the subscribe toward gB.
    assert _frames(sent, "gB", "chat_subscribe") == [{
        "type": "chat_subscribe", "target_machine": "gB", "bot": "b", "chat_id": "c",
    }]

    # gB publishes; host relays back to gA.
    await syncer.handle_frame("gB", {
        "type": "chat_event", "origin_machine": "gB",
        "bot": "b", "chat_id": "c", "event": {"type": "message", "text": "relayed"},
    })
    relayed = _frames(sent, "gA", "chat_event")
    assert relayed == [{
        "type": "chat_event", "origin_machine": "gB",
        "bot": "b", "chat_id": "c", "event": {"type": "message", "text": "relayed"},
    }]


async def test_host_relay_refcount_across_two_downstream_guests():
    syncer, sent, attach = _make("host", route=lambda target: target)
    attach("gA")
    attach("gC")
    attach("gB")

    await syncer.handle_frame("gA", {
        "type": "chat_subscribe", "target_machine": "gB", "bot": "b", "chat_id": "c",
    })
    await syncer.handle_frame("gC", {
        "type": "chat_subscribe", "target_machine": "gB", "bot": "b", "chat_id": "c",
    })
    # Single upstream subscribe toward gB despite two downstream guests.
    assert len(_frames(sent, "gB", "chat_subscribe")) == 1

    # gA leaves: still one downstream (gC), no unsubscribe.
    await syncer.handle_frame("gA", {
        "type": "chat_unsubscribe", "target_machine": "gB", "bot": "b", "chat_id": "c",
    })
    assert _frames(sent, "gB", "chat_unsubscribe") == []
    # gC leaves: release upstream.
    await syncer.handle_frame("gC", {
        "type": "chat_unsubscribe", "target_machine": "gB", "bot": "b", "chat_id": "c",
    })
    assert len(_frames(sent, "gB", "chat_unsubscribe")) == 1


async def test_host_relay_detach_releases_upstream():
    syncer, sent, attach = _make("host", route=lambda target: target)
    attach("gA")
    attach("gB")
    await syncer.handle_frame("gA", {
        "type": "chat_subscribe", "target_machine": "gB", "bot": "b", "chat_id": "c",
    })
    assert len(_frames(sent, "gB", "chat_subscribe")) == 1

    # gA's WS drops entirely → host must release the upstream sub toward gB.
    await syncer.detach_peer("gA")
    assert len(_frames(sent, "gB", "chat_unsubscribe")) == 1


# ── misc ──

async def test_handle_frame_returns_false_for_unknown_type():
    syncer, _sent, _attach = _make("host", route=lambda target: None)
    assert await syncer.handle_frame("x", {"type": "event_batch"}) is False
    assert await syncer.handle_frame("x", {"type": "chat_event",
        "origin_machine": "m", "bot": "b", "chat_id": "c", "event": {}}) is True


async def test_subscriber_queue_full_drops_without_crashing():
    syncer, _sent, attach = _make("guestA", route=lambda target: "host")
    attach("host")
    q = await syncer.remote_subscribe("host_m", "b", "c")
    # Overfill past QUEUE_MAXSIZE; excess events are dropped, no exception.
    from boxagent.cluster.chat_sync import QUEUE_MAXSIZE
    for _ in range(QUEUE_MAXSIZE + 5):
        await syncer.handle_frame("host", {
            "type": "chat_event", "origin_machine": "host_m",
            "bot": "b", "chat_id": "c", "event": {"n": 1},
        })
    assert q.qsize() == QUEUE_MAXSIZE


# ── owner-side demand callback (drives the ChatBus pump) ──

async def test_local_demand_fires_on_first_and_last_owner_sub():
    syncer, _sent, attach = _make("host", route=lambda target: None)
    events: list[tuple] = []
    syncer.on_local_demand = lambda bot, chat_id, active: events.append((bot, chat_id, active))
    attach("gA")
    attach("gB")

    # First peer for (b, c): demand goes active.
    await syncer.handle_frame("gA", {
        "type": "chat_subscribe", "target_machine": "host", "bot": "b", "chat_id": "c",
    })
    assert events == [("b", "c", True)]
    # Second peer for the SAME chat: no new demand edge.
    await syncer.handle_frame("gB", {
        "type": "chat_subscribe", "target_machine": "host", "bot": "b", "chat_id": "c",
    })
    assert events == [("b", "c", True)]
    # One leaves: still one watcher, no edge.
    await syncer.handle_frame("gA", {
        "type": "chat_unsubscribe", "target_machine": "host", "bot": "b", "chat_id": "c",
    })
    assert events == [("b", "c", True)]
    # Last leaves: demand goes inactive.
    await syncer.handle_frame("gB", {
        "type": "chat_unsubscribe", "target_machine": "host", "bot": "b", "chat_id": "c",
    })
    assert events == [("b", "c", True), ("b", "c", False)]


async def test_local_demand_deactivates_on_peer_detach():
    syncer, _sent, attach = _make("host", route=lambda target: None)
    events: list[tuple] = []
    syncer.on_local_demand = lambda bot, chat_id, active: events.append((bot, chat_id, active))
    attach("gA")
    await syncer.handle_frame("gA", {
        "type": "chat_subscribe", "target_machine": "host", "bot": "b", "chat_id": "c",
    })
    assert events == [("b", "c", True)]
    # gA's WS drops → its owner-side subscription is the last one → demand off.
    await syncer.detach_peer("gA")
    assert events == [("b", "c", True), ("b", "c", False)]
