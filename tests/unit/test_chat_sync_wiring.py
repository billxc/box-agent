"""Tests for chat_sync_wiring — chaining chat hooks onto registry/guest_client.

The critical property: the EventSyncer already owns on_unknown_frame /
on_guest_attached / on_guest_detached, so the chat installers must CHAIN, not
overwrite. These tests install a prior (event-syncer-like) handler first, then
the chat hooks, and assert both survive.
"""
from __future__ import annotations

import asyncio

from boxagent.cluster.chat_sync import ChatSyncer
from boxagent.cluster.chat_sync_wiring import (
    install_guest_client_hooks,
    install_registry_hooks,
)


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, frame: dict) -> None:
        self.sent.append(frame)


class FakeSession:
    def __init__(self, ws: FakeWS) -> None:
        self.ws = ws


class FakeRegistry:
    def __init__(self) -> None:
        self.on_guest_attached = None
        self.on_guest_detached = None
        self.on_unknown_frame = None


class FakeGuestClient:
    def __init__(self) -> None:
        self._ws = FakeWS()
        self.on_connect = None
        self.on_disconnect = None
        self.on_unknown_frame = None


# ── registry: unknown-frame chaining ──

async def test_registry_unknown_frame_chains_event_and_chat():
    registry = FakeRegistry()
    handled_by_prior: list[dict] = []

    async def prior_unknown(machine_id, payload):
        if payload.get("type") == "event_batch":
            handled_by_prior.append(payload)
            return True
        return False

    registry.on_unknown_frame = prior_unknown  # event syncer's handler
    syncer = ChatSyncer(local_machine="host", route=lambda target: None)
    install_registry_hooks(syncer, registry)

    # Event frame still reaches the prior handler.
    assert await registry.on_unknown_frame("gA", {"type": "event_batch"}) is True
    assert handled_by_prior

    # Chat frame is consumed by the chat syncer.
    assert await registry.on_unknown_frame("gA", {
        "type": "chat_event", "origin_machine": "m", "bot": "b", "chat_id": "c", "event": {},
    }) is True

    # Truly-unknown frame is handled by neither.
    assert await registry.on_unknown_frame("gA", {"type": "garbage"}) is False


async def test_registry_attach_registers_chat_peer_and_chains_prior():
    registry = FakeRegistry()
    prior_calls: list[str] = []
    registry.on_guest_attached = lambda machine_id, session: prior_calls.append(machine_id)
    syncer = ChatSyncer(local_machine="host", route=lambda target: None)
    install_registry_hooks(syncer, registry)

    ws = FakeWS()
    registry.on_guest_attached("gA", FakeSession(ws))
    await asyncio.sleep(0)  # let the resubscribe task run
    assert prior_calls == ["gA"]  # prior hook still fired

    # The chat peer is registered: a subscribe to our local bot + publish
    # reaches gA's WS as a chat_event.
    await registry.on_unknown_frame("gA", {
        "type": "chat_subscribe", "target_machine": "host", "bot": "b", "chat_id": "c",
    })
    await syncer.on_local_publish("b", "c", {"type": "message", "x": 1})
    assert any(f["type"] == "chat_event" for f in ws.sent)


async def test_registry_detach_removes_chat_peer_and_chains_prior():
    registry = FakeRegistry()
    prior_calls: list[str] = []
    registry.on_guest_detached = lambda machine_id: prior_calls.append(machine_id)
    syncer = ChatSyncer(local_machine="host", route=lambda target: None)
    install_registry_hooks(syncer, registry)

    ws = FakeWS()
    registry.on_guest_attached("gA", FakeSession(ws))
    await asyncio.sleep(0)
    await registry.on_unknown_frame("gA", {
        "type": "chat_subscribe", "target_machine": "host", "bot": "b", "chat_id": "c",
    })

    registry.on_guest_detached("gA")
    await asyncio.sleep(0)  # let detach_peer task run
    assert prior_calls == ["gA"]

    # After detach, publishing no longer reaches gA.
    ws.sent.clear()
    await syncer.on_local_publish("b", "c", {"type": "message"})
    assert ws.sent == []


# ── guest_client: peer key "host" ──

async def test_guest_client_unknown_frame_chains_and_connects_peer():
    client = FakeGuestClient()
    prior_unknown_seen: list[dict] = []

    async def prior_unknown(payload):
        if payload.get("type") == "event_resync":
            prior_unknown_seen.append(payload)
            return True
        return False

    client.on_unknown_frame = prior_unknown
    prior_connect: list[object] = []
    client.on_connect = lambda c: prior_connect.append(c)

    syncer = ChatSyncer(local_machine="guestA", route=lambda target: "host")
    install_guest_client_hooks(syncer, client)

    client.on_connect(client)
    await asyncio.sleep(0)
    assert prior_connect == [client]  # prior connect hook chained

    # Prior event handler survives.
    assert await client.on_unknown_frame({"type": "event_resync"}) is True
    assert prior_unknown_seen
    # Chat frame consumed.
    assert await client.on_unknown_frame({
        "type": "chat_event", "origin_machine": "m", "bot": "b", "chat_id": "c", "event": {},
    }) is True
    # Unknown → False.
    assert await client.on_unknown_frame({"type": "nope"}) is False
