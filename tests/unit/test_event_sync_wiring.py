"""Tests for sync_wiring: hooks that bridge cluster registry/guest_client to EventSyncer."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import pytest

from boxagent.events.bus import EventBus
from boxagent.events.storage import EventStore
from boxagent.events.sync import EventSyncer, event_to_dict
from boxagent.events.sync_wiring import (
    install_guest_client_hooks,
    install_registry_hooks,
)


@dataclass
class _FakeRegistry:
    """Mimics GuestRegistry hook surface."""
    on_unknown_frame: Callable[[str, dict], Awaitable[bool]] | None = None
    on_guest_attached: Callable[[str, object], None] | None = None
    on_guest_detached: Callable[[str], None] | None = None
    sessions: dict = field(default_factory=dict)


@dataclass
class _FakeWs:
    sent: list = field(default_factory=list)
    closed: bool = False

    async def send_json(self, frame):
        self.sent.append(frame)


@dataclass
class _FakeSession:
    machine_id: str
    ws: _FakeWs = field(default_factory=_FakeWs)

    @property
    def sent(self):
        return self.ws.sent


@dataclass
class _FakeGuestClient:
    """Mimics GuestClient hook surface."""
    on_unknown_frame: Callable[[dict], Awaitable[bool]] | None = None
    on_connect: Callable[[object], None] | None = None
    on_disconnect: Callable[[], None] | None = None
    _ws: _FakeWs = field(default_factory=_FakeWs)

    @property
    def sent(self):
        return self._ws.sent


# ---------- registry side ----------

@pytest.mark.asyncio
async def test_registry_hook_attaches_peer_on_guest_connect(tmp_path):
    store = EventStore(tmp_path / "e.db")
    bus = EventBus(store, "host")
    syncer = EventSyncer(store, bus, debounce_seconds=0.01)
    reg = _FakeRegistry()
    install_registry_hooks(syncer, reg)

    session = _FakeSession(machine_id="g1")
    reg.sessions["g1"] = session
    reg.on_guest_attached("g1", session)
    await asyncio.sleep(0.02)  # let the resync coroutine run

    # Resync request was sent to the new guest
    assert any(f.get("type") == "event_resync" for f in session.sent)
    syncer.close()


@pytest.mark.asyncio
async def test_registry_hook_detaches_on_guest_disconnect(tmp_path):
    store = EventStore(tmp_path / "e.db")
    bus = EventBus(store, "host")
    syncer = EventSyncer(store, bus, debounce_seconds=0.01)
    reg = _FakeRegistry()
    install_registry_hooks(syncer, reg)
    session = _FakeSession(machine_id="g1")
    reg.sessions["g1"] = session
    reg.on_guest_attached("g1", session)
    await asyncio.sleep(0.02)
    reg.on_guest_detached("g1")
    # After detach, publishing should not throw and not deliver to g1
    session.sent.clear()
    bus.publish("info", "c", "after detach")
    await asyncio.sleep(0.05)
    assert all(f.get("type") != "event_batch" for f in session.sent)
    syncer.close()


@pytest.mark.asyncio
async def test_registry_hook_dispatches_event_batch(tmp_path):
    store = EventStore(tmp_path / "e.db")
    bus = EventBus(store, "host")
    syncer = EventSyncer(store, bus, debounce_seconds=0.01)
    reg = _FakeRegistry()
    install_registry_hooks(syncer, reg)

    session = _FakeSession(machine_id="g1")
    reg.sessions["g1"] = session
    reg.on_guest_attached("g1", session)
    await asyncio.sleep(0.02)

    # Simulate guest sending us an event_batch
    fake_event = {
        "origin_machine": "g1", "origin_seq": 1, "ts": 9_999_999_999.0,
        "level": "info", "category": "agent.test", "message": "from guest",
        "bot": None, "meta": {},
    }
    consumed = await reg.on_unknown_frame("g1", {
        "type": "event_batch", "events": [fake_event],
    })
    assert consumed is True
    # The batch was inserted into our store
    assert any(e.message == "from guest" for e in store.query())
    syncer.close()


# ---------- guest_client side ----------

@pytest.mark.asyncio
async def test_guest_client_hook_attaches_host_on_connect(tmp_path):
    store = EventStore(tmp_path / "e.db")
    bus = EventBus(store, "g1")
    syncer = EventSyncer(store, bus, debounce_seconds=0.01)
    client = _FakeGuestClient()
    install_guest_client_hooks(syncer, client)

    client.on_connect(client)
    await asyncio.sleep(0.02)
    assert any(f.get("type") == "event_resync" for f in client.sent)
    syncer.close()


@pytest.mark.asyncio
async def test_guest_client_hook_dispatches_event_batch(tmp_path):
    store = EventStore(tmp_path / "e.db")
    bus = EventBus(store, "g1")
    syncer = EventSyncer(store, bus, debounce_seconds=0.01)
    client = _FakeGuestClient()
    install_guest_client_hooks(syncer, client)
    client.on_connect(client)
    await asyncio.sleep(0.02)

    fake_event = {
        "origin_machine": "host", "origin_seq": 1, "ts": 9_999_999_999.0,
        "level": "error", "category": "backend.crash", "message": "from host",
        "bot": None, "meta": {},
    }
    consumed = await client.on_unknown_frame({
        "type": "event_batch", "events": [fake_event],
    })
    assert consumed is True
    assert any(e.message == "from host" for e in store.query())
    syncer.close()


@pytest.mark.asyncio
async def test_guest_client_hook_detaches_on_disconnect(tmp_path):
    store = EventStore(tmp_path / "e.db")
    bus = EventBus(store, "g1")
    syncer = EventSyncer(store, bus, debounce_seconds=0.01)
    client = _FakeGuestClient()
    install_guest_client_hooks(syncer, client)
    client.on_connect(client)
    await asyncio.sleep(0.02)
    client.sent.clear()
    client.on_disconnect()
    bus.publish("info", "c", "after disconnect")
    await asyncio.sleep(0.05)
    assert all(f.get("type") != "event_batch" for f in client.sent)
    syncer.close()


# ---------- integration with REAL registry / guest_client objects ----------
#
# These pin the wiring to the actual attribute paths
# (`session.ws.send_json`, `client._ws.send_json`). If someone renames
# either, fakes wouldn't catch it but these tests will.

class _RecordingWs:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send_json(self, frame):
        self.sent.append(frame)


@pytest.mark.asyncio
async def test_real_registry_wiring_uses_session_ws(tmp_path):
    from boxagent.cluster.registry import GuestRegistry, GuestSession

    store = EventStore(tmp_path / "e.db")
    bus = EventBus(store, "host")
    syncer = EventSyncer(store, bus, debounce_seconds=0.01)
    registry = GuestRegistry(expected_token="t")
    install_registry_hooks(syncer, registry)

    ws = _RecordingWs()
    session = GuestSession(machine_id="g1", ws=ws)  # type: ignore[arg-type]
    registry.on_guest_attached("g1", session)
    await asyncio.sleep(0.02)

    assert any(f.get("type") == "event_resync" for f in ws.sent)
    syncer.close()


@pytest.mark.asyncio
async def test_real_guest_client_wiring_uses_underscore_ws(tmp_path):
    from boxagent.cluster.guest_client import GuestClient

    store = EventStore(tmp_path / "e.db")
    bus = EventBus(store, "g1")
    syncer = EventSyncer(store, bus, debounce_seconds=0.01)
    client = GuestClient(
        host_url="", host_token="", machine_id="g1", local_web_port=0,
    )
    install_guest_client_hooks(syncer, client)

    ws = _RecordingWs()
    client._ws = ws  # type: ignore[assignment]
    client.on_connect(client)
    await asyncio.sleep(0.02)

    assert any(f.get("type") == "event_resync" for f in ws.sent)
    syncer.close()
