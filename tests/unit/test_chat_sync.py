"""Tests for ChatSyncer — the cross-machine chat bridge on the shared MessageBus.

ChatSyncer is now a bus citizen: owner-side chat is published onto
``chat.<machine>.<bot>.<chat_id>`` by the local WebChannel (here simulated with
``bus.publish``), and a local browser subscribes with ``bus.subscribe`` on the
same topic (here a QueueSubscriber). The syncer bridges that bus to peers:

- outbound: forwards local publishes for a key to the peers subscribed to it
- demand:   a local subscription to a REMOTE-owned topic → upstream chat_subscribe
- inbound:  a chat_event frame is re-published onto the local bus

Outbound peer frames ride an ordered async drain (sync bus → async WS), so a test
that asserts on sent frames awaits ``_settle`` first. Peers are fake: each records
the frames sent to it. Mirrors the black-box style of test_event_syncer.py.
"""
from __future__ import annotations

import asyncio

from boxagent.bus.core import MessageBus
from boxagent.bus.subscriber import QueueSubscriber
from boxagent.cluster.chat_sync import QUEUE_MAXSIZE, ChatSyncer


def _make(local: str, route):
    """Return (bus, syncer, sent, attach). `sent[peer]` is the frame list for a
    peer; `attach(peer)` registers a recording send_frame for that peer_key."""
    bus = MessageBus()
    sent: dict[str, list[dict]] = {}
    syncer = ChatSyncer(local_machine=local, route=route, message_bus=bus)

    def attach(peer: str):
        async def send(frame):
            sent.setdefault(peer, []).append(frame)
        syncer.attach_peer(peer, send)

    return bus, syncer, sent, attach


def _topic(machine: str, bot: str, chat_id: str) -> str:
    return f"chat.{machine}.{bot}.{chat_id}"


