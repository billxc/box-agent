"""Tests for ChatBus — the location-transparent chat subscription façade.

Drives ChatBus with a fake WebChannel (real asyncio.Queue fan-out) + a real
ChatSyncer whose single peer records frames. Covers: local vs remote subscribe
dispatch, and the owner-side pump that forwards a local bot's events to a remote
subscriber in order.
"""
from __future__ import annotations

import asyncio

from boxagent.cluster.chat_bus import ChatBus
from boxagent.cluster.chat_sync import ChatSyncer


class FakeChannel:
    """Minimal WebChannel stand-in: subscribe/unsubscribe + publish."""

    def __init__(self) -> None:
        self.subs: dict[str, list[asyncio.Queue]] = {}
        self.unsubscribed: list[tuple[str, asyncio.Queue]] = []

    def subscribe(self, chat_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self.subs.setdefault(chat_id, []).append(q)
        return q

    def unsubscribe(self, chat_id: str, q: asyncio.Queue) -> None:
        self.unsubscribed.append((chat_id, q))
        queues = self.subs.get(chat_id)
        if queues and q in queues:
            queues.remove(q)

    def publish(self, chat_id: str, event: dict) -> None:
        for q in self.subs.get(chat_id, []):
            q.put_nowait(event)


def _make_bus(local: str, route, channels: dict):
    syncer = ChatSyncer(local_machine=local, route=route)
    sent: dict[str, list[dict]] = {}

    def attach(peer: str):
        async def send(frame):
            sent.setdefault(peer, []).append(frame)
        syncer.attach_peer(peer, send)

    bus = ChatBus(local_machine=local, syncer=syncer, channel_for=channels.get)
    return bus, syncer, sent, attach


# ── subscribe dispatch ──

async def test_subscribe_local_returns_channel_queue():
    channel = FakeChannel()
    bus, _syncer, _sent, _attach = _make_bus("host", lambda t: None, {"b": channel})
    q = await bus.subscribe("b", "c", "host")
    # It's the channel's own queue: a channel publish lands on it.
    channel.publish("c", {"type": "message", "text": "hi"})
    assert q is channel.subs["c"][0]
    assert q.get_nowait() == {"type": "message", "text": "hi"}


async def test_subscribe_local_unknown_bot_returns_none():
    bus, _syncer, _sent, _attach = _make_bus("host", lambda t: None, {})
    assert await bus.subscribe("missing", "c", "host") is None


async def test_subscribe_remote_goes_through_syncer():
    bus, _syncer, sent, attach = _make_bus("guestA", lambda t: "host", {})
    attach("host")
    q = await bus.subscribe("b", "c", "host_m")
    assert isinstance(q, asyncio.Queue)
    # Upstream chat_subscribe was sent toward the host.
    assert sent["host"] == [{
        "type": "chat_subscribe", "target_machine": "host_m", "bot": "b", "chat_id": "c",
    }]


async def test_unsubscribe_local_releases_channel():
    channel = FakeChannel()
    bus, _syncer, _sent, _attach = _make_bus("host", lambda t: None, {"b": channel})
    q = await bus.subscribe("b", "c", "host")
    await bus.unsubscribe("b", "c", "host", q)
    assert channel.unsubscribed == [("c", q)]


# ── owner-side pump ──

async def test_owner_pump_forwards_local_events_in_order():
    channel = FakeChannel()
    bus, syncer, sent, attach = _make_bus("host", lambda t: None, {"b": channel})
    attach("gA")

    # A remote peer subscribes to our local bot → demand activates the pump.
    await syncer.handle_frame("gA", {
        "type": "chat_subscribe", "target_machine": "host", "bot": "b", "chat_id": "c",
    })
    await asyncio.sleep(0.02)  # let the pump task subscribe to the channel
    assert "c" in channel.subs  # pump subscribed

    # Local bot emits two events; the pump forwards both, in order.
    channel.publish("c", {"type": "message", "n": 1})
    channel.publish("c", {"type": "message", "n": 2})
    await asyncio.sleep(0.02)

    events = [f for f in sent.get("gA", []) if f["type"] == "chat_event"]
    assert [f["event"]["n"] for f in events] == [1, 2]
    assert all(f["origin_machine"] == "host" for f in events)


async def test_owner_pump_stops_and_unsubscribes_on_last_leave():
    channel = FakeChannel()
    bus, syncer, _sent, attach = _make_bus("host", lambda t: None, {"b": channel})
    attach("gA")
    await syncer.handle_frame("gA", {
        "type": "chat_subscribe", "target_machine": "host", "bot": "b", "chat_id": "c",
    })
    await asyncio.sleep(0.02)
    q = channel.subs["c"][0]

    await syncer.handle_frame("gA", {
        "type": "chat_unsubscribe", "target_machine": "host", "bot": "b", "chat_id": "c",
    })
    await asyncio.sleep(0.02)
    assert (("c", q)) in channel.unsubscribed  # pump released its subscription


async def test_aclose_cancels_pumps():
    channel = FakeChannel()
    bus, syncer, _sent, attach = _make_bus("host", lambda t: None, {"b": channel})
    attach("gA")
    await syncer.handle_frame("gA", {
        "type": "chat_subscribe", "target_machine": "host", "bot": "b", "chat_id": "c",
    })
    await asyncio.sleep(0.02)
    q = channel.subs["c"][0]
    await bus.aclose()
    await asyncio.sleep(0.02)
    assert (("c", q)) in channel.unsubscribed
    assert not bus._pumps
