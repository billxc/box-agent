"""Tests for bus_wiring — the one wiring that dispatches both syncers' frames.

Replaces test_event_sync_wiring.py + test_chat_sync_wiring.py. The behavior they
covered (each syncer's frames reach it; frames don't swallow each other) is now
one unified dispatch with no install-order chain, exercised here with fake
syncers + a fake registry / guest_client.
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

    def detach_peer(self, peer_key) -> None:  # sync, like the real EventSyncer
        self.detached.append(peer_key)

    async def handle_frame(self, peer_key, payload) -> bool:
        if str(payload.get("type", "")).startswith("event_"):
            self.handled.append((peer_key, payload))
            return True
        return False


class FakeChatSyncer:
    """Matches ChatSyncer's shape: sync attach, async detach/resubscribe/handle."""

    def __init__(self) -> None:
        self.attached: list[str] = []
        self.detached: list[str] = []
        self.resubscribed: list[str] = []
        self.handled: list[tuple[str, dict]] = []

    def attach_peer(self, peer_key, send_frame) -> None:
        self.attached.append(peer_key)

    async def detach_peer(self, peer_key) -> None:  # async, like the real ChatSyncer
        self.detached.append(peer_key)

    async def resubscribe(self, peer_key) -> None:
        self.resubscribed.append(peer_key)

    async def handle_frame(self, peer_key, payload) -> bool:
        if str(payload.get("type", "")).startswith("chat_"):
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

async def test_registry_attaches_both_syncers_on_guest():
    event_syncer, chat_syncer = FakeEventSyncer(), FakeChatSyncer()
    registry = FakeRegistry()
    install_registry_hooks(event_syncer, chat_syncer, registry)

    registry.on_guest_attached("guestA", FakeSession())
    await asyncio.sleep(0)  # let the chat resubscribe task run
    assert event_syncer.attached == ["guestA"]
    assert chat_syncer.attached == ["guestA"]
    assert chat_syncer.resubscribed == ["guestA"]


async def test_registry_detaches_both_syncers():
    event_syncer, chat_syncer = FakeEventSyncer(), FakeChatSyncer()
    registry = FakeRegistry()
    install_registry_hooks(event_syncer, chat_syncer, registry)

    registry.on_guest_detached("guestA")
    await asyncio.sleep(0)
    assert event_syncer.detached == ["guestA"]
    assert chat_syncer.detached == ["guestA"]


async def test_registry_frame_dispatch_no_swallow():
    event_syncer, chat_syncer = FakeEventSyncer(), FakeChatSyncer()
    registry = FakeRegistry()
    install_registry_hooks(event_syncer, chat_syncer, registry)

    # event_* → event syncer only
    assert await registry.on_unknown_frame("guestA", {"type": "event_batch"}) is True
    assert len(event_syncer.handled) == 1 and not chat_syncer.handled
    # chat_* → chat syncer only (not swallowed by the event syncer)
    assert await registry.on_unknown_frame("guestA", {"type": "chat_event"}) is True
    assert len(chat_syncer.handled) == 1
    # unknown → neither
    assert await registry.on_unknown_frame("guestA", {"type": "mystery"}) is False


# ── guest_client side (peer key = 'host') ──

async def test_guest_client_attaches_both_on_connect():
    event_syncer, chat_syncer = FakeEventSyncer(), FakeChatSyncer()
    client = FakeGuestClient()
    install_guest_client_hooks(event_syncer, chat_syncer, client)

    client.on_connect(client)
    await asyncio.sleep(0)
    assert event_syncer.attached == ["host"]
    assert chat_syncer.attached == ["host"]
    assert chat_syncer.resubscribed == ["host"]


async def test_guest_client_frame_dispatch():
    event_syncer, chat_syncer = FakeEventSyncer(), FakeChatSyncer()
    client = FakeGuestClient()
    install_guest_client_hooks(event_syncer, chat_syncer, client)

    assert await client.on_unknown_frame({"type": "event_resync"}) is True
    assert await client.on_unknown_frame({"type": "chat_subscribe"}) is True
    assert await client.on_unknown_frame({"type": "nope"}) is False
    assert len(event_syncer.handled) == 1 and len(chat_syncer.handled) == 1

    client.on_disconnect()
    await asyncio.sleep(0)
    assert event_syncer.detached == ["host"] and chat_syncer.detached == ["host"]