def _watch(bus: MessageBus, machine: str, bot: str, chat_id: str):
    """Simulate a local browser subscribing to a chat topic; return (queue, sub)."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
    subscription = bus.subscribe(_topic(machine, bot, chat_id), QueueSubscriber(queue))
    return queue, subscription


async def _settle(syncer: ChatSyncer) -> None:
    """Let the ordered async send drain flush its queued peer frames."""
    for _ in range(100):
        await asyncio.sleep(0)
        queue = syncer._sendq
        if queue is None or queue.empty():
            await asyncio.sleep(0)
            if queue is None or queue.empty():
                return


def _frames(sent, peer, kind=None):
    got = sent.get(peer, [])
    return [f for f in got if kind is None or f.get("type") == kind]


def _drain(q: asyncio.Queue) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# ── owner side: a remote peer watches one of MY local bots ──

async def test_owner_forwards_local_publish_to_subscribed_peer():
    bus, syncer, sent, attach = _make("host", route=lambda target: None)
    attach("guestA")
    await syncer.handle_frame("guestA", {
        "type": "chat_subscribe", "target_machine": "host", "bot": "b", "chat_id": "c",
    })

    bus.publish(_topic("host", "b", "c"), {"type": "message", "text": "hi"}, 0.0)
    await _settle(syncer)

    events = _frames(sent, "guestA", "chat_event")
    assert len(events) == 1
    assert events[0]["origin_machine"] == "host"
    assert events[0]["bot"] == "b" and events[0]["chat_id"] == "c"
    assert events[0]["event"] == {"type": "message", "text": "hi"}


async def test_owner_publish_to_unwatched_chat_sends_nothing():
    bus, syncer, sent, attach = _make("host", route=lambda target: None)
    attach("guestA")
    await syncer.handle_frame("guestA", {
        "type": "chat_subscribe", "target_machine": "host", "bot": "b", "chat_id": "c",
    })
    bus.publish(_topic("host", "b", "OTHER"), {"type": "message"}, 0.0)
    await _settle(syncer)
    assert _frames(sent, "guestA", "chat_event") == []


async def test_owner_unsubscribe_stops_delivery():
    bus, syncer, sent, attach = _make("host", route=lambda target: None)
    attach("guestA")
    await syncer.handle_frame("guestA", {
        "type": "chat_subscribe", "target_machine": "host", "bot": "b", "chat_id": "c",
    })
    await syncer.handle_frame("guestA", {
        "type": "chat_unsubscribe", "target_machine": "host", "bot": "b", "chat_id": "c",
    })
    bus.publish(_topic("host", "b", "c"), {"type": "message"}, 0.0)
    await _settle(syncer)
    assert _frames(sent, "guestA", "chat_event") == []


async def test_owner_ignores_subscribe_with_missing_fields():
    bus, syncer, sent, attach = _make("host", route=lambda target: None)
    attach("guestA")
    await syncer.handle_frame("guestA", {
        "type": "chat_subscribe", "target_machine": "host", "bot": "", "chat_id": "c",
    })
    bus.publish(_topic("host", "", "c"), {"type": "message"}, 0.0)
    await _settle(syncer)
    assert _frames(sent, "guestA", "chat_event") == []


# ── subscriber side: MY browser watches a remote bot ──

async def test_subscriber_sends_upstream_and_enqueues_events():
    bus, syncer, sent, attach = _make("guestA", route=lambda target: "host")
    attach("host")
    queue, _sub = _watch(bus, "host_m", "b", "c")
    await _settle(syncer)

    subs = _frames(sent, "host", "chat_subscribe")
    assert subs == [{
        "type": "chat_subscribe", "target_machine": "host_m", "bot": "b", "chat_id": "c", "v": 2,
    }]

    await syncer.handle_frame("host", {
        "type": "chat_event", "origin_machine": "host_m",
        "bot": "b", "chat_id": "c", "event": {"type": "message", "text": "yo"},
    })
    assert _drain(queue) == [{"type": "message", "text": "yo"}]


async def test_subscriber_event_for_other_key_not_delivered():
    bus, syncer, sent, attach = _make("guestA", route=lambda target: "host")
    attach("host")
    queue, _sub = _watch(bus, "host_m", "b", "c")
    await syncer.handle_frame("host", {
        "type": "chat_event", "origin_machine": "host_m",
        "bot": "b", "chat_id": "OTHER", "event": {"type": "message"},
    })
    assert _drain(queue) == []


async def test_subscriber_refcount_single_upstream_sub():
    bus, syncer, sent, attach = _make("guestA", route=lambda target: "host")
    attach("host")
    queue1, sub1 = _watch(bus, "host_m", "b", "c")
    queue2, sub2 = _watch(bus, "host_m", "b", "c")
    await _settle(syncer)
    # Only ONE upstream chat_subscribe despite two local browsers.
    assert len(_frames(sent, "host", "chat_subscribe")) == 1

    # Both queues receive the event.
    await syncer.handle_frame("host", {
        "type": "chat_event", "origin_machine": "host_m",
        "bot": "b", "chat_id": "c", "event": {"n": 1},
    })
    assert _drain(queue1) == [{"n": 1}]
    assert _drain(queue2) == [{"n": 1}]

    # First unsubscribe: no upstream chat_unsubscribe yet.
    sub1.close()
    await _settle(syncer)
    assert _frames(sent, "host", "chat_unsubscribe") == []
    # Last unsubscribe: release upstream.
    sub2.close()
    await _settle(syncer)
    assert len(_frames(sent, "host", "chat_unsubscribe")) == 1


async def test_subscriber_reconnect_resends_subscribe():
    bus, syncer, sent, attach = _make("guestA", route=lambda target: "host")
    attach("host")
    _watch(bus, "host_m", "b", "c")
    await _settle(syncer)
    assert len(_frames(sent, "host", "chat_subscribe")) == 1

    # Drop + reattach the host peer (WS reconnect), then resubscribe.
    await syncer.detach_peer("host")
    attach("host")
    await syncer.resubscribe("host")
    await _settle(syncer)
    assert len(_frames(sent, "host", "chat_subscribe")) == 2


# ── host relay: guest A watches guest B, host in the middle ──

async def test_host_relays_subscribe_and_events_between_guests():
    # On the host, the peer_key for a guest IS its machine id.
    bus, syncer, sent, attach = _make("host", route=lambda target: target)
    attach("gA")
    attach("gB")

    # gA subscribes to gB's bot.
    await syncer.handle_frame("gA", {
        "type": "chat_subscribe", "target_machine": "gB", "bot": "b", "chat_id": "c",
    })
    await _settle(syncer)
    # Host forwards the subscribe toward gB.
    assert _frames(sent, "gB", "chat_subscribe") == [{
        "type": "chat_subscribe", "target_machine": "gB", "bot": "b", "chat_id": "c", "v": 2,
    }]

    # gB publishes; host relays back to gA.
    await syncer.handle_frame("gB", {
        "type": "chat_event", "origin_machine": "gB",
        "bot": "b", "chat_id": "c", "event": {"type": "message", "text": "relayed"},
    })
    await _settle(syncer)
    relayed = _frames(sent, "gA", "chat_event")
    assert relayed == [{
        "type": "chat_event", "origin_machine": "gB",
        "bot": "b", "chat_id": "c", "event": {"type": "message", "text": "relayed"}, "v": 2,
    }]


async def test_host_relay_refcount_across_two_downstream_guests():
    bus, syncer, sent, attach = _make("host", route=lambda target: target)
    attach("gA")
    attach("gC")
    attach("gB")

    await syncer.handle_frame("gA", {
        "type": "chat_subscribe", "target_machine": "gB", "bot": "b", "chat_id": "c",
    })
    await syncer.handle_frame("gC", {
        "type": "chat_subscribe", "target_machine": "gB", "bot": "b", "chat_id": "c",
    })
    await _settle(syncer)
    # Single upstream subscribe toward gB despite two downstream guests.
    assert len(_frames(sent, "gB", "chat_subscribe")) == 1

    # gA leaves: still one downstream (gC), no unsubscribe.
    await syncer.handle_frame("gA", {
        "type": "chat_unsubscribe", "target_machine": "gB", "bot": "b", "chat_id": "c",
    })
    await _settle(syncer)
    assert _frames(sent, "gB", "chat_unsubscribe") == []
    # gC leaves: release upstream.
    await syncer.handle_frame("gC", {
        "type": "chat_unsubscribe", "target_machine": "gB", "bot": "b", "chat_id": "c",
    })
    await _settle(syncer)
    assert len(_frames(sent, "gB", "chat_unsubscribe")) == 1


async def test_host_relay_detach_releases_upstream():
    bus, syncer, sent, attach = _make("host", route=lambda target: target)
    attach("gA")
    attach("gB")
    await syncer.handle_frame("gA", {
        "type": "chat_subscribe", "target_machine": "gB", "bot": "b", "chat_id": "c",
    })
    await _settle(syncer)
    assert len(_frames(sent, "gB", "chat_subscribe")) == 1

    # gA's WS drops entirely → host must release the upstream sub toward gB.
    await syncer.detach_peer("gA")
    await _settle(syncer)
    assert len(_frames(sent, "gB", "chat_unsubscribe")) == 1


# ── misc ──

async def test_handle_frame_returns_false_for_unknown_type():
    _bus, syncer, _sent, _attach = _make("host", route=lambda target: None)
    assert await syncer.handle_frame("x", {"type": "event_batch"}) is False
    assert await syncer.handle_frame("x", {"type": "chat_event",
        "origin_machine": "m", "bot": "b", "chat_id": "c", "event": {}}) is True


async def test_subscriber_queue_full_drops_without_crashing():
    bus, syncer, _sent, attach = _make("guestA", route=lambda target: "host")
    attach("host")
    queue, _sub = _watch(bus, "host_m", "b", "c")
    # Overfill past QUEUE_MAXSIZE; excess events are dropped, no exception.
    for _ in range(QUEUE_MAXSIZE + 5):
        await syncer.handle_frame("host", {
            "type": "chat_event", "origin_machine": "host_m",
            "bot": "b", "chat_id": "c", "event": {"n": 1},
        })
    assert queue.qsize() == QUEUE_MAXSIZE


# ── demand edges driven by local bus subscriptions ──

async def test_local_demand_first_and_last_sub_toggle_upstream():
    bus, syncer, sent, attach = _make("guestA", route=lambda target: "host")
    attach("host")

    # First local browser for (host_m, b, c): upstream chat_subscribe once.
    _q1, sub1 = _watch(bus, "host_m", "b", "c")
    _q2, sub2 = _watch(bus, "host_m", "b", "c")
    await _settle(syncer)
    assert len(_frames(sent, "host", "chat_subscribe")) == 1

    # One leaves: still one watcher, no unsubscribe.
    sub1.close()
    await _settle(syncer)
    assert _frames(sent, "host", "chat_unsubscribe") == []
    # Last leaves: upstream released.
    sub2.close()
    await _settle(syncer)
    assert len(_frames(sent, "host", "chat_unsubscribe")) == 1


async def test_local_subscription_to_own_machine_sends_no_upstream():
    # A browser watching a LOCAL bot must NOT emit any upstream frame — the
    # owner's WebChannel publishes to the bus directly.
    bus, syncer, sent, attach = _make("host", route=lambda target: "host")
    attach("host")
    _watch(bus, "host", "b", "c")  # owner == self
    await _settle(syncer)
    assert sent.get("host", []) == []
