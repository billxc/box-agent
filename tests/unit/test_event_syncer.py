"""Tests for EventSyncer — directly wire two stores via in-memory send-callables."""
from __future__ import annotations

import asyncio
import time

import pytest

from boxagent.events.bus import EventBus
from boxagent.events.storage import EventStore
from boxagent.events.sync import (
    EventSyncer,
    event_from_dict,
    event_to_dict,
    SYNC_WINDOW_SECONDS,
)


def _make_node(tmp_path, name):
    store = EventStore(tmp_path / f"{name}.db")
    bus = EventBus(store=store, machine_id=name)
    syncer = EventSyncer(store, bus, debounce_seconds=0.01)
    return store, bus, syncer


def _wire_pair(syncer_a, syncer_b, key_a="A", key_b="B"):
    """Attach a↔b so frames sent by a arrive at b.handle_frame(key_a, ...)."""
    async def a_to_b(frame):
        await syncer_b.handle_frame(key_a, frame)

    async def b_to_a(frame):
        await syncer_a.handle_frame(key_b, frame)

    syncer_a.attach_peer(key_b, a_to_b)
    syncer_b.attach_peer(key_a, b_to_a)


# ---------- helpers ----------

def test_event_dict_roundtrip():
    store = EventStore(":memory:")
    e = store.insert_local("m1", "info", "x.y", "hello", bot="b1", meta={"a": 1})
    raw = event_to_dict(e)
    e2 = event_from_dict(raw)
    assert e2.origin_machine == "m1"
    assert e2.origin_seq == e.origin_seq
    assert e2.message == "hello"
    assert e2.bot == "b1"
    assert e2.meta == {"a": 1}


# ---------- one-direction live publish ----------

@pytest.mark.asyncio
async def test_local_publish_propagates_to_peer(tmp_path):
    store_a, bus_a, sync_a = _make_node(tmp_path, "A")
    store_b, bus_b, sync_b = _make_node(tmp_path, "B")
    _wire_pair(sync_a, sync_b)

    bus_a.publish("info", "scheduler.run", "task fired", bot="bot_a")
    await asyncio.sleep(0.05)  # let debounce flush

    events = store_b.query()
    assert len(events) == 1
    assert events[0].origin_machine == "A"
    assert events[0].message == "task fired"
    sync_a.close(); sync_b.close()


# ---------- bidirectional ----------

@pytest.mark.asyncio
async def test_bidirectional_sync(tmp_path):
    store_a, bus_a, sync_a = _make_node(tmp_path, "A")
    store_b, bus_b, sync_b = _make_node(tmp_path, "B")
    _wire_pair(sync_a, sync_b)

    bus_a.publish("info", "c.a", "from A")
    bus_b.publish("error", "c.b", "from B")
    await asyncio.sleep(0.05)

    a_events = {e.message for e in store_a.query()}
    b_events = {e.message for e in store_b.query()}
    assert a_events == {"from A", "from B"}
    assert b_events == {"from A", "from B"}
    sync_a.close(); sync_b.close()


# ---------- dedup via natural key ----------

@pytest.mark.asyncio
async def test_duplicate_batch_is_ignored(tmp_path):
    store_a, bus_a, sync_a = _make_node(tmp_path, "A")
    store_b, bus_b, sync_b = _make_node(tmp_path, "B")
    _wire_pair(sync_a, sync_b)

    bus_a.publish("info", "c", "once")
    await asyncio.sleep(0.05)

    # Manually re-deliver the same batch — store dedup must drop it
    e = store_a.query()[0]
    await sync_b.handle_frame("A", {
        "type": "event_batch",
        "events": [event_to_dict(e)],
    })
    assert len(store_b.query()) == 1
    sync_a.close(); sync_b.close()


# ---------- resync on attach (catches up backlog) ----------

@pytest.mark.asyncio
async def test_resync_on_attach_backfills(tmp_path):
    store_a, bus_a, sync_a = _make_node(tmp_path, "A")
    store_b, bus_b, sync_b = _make_node(tmp_path, "B")
    # Publish before wiring — B should not have these yet
    bus_a.publish("info", "c", "old1")
    bus_a.publish("info", "c", "old2")
    assert store_b.query() == []

    _wire_pair(sync_a, sync_b)
    # attach_peer schedules an event_resync; give the loop time to run it
    await asyncio.sleep(0.05)

    msgs = {e.message for e in store_b.query()}
    assert msgs == {"old1", "old2"}
    sync_a.close(); sync_b.close()


# ---------- gossip: host re-broadcasts to other guests ----------

