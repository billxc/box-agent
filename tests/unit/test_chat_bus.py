"""Tests for ChatBus — the location-transparent chat subscription façade.

ChatBus does ONE thing now: ``bus.subscribe(chat.<owner>.<bot>.<chat_id>)`` for
both local and remote owners. For a local owner the WebChannel publishes onto
that topic (here simulated with ``bus.publish``); for a remote owner the
ChatSyncer bridge (on the same bus) turns the subscription into an upstream
chat_subscribe and re-publishes inbound frames. No local/remote fork, no pump.
"""
from __future__ import annotations

import asyncio

from boxagent.bus.core import MessageBus
from boxagent.cluster.chat_bus import ChatBus
from boxagent.cluster.chat_sync import ChatSyncer


def _make(local: str, route=lambda target: None, bots=("b",)):
    bus = MessageBus()
    syncer = ChatSyncer(local_machine=local, route=route, message_bus=bus)
    sent: dict[str, list[dict]] = {}

    def attach(peer: str):
        async def send(frame):
            sent.setdefault(peer, []).append(frame)
        syncer.attach_peer(peer, send)

    channels = {name: object() for name in bots}  # truthy = web-enabled bot
    chat_bus = ChatBus(local_machine=local, message_bus=bus, channel_for=channels.get)
    return bus, syncer, chat_bus, sent, attach


async def _settle(syncer: ChatSyncer) -> None:
    for _ in range(100):
        await asyncio.sleep(0)
        queue = syncer._sendq
        if queue is None or queue.empty():
            await asyncio.sleep(0)
            if queue is None or queue.empty():
                return


def _frames(sent, peer, kind):
    return [f for f in sent.get(peer, []) if f.get("type") == kind]


# ── subscribe path (local & remote are the same bus.subscribe) ──

async def test_subscribe_local_receives_channel_publish():
    bus, _syncer, chat_bus, _sent, _attach = _make("host")
    queue = await chat_bus.subscribe("b", "c", "host")
    # The local WebChannel would publish onto this exact topic.
    bus.publish("chat.host.b.c", {"type": "message", "text": "hi"}, 0.0)
    assert queue.get_nowait() == {"type": "message", "text": "hi"}


async def test_subscribe_local_unknown_bot_returns_none():
    _bus, _syncer, chat_bus, _sent, _attach = _make("host", bots=())
    assert await chat_bus.subscribe("missing", "c", "host") is None


async def test_subscribe_remote_drives_upstream_subscribe():
    bus, syncer, chat_bus, sent, attach = _make("guestA", route=lambda target: "host")
    attach("host")
    queue = await chat_bus.subscribe("b", "c", "host_m")
    await _settle(syncer)
    assert isinstance(queue, asyncio.Queue)
    assert _frames(sent, "host", "chat_subscribe") == [{
        "type": "chat_subscribe", "target_machine": "host_m", "bot": "b", "chat_id": "c", "v": 2,
    }]


async def test_subscribe_remote_delivers_reinjected_event():
    bus, syncer, chat_bus, _sent, attach = _make("guestA", route=lambda target: "host")
    attach("host")
    queue = await chat_bus.subscribe("b", "c", "host_m")
    # Inbound chat_event from the host → syncer re-publishes onto the topic.
    await syncer.handle_frame("host", {
        "type": "chat_event", "origin_machine": "host_m",
        "bot": "b", "chat_id": "c", "event": {"type": "message", "text": "yo"},
    })
    assert queue.get_nowait() == {"type": "message", "text": "yo"}


# ── unsubscribe ──

async def test_unsubscribe_remote_releases_upstream():
    bus, syncer, chat_bus, sent, attach = _make("guestA", route=lambda target: "host")
    attach("host")
    queue = await chat_bus.subscribe("b", "c", "host_m")
    await _settle(syncer)
    assert len(_frames(sent, "host", "chat_subscribe")) == 1

    await chat_bus.unsubscribe("b", "c", "host_m", queue)
    await _settle(syncer)
    assert len(_frames(sent, "host", "chat_unsubscribe")) == 1


async def test_unsubscribe_local_sends_no_frame():
    bus, syncer, chat_bus, sent, _attach = _make("host")
    queue = await chat_bus.subscribe("b", "c", "host")
    await chat_bus.unsubscribe("b", "c", "host", queue)
    await _settle(syncer)
    assert sent == {}


# ── shutdown ──

async def test_aclose_signals_close_and_detaches():
    bus, _syncer, chat_bus, _sent, _attach = _make("host")
    queue = await chat_bus.subscribe("b", "c", "host")
    await chat_bus.aclose()
    assert queue.get_nowait() == {"type": "_close"}
    # Subscription closed: a later publish no longer reaches the queue.
    bus.publish("chat.host.b.c", {"x": 1}, 0.0)
    assert queue.empty()
