"""Tests for bus_wiring — the wiring that bridges registry/guest_client → EventSyncer.

Chat now rides the ClusterBus packet path; this wiring is events-only. Covers:
event_* frames reach the syncer, attach/detach per peer, and the wire-version
gate drops mismatched frames.
"""
from __future__ import annotations

import asyncio

from boxagent.cluster.bus_wiring import (
    install_guest_client_hooks,
    install_registry_hooks,
)


class FakeEventSyncer:
    """Matches EventSyncer's shape: sync attach/detach, async handle_frame."""

    def __init__(self) -> None:
        self.attached: list[str] = []
        self.detached: list[str] = []
        self.handled: list[tuple[str, dict]] = []

    def attach_peer(self, peer_key, send_frame) -> None:
        self.attached.append(peer_key)

    def detach_peer(self, peer_key) -> None:
        self.detached.append(peer_key)

    async def handle_frame(self, peer_key, payload) -> bool:
        if str(payload.get("type", "")).startswith("event_"):
            self.handled.append((peer_key, payload))
            return True
        return False


class FakeWS:
    def __init__(self) -> None:
        self.closed = False

    async def send_json(self, frame) -> None:
        pass


class FakeSession:
    def __init__(self) -> None:
        self.ws = FakeWS()


class FakeRegistry:
    on_guest_attached = None
    on_guest_detached = None
    on_unknown_frame = None


class FakeGuestClient:
    def __init__(self) -> None:
        self._ws = FakeWS()
        self.on_connect = None
        self.on_disconnect = None
        self.on_unknown_frame = None


# ── registry (host) side ──

async def test_registry_attaches_event_syncer_on_guest():
    event_syncer = FakeEventSyncer()
    registry = FakeRegistry()
    install_registry_hooks(event_syncer, registry)

    registry.on_guest_attached("guestA", FakeSession())
    assert event_syncer.attached == ["guestA"]


async def test_registry_detaches_event_syncer():
    event_syncer = FakeEventSyncer()
    registry = FakeRegistry()
    install_registry_hooks(event_syncer, registry)

    registry.on_guest_detached("guestA")
    assert event_syncer.detached == ["guestA"]


async def test_registry_frame_dispatch():
    event_syncer = FakeEventSyncer()
    registry = FakeRegistry()
    install_registry_hooks(event_syncer, registry)

    # event_* → event syncer
    assert await registry.on_unknown_frame("guestA", {"type": "event_batch"}) is True
    assert len(event_syncer.handled) == 1
    # unknown → not consumed
    assert await registry.on_unknown_frame("guestA", {"type": "mystery"}) is False


# ── guest_client side (peer key = 'host') ──

async def test_guest_client_attaches_on_connect():
    event_syncer = FakeEventSyncer()
    client = FakeGuestClient()
    install_guest_client_hooks(event_syncer, client)

    client.on_connect(client)
    assert event_syncer.attached == ["host"]


async def test_guest_client_frame_dispatch():
    event_syncer = FakeEventSyncer()
    client = FakeGuestClient()
    install_guest_client_hooks(event_syncer, client)

    assert await client.on_unknown_frame({"type": "event_resync"}) is True
    assert await client.on_unknown_frame({"type": "nope"}) is False
    assert len(event_syncer.handled) == 1

    client.on_disconnect()
    assert event_syncer.detached == ["host"]


# ── wire-version gate (mixed-version graceful drop) ──

async def test_frame_with_unsupported_wire_version_is_dropped():
    event_syncer = FakeEventSyncer()
    registry = FakeRegistry()
    install_registry_hooks(event_syncer, registry)

    # A frame from a newer protocol version: consumed (dropped), never dispatched.
    handled = await registry.on_unknown_frame("guestA", {"type": "event_batch", "v": 999})
    assert handled is True
    assert not event_syncer.handled

    # Missing v (legacy peer) is accepted and dispatched normally.
    assert await registry.on_unknown_frame("guestA", {"type": "event_batch"}) is True
    assert len(event_syncer.handled) == 1


async def test_current_wire_version_is_accepted():
    from boxagent.cluster.peer_transport import WIRE_VERSION
    event_syncer = FakeEventSyncer()
    registry = FakeRegistry()
    install_registry_hooks(event_syncer, registry)

    assert await registry.on_unknown_frame("guestA", {"type": "event_batch", "v": WIRE_VERSION}) is True
    assert len(event_syncer.handled) == 1