@pytest.mark.asyncio
async def test_host_gossips_guest_event_to_other_guests(tmp_path):
    """Hub-and-spoke: g1 → host → g2."""
    _, bus_h, sync_h = _make_node(tmp_path, "host")
    _, bus_g1, sync_g1 = _make_node(tmp_path, "g1")
    store_g2, bus_g2, sync_g2 = _make_node(tmp_path, "g2")

    # Host attaches both guests; each guest only sees host
    async def h_to_g1(f): await sync_g1.handle_frame("host", f)
    async def h_to_g2(f): await sync_g2.handle_frame("host", f)
    async def g1_to_h(f): await sync_h.handle_frame("g1", f)
    async def g2_to_h(f): await sync_h.handle_frame("g2", f)
    sync_h.attach_peer("g1", h_to_g1)
    sync_h.attach_peer("g2", h_to_g2)
    sync_g1.attach_peer("host", g1_to_h)
    sync_g2.attach_peer("host", g2_to_h)

    bus_g1.publish("info", "agent.notify", "hi from g1")
    await asyncio.sleep(0.05)

    msgs = {e.message for e in store_g2.query()}
    assert "hi from g1" in msgs
    sync_h.close(); sync_g1.close(); sync_g2.close()


# ---------- 3-day window: old events not re-synced ----------

@pytest.mark.asyncio
async def test_old_events_excluded_from_resync(tmp_path):
    store_a, bus_a, sync_a = _make_node(tmp_path, "A")
    store_b, bus_b, sync_b = _make_node(tmp_path, "B")

    # Inject one fresh and one ancient event into A
    now = time.time()
    store_a.insert_local("A", "info", "c", "fresh", ts=now)
    store_a.insert_local("A", "info", "c", "ancient", ts=now - SYNC_WINDOW_SECONDS - 100)

    _wire_pair(sync_a, sync_b)
    await asyncio.sleep(0.05)

    msgs = {e.message for e in store_b.query()}
    assert msgs == {"fresh"}
    sync_a.close(); sync_b.close()


# ---------- new local event after old ones: window filter on emit ----------

@pytest.mark.asyncio
async def test_old_event_not_pushed_on_publish(tmp_path):
    store_a, bus_a, sync_a = _make_node(tmp_path, "A")
    store_b, bus_b, sync_b = _make_node(tmp_path, "B")
    _wire_pair(sync_a, sync_b)
    await asyncio.sleep(0.05)  # drain initial resync (empty)

    # Manually insert an ancient event then trigger callback path.
    # Easier: insert via store directly and confirm bus.publish path filters
    # by simulating the event via subscriber.
    from boxagent.events.models import Event
    ancient = Event(
        id=1, origin_machine="A", origin_seq=1,
        ts=time.time() - SYNC_WINDOW_SECONDS - 10,
        level="info", category="c", message="too old", bot=None, meta={},
    )
    sync_a._on_local_event(ancient)
    await asyncio.sleep(0.05)
    assert store_b.query() == []
    sync_a.close(); sync_b.close()


# ---------- detach_peer stops delivery ----------

@pytest.mark.asyncio
async def test_detach_peer_stops_sync(tmp_path):
    store_a, bus_a, sync_a = _make_node(tmp_path, "A")
    store_b, bus_b, sync_b = _make_node(tmp_path, "B")
    _wire_pair(sync_a, sync_b)
    await asyncio.sleep(0.05)

    sync_a.detach_peer("B")
    bus_a.publish("info", "c", "after detach")
    await asyncio.sleep(0.05)

    msgs = {e.message for e in store_b.query()}
    assert "after detach" not in msgs
    sync_a.close(); sync_b.close()


# ---------- send failure does not crash ----------

@pytest.mark.asyncio
async def test_send_failure_is_swallowed(tmp_path):
    store_a, bus_a, sync_a = _make_node(tmp_path, "A")

    async def failing(_frame): raise RuntimeError("boom")
    sync_a.attach_peer("ghost", failing)
    await asyncio.sleep(0.05)

    bus_a.publish("info", "c", "ok")
    await asyncio.sleep(0.05)
    # Local store still has the event
    assert len(store_a.query()) == 1
    sync_a.close()


# ---------- handle_frame returns False for unknown types ----------

@pytest.mark.asyncio
async def test_handle_frame_unknown_returns_false(tmp_path):
    _, _, sync_a = _make_node(tmp_path, "A")
    consumed = await sync_a.handle_frame("peer", {"type": "rpc"})
    assert consumed is False
    consumed2 = await sync_a.handle_frame("peer", {"type": "event_batch", "events": []})
    assert consumed2 is True
    sync_a.close()
