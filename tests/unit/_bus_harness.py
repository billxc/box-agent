"""In-process 2/3-node cluster harness for cross-machine EVENT replication.

Wires, per node, a REAL EventStore + EventBus + EventSyncer, linked by a
bidirectional in-memory shuttle. A frame sent by node A over a link arrives at
node B's EventSyncer.handle_frame — reproducing the production single-WS event
path without any WebSocket. (Chat + rpc now ride the ClusterBus and are covered
by test_cluster_bus.py / test_request_reply.py; this harness is events-only.)

This is TEST INFRASTRUCTURE, not product code. It generalizes
test_event_syncer.py::_wire_pair.

Observable-only surface (the invariants read ONLY these):
- `publish_event(...)`   — via the real EventBus.publish path
- `store_rows(node)`     — snapshot of a node's EventStore rows
- `link()/drop_link()/relink()` — reconnect (event cursor-resync)
- `settle()`             — polls OBSERVABLE conditions, never a bare sleep
- `reorder_tasks`        — injection hook to permute pending-task order (RED proof)
- `CountingEventStore`   — counts insert_local/insert_remote

Test-only delivery seams (own the syncer reach-ins so the invariant file stays
black-box):
- `record_event_frames(node, peer)` — recording wrapper via the PUBLIC attach_peer
- `redeliver_event_batch(target, peer, events)` — the real duplicate-delivery path
- `fail_event_peer(node, peer)` — a real failing link via attach_peer
- `deliver_events_per_message(origin, target, messages, permute)` — the broken
  create_task-per-message replication path used by the RED proof.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from boxagent.bus.core import MessageBus
from boxagent.events.bus import EventBus
from boxagent.events.storage import EventStore
from boxagent.events.sync import EventSyncer, event_to_dict


# --------------------------------------------------------------------------
# CountingEventStore — spy that counts the ONLY two SQLite write points.
# --------------------------------------------------------------------------
class CountingEventStore(EventStore):
    def __init__(self, db_path) -> None:
        super().__init__(db_path)
        self.insert_local_count = 0
        self.insert_remote_count = 0

    def insert_local(self, *args, **kwargs):
        self.insert_local_count += 1
        return super().insert_local(*args, **kwargs)

    def insert_remote(self, event) -> bool:
        self.insert_remote_count += 1
        return super().insert_remote(event)

    @property
    def total_inserts(self) -> int:
        return self.insert_local_count + self.insert_remote_count


# --------------------------------------------------------------------------
# A single node: real store + bus + event syncer.
# --------------------------------------------------------------------------
class _Node:
    def __init__(self, machine_id: str, tmp_path, *, debounce_seconds: float) -> None:
        self.machine_id = machine_id
        self.store = CountingEventStore(tmp_path / f"{machine_id}.db")
        self.message_bus = MessageBus(machine_id=machine_id)
        self.bus = EventBus(store=self.store, machine_id=machine_id, bus=self.message_bus)
        self.event_syncer = EventSyncer(
            self.store, self.bus, debounce_seconds=debounce_seconds,
        )

    def close(self) -> None:
        try:
            self.event_syncer.close()
        except Exception:
            pass
        self.store.close()


# --------------------------------------------------------------------------
# A directed shuttle: frames from `source_node` toward `peer_key` are handed to
# `target_node`'s EventSyncer. `reverse_peer_key` is the key `target_node` uses
# to name `source_node`.
# --------------------------------------------------------------------------
class _Shuttle:
    def __init__(self, target_node: _Node, reverse_peer_key: str) -> None:
        self._target = target_node
        self._reverse_peer_key = reverse_peer_key
        self.connected = True
        self.deliver_hook: Callable[[Callable[[], Awaitable[None]]], Awaitable[None]] | None = None

    async def deliver(self, frame: dict) -> None:
        if not self.connected:
            return

        async def _dispatch() -> None:
            await self._target.event_syncer.handle_frame(self._reverse_peer_key, frame)

        if self.deliver_hook is not None:
            await self.deliver_hook(_dispatch)
        else:
            await _dispatch()


class _ReorderController:
    """Collects dispatch coroutines during a burst and releases them in a
    permuted order — used to PROVE the ordering invariant can go RED against a
    broken replicator that uses create_task-per-message."""

    def __init__(self) -> None:
        self.armed = False
        self._buffered: list[Callable[[], Awaitable[None]]] = []
        self.permute: Callable[[list], list] = lambda items: list(reversed(items))

    async def hook(self, dispatch: Callable[[], Awaitable[None]]) -> None:
        if not self.armed:
            await dispatch()
            return
        self._buffered.append(dispatch)

    async def release(self) -> None:
        pending = self.permute(self._buffered)
        self._buffered = []
        for dispatch in pending:
            await dispatch()


# --------------------------------------------------------------------------
# The cluster: N nodes fully wired by shuttles.
# --------------------------------------------------------------------------
class _Cluster:
    DEBOUNCE = 0.01

    def __init__(self, tmp_path) -> None:
        self._tmp_path = tmp_path
        self.nodes: dict[str, _Node] = {}
        self._links: dict[tuple[str, str], tuple[_Shuttle, _Shuttle]] = {}
        self._link_specs: list[tuple[str, str]] = []
        self.reorder = _ReorderController()

    def _add_node(self, machine_id: str) -> _Node:
        node = _Node(machine_id, self._tmp_path, debounce_seconds=self.DEBOUNCE)
        self.nodes[machine_id] = node
        return node

    def _connect(self, a_machine: str, b_machine: str) -> None:
        node_a = self.nodes[a_machine]
        node_b = self.nodes[b_machine]

        shuttle_a_to_b = _Shuttle(node_b, reverse_peer_key=a_machine)
        shuttle_b_to_a = _Shuttle(node_a, reverse_peer_key=b_machine)
        shuttle_a_to_b.deliver_hook = self.reorder.hook
        shuttle_b_to_a.deliver_hook = self.reorder.hook

        async def a_send(frame):
            await shuttle_a_to_b.deliver(frame)

        async def b_send(frame):
            await shuttle_b_to_a.deliver(frame)

        node_a.event_syncer.attach_peer(b_machine, a_send)
        node_b.event_syncer.attach_peer(a_machine, b_send)
        self._links[(a_machine, b_machine)] = (shuttle_a_to_b, shuttle_b_to_a)

    async def _disconnect(self, a_machine: str, b_machine: str) -> None:
        pair = self._links.pop((a_machine, b_machine), None)
        if pair is None:
            return
        shuttle_a_to_b, shuttle_b_to_a = pair
        shuttle_a_to_b.connected = False
        shuttle_b_to_a.connected = False
        self.nodes[a_machine].event_syncer.detach_peer(b_machine)
        self.nodes[b_machine].event_syncer.detach_peer(a_machine)

    # -- public link controls ------------------------------------------------

    def link(self) -> None:
        for a_machine, b_machine in self._link_specs:
            if (a_machine, b_machine) not in self._links:
                self._connect(a_machine, b_machine)

    async def drop_link(self, a_machine: str, b_machine: str) -> None:
        if (a_machine, b_machine) in self._links:
            await self._disconnect(a_machine, b_machine)
        elif (b_machine, a_machine) in self._links:
            await self._disconnect(b_machine, a_machine)

    async def relink(self, a_machine: str, b_machine: str) -> None:
        """Reconnect a dropped link and drive event cursor-resync via attach_peer."""
        spec = None
        for a_spec, b_spec in self._link_specs:
            if {a_spec, b_spec} == {a_machine, b_machine}:
                spec = (a_spec, b_spec)
                break
        if spec is None:
            return
        self._connect(*spec)

    # -- publish (the real product path) -------------------------------------

    def publish_event(self, machine: str, level: str, category: str,
                      message: str, **meta) -> None:
        self.nodes[machine].bus.publish(level, category, message, **meta)

    # -- observation ---------------------------------------------------------

    def store_rows(self, machine: str, **query_filter):
        return self.nodes[machine].store.query(**query_filter)

    def store(self, machine: str) -> CountingEventStore:
        return self.nodes[machine].store

    # -- test-only delivery seams --------------------------------------------

    def record_event_frames(self, machine: str, peer_key: str) -> list[dict]:
        """Record every event frame `machine` sends toward `peer_key`, via the
        PUBLIC attach_peer seam. Returns a live list."""
        recorded: list[dict] = []
        syncer = self.nodes[machine].event_syncer
        original = syncer._peers.get(peer_key)

        async def recording(frame: dict) -> None:
            recorded.append(frame)
            if original is not None:
                await original(frame)

        syncer.attach_peer(peer_key, recording)
        return recorded

    async def redeliver_event_batch(self, target: str, peer_key: str,
                                    events: list) -> None:
        frame = {"type": "event_batch",
                 "events": [event_to_dict(event) for event in events]}
        await self.nodes[target].event_syncer.handle_frame(peer_key, frame)

    def fail_event_peer(self, machine: str, peer_key: str) -> None:
        async def failing(_frame: dict) -> None:
            raise RuntimeError("boom")

        self.nodes[machine].event_syncer.attach_peer(peer_key, failing)

    async def deliver_events_per_message(
        self, *, origin: str, target: str, messages: list[str],
        permute: Callable[[list], list] = lambda items: list(items),
    ) -> None:
        """Deliberately-broken create_task-per-message replication (INV-E-RED):
        one frame per event, delivered permuted, so arrival order scrambles."""
        origin_node = self.nodes[origin]
        target_node = self.nodes[target]
        frames: list[dict] = []
        for message in messages:
            event = origin_node.store.insert_local(origin, "info", "c", message)
            frames.append({"type": "event_batch", "events": [event_to_dict(event)]})
        for frame in permute(frames):
            await target_node.event_syncer.handle_frame(origin, frame)

    # -- settle: poll observable conditions, never a bare sleep --------------

    async def settle(self, *, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        stable_rounds = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(self.DEBOUNCE * 1.5)
            await _drain_ready_callbacks()
            if self._quiescent():
                stable_rounds += 1
                if stable_rounds >= 2:
                    return
            else:
                stable_rounds = 0
        await _drain_ready_callbacks()

    def _quiescent(self) -> bool:
        for node in self.nodes.values():
            syncer = node.event_syncer
            if syncer._buffer:
                return False
            flush_task = syncer._flush_task
            if flush_task is not None and not flush_task.done():
                return False
        return True

    async def aclose(self) -> None:
        for a_machine, b_machine in list(self._links.keys()):
            await self._disconnect(a_machine, b_machine)
        for node in self.nodes.values():
            node.close()


async def _drain_ready_callbacks() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------
# Concrete topologies.
# --------------------------------------------------------------------------
class TwoNodeCluster(_Cluster):
    """Two peers, A <-> B, directly linked."""

    def __init__(self, tmp_path) -> None:
        super().__init__(tmp_path)
        self._add_node("A")
        self._add_node("B")
        self._link_specs = [("A", "B")]
        self.link()


class ThreeNodeCluster(_Cluster):
    """Hub-and-spoke: gA <-> host <-> gB. host gossips events."""

    def __init__(self, tmp_path) -> None:
        super().__init__(tmp_path)
        self._add_node("gA")
        self._add_node("host")
        self._add_node("gB")
        self._link_specs = [("gA", "host"), ("gB", "host")]
        self.link()
