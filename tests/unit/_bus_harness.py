"""In-process 2/3-node cluster harness for the MessageBus migration (Phase 0).

Wires, per node, a REAL EventStore + EventBus + EventSyncer + ChatSyncer +
ChatBus + WebChannel, linked by a bidirectional in-memory shuttle. A frame sent
by node A over a link arrives at node B's `handle_frame` — and, mirroring the
production single-WS `on_unknown_frame` chain, is offered first to B's
EventSyncer, then (if unconsumed) to B's ChatSyncer. This reproduces the real
"two frame vocabularies on one link" behaviour without any WebSocket.

This is TEST INFRASTRUCTURE, not product code. It generalizes:
- `test_event_syncer.py::_wire_pair` (event frames both directions), and
- `test_chat_sync.py::_make` + the `route` closure (chat frames + relay).

Observable-only surface (the invariants read ONLY these):
- `publish_event(...)`   — via the real `boxagent.log`-shaped EventBus.publish path
- `publish_chat(...)`    — via the real WebChannel._publish
- `subscribe_chat(...)`  — via the real ChatBus.subscribe -> asyncio.Queue
- `store_rows(node)`     — snapshot of a node's EventStore rows
- `link()/drop_link()/relink()` — reconnect (event cursor-resync + chat re-subscribe)
- `settle()`             — polls OBSERVABLE conditions, never a bare sleep
- `reorder_tasks`        — injection hook to permute pending-task order (RED proof)
- `CountingEventStore`   — counts insert_local/insert_remote (INV-A2 spy)

Test-only delivery seams (own the syncer reach-ins so the FROZEN invariant file
stays black-box; only the harness updates when the syncers move to
PeerTransport in Phase 1):
- `record_event_frames(node, peer)` / `record_chat_frames(node, peer)` —
  install a recording wrapper through the PUBLIC `attach_peer` seam; return a
  live list of frames sent toward that peer (INV-C2).
- `redeliver_event_batch(target, peer, events)` — the real duplicate-delivery
  path (INV-B3).
- `fail_event_peer(node, peer)` — swap in a raising send via `attach_peer`, a
  real failing link (INV-B8).
- `deliver_events_per_message(origin, target, messages, permute)` — the
  deliberately-broken create_task-per-message replication path used by the RED
  proof (INV-E-RED): one frame per event, delivered permuted, so arrival order
  (target store `id` order) scrambles.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from boxagent.cluster.chat_bus import ChatBus
from boxagent.cluster.chat_sync import ChatSyncer
from boxagent.events.bus import EventBus
from boxagent.events.storage import EventStore
from boxagent.events.sync import EventSyncer, event_to_dict
from boxagent.transports.web.channel import WebChannel


# --------------------------------------------------------------------------
# CountingEventStore — spy that counts the ONLY two SQLite write points.
# Used by INV-A2 to prove chat traffic never reaches the store: assert the
# insert-delta across a chat publish burst is exactly 0.
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
# A single node: real store + bus + event syncer + chat syncer + chat bus +
# one WebChannel per bot.
# --------------------------------------------------------------------------
class _Node:
    def __init__(self, machine_id: str, tmp_path, *, debounce_seconds: float) -> None:
        self.machine_id = machine_id
        self.store = CountingEventStore(tmp_path / f"{machine_id}.db")
        self.bus = EventBus(store=self.store, machine_id=machine_id)
        self.event_syncer = EventSyncer(
            self.store, self.bus, debounce_seconds=debounce_seconds,
        )

        # peer_key -> machine on the far side of that link (chat route needs this)
        self._peer_machine: dict[str, str] = {}
        # machine -> peer_key that reaches it (inverse; used by chat route())
        self._route_table: dict[str, str] = {}

        self.chat_syncer = ChatSyncer(
            local_machine=machine_id, route=self._route,
        )

        # One WebChannel per bot, lazily created.
        self._channels: dict[str, WebChannel] = {}
        self.chat_bus = ChatBus(
            local_machine=machine_id,
            syncer=self.chat_syncer,
            channel_for=self._channel_for,
        )

    # -- chat routing --------------------------------------------------------

    def _route(self, owner_machine: str) -> str | None:
        return self._route_table.get(owner_machine)

    def _channel_for(self, bot: str):
        return self._channels.get(bot)

    def channel(self, bot: str) -> WebChannel:
        channel = self._channels.get(bot)
        if channel is None:
            channel = WebChannel(bot_name=bot)
            self._channels[bot] = channel
        return channel

    # -- link bookkeeping ----------------------------------------------------

    def register_route(self, target_machine: str, peer_key: str) -> None:
        """Tell this node: to reach `target_machine`, send toward `peer_key`."""
        self._route_table[target_machine] = peer_key
        self._peer_machine[peer_key] = target_machine

    def clear_route(self, target_machine: str, peer_key: str) -> None:
        self._route_table.pop(target_machine, None)
        self._peer_machine.pop(peer_key, None)

    def close(self) -> None:
        try:
            self.event_syncer.close()
        except Exception:
            pass
        self.store.close()


# --------------------------------------------------------------------------
# A directed shuttle: frames from `source_node` toward `peer_key` are handed to
# `target_node`'s syncers. Mirrors the production single-WS dispatch:
# EventSyncer.handle_frame first, then (if not consumed) ChatSyncer.handle_frame.
# The `reverse_peer_key` is the key `target_node` uses to name `source_node`.
# --------------------------------------------------------------------------
class _Shuttle:
    def __init__(self, target_node: _Node, reverse_peer_key: str) -> None:
        self._target = target_node
        self._reverse_peer_key = reverse_peer_key
        self.connected = True
        # Injection hook: if set, called with a 0-arg coroutine-producing
        # callable list to control completion order (see reorder_tasks).
        self.deliver_hook: Callable[[Callable[[], Awaitable[None]]], Awaitable[None]] | None = None

    async def deliver(self, frame: dict) -> None:
        if not self.connected:
            return

        async def _dispatch() -> None:
            # Event frames first; chat picks up what event returns False for.
            consumed = await self._target.event_syncer.handle_frame(
                self._reverse_peer_key, frame,
            )
            if not consumed:
                await self._target.chat_syncer.handle_frame(
                    self._reverse_peer_key, frame,
                )

        if self.deliver_hook is not None:
            await self.deliver_hook(_dispatch)
        else:
            await _dispatch()


class _ReorderController:
    """Collects dispatch coroutines during a burst and releases them in a
    permuted order — used to PROVE the ordering invariant can go RED against a
    broken replicator that uses create_task-per-message.

    When NOT armed, deliveries pass straight through in call order (real code
    stays green). When armed, deliveries are buffered; `release()` runs them in
    the order chosen by `permute`.
    """

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
        # (a_machine, b_machine) -> (shuttle_a_to_b, shuttle_b_to_a)
        self._links: dict[tuple[str, str], tuple[_Shuttle, _Shuttle]] = {}
        # remembered link topology so relink() can rebuild without re-specifying.
        self._link_specs: list[tuple[str, str]] = []
        self.reorder = _ReorderController()

    # -- node/link construction ---------------------------------------------

    def _add_node(self, machine_id: str) -> _Node:
        node = _Node(machine_id, self._tmp_path, debounce_seconds=self.DEBOUNCE)
        self.nodes[machine_id] = node
        return node

    def _connect(self, a_machine: str, b_machine: str) -> None:
        """Create a bidirectional link between two nodes.

        peer_key convention: node A names its link to B by B's machine id, and
        vice-versa (matches the real host case where peer_key == guest machine).
        """
        node_a = self.nodes[a_machine]
        node_b = self.nodes[b_machine]

        # A frame A sends "toward B" is delivered to B; B names A by a_machine.
        shuttle_a_to_b = _Shuttle(node_b, reverse_peer_key=a_machine)
        shuttle_b_to_a = _Shuttle(node_a, reverse_peer_key=b_machine)
        shuttle_a_to_b.deliver_hook = self.reorder.hook
        shuttle_b_to_a.deliver_hook = self.reorder.hook

        async def a_send(frame):
            await shuttle_a_to_b.deliver(frame)

        async def b_send(frame):
            await shuttle_b_to_a.deliver(frame)

        # Event syncer peers (peer_key == far machine id).
        node_a.event_syncer.attach_peer(b_machine, a_send)
        node_b.event_syncer.attach_peer(a_machine, b_send)
        # Chat syncer peers (same keys).
        node_a.chat_syncer.attach_peer(b_machine, a_send)
        node_b.chat_syncer.attach_peer(a_machine, b_send)
        # Chat route tables.
        node_a.register_route(b_machine, b_machine)
        node_b.register_route(a_machine, a_machine)

        self._links[(a_machine, b_machine)] = (shuttle_a_to_b, shuttle_b_to_a)

    async def _disconnect(self, a_machine: str, b_machine: str) -> None:
        pair = self._links.pop((a_machine, b_machine), None)
        if pair is None:
            return
        shuttle_a_to_b, shuttle_b_to_a = pair
        shuttle_a_to_b.connected = False
        shuttle_b_to_a.connected = False
        node_a = self.nodes[a_machine]
        node_b = self.nodes[b_machine]
        node_a.event_syncer.detach_peer(b_machine)
        node_b.event_syncer.detach_peer(a_machine)
        await node_a.chat_syncer.detach_peer(b_machine)
        await node_b.chat_syncer.detach_peer(a_machine)
        node_a.clear_route(b_machine, b_machine)
        node_b.clear_route(a_machine, a_machine)

    # -- public link controls ------------------------------------------------

    def link(self) -> None:
        """(Re)establish every link in the remembered topology."""
        for a_machine, b_machine in self._link_specs:
            if (a_machine, b_machine) not in self._links:
                self._connect(a_machine, b_machine)

    async def drop_link(self, a_machine: str, b_machine: str) -> None:
        """Sever the link between two named nodes (WS disconnect)."""
        # Links are keyed by insertion order; try both orientations.
        if (a_machine, b_machine) in self._links:
            await self._disconnect(a_machine, b_machine)
        elif (b_machine, a_machine) in self._links:
            await self._disconnect(b_machine, a_machine)

    async def relink(self, a_machine: str, b_machine: str) -> None:
        """Reconnect a previously dropped link and drive the reconnect
        recovery (event cursor-resync via attach_peer + chat re-subscribe)."""
        spec = None
        for a_spec, b_spec in self._link_specs:
            if {a_spec, b_spec} == {a_machine, b_machine}:
                spec = (a_spec, b_spec)
                break
        if spec is None:
            return
        self._connect(*spec)
        # Chat: each side re-sends its remote chat_subscribe frames.
        node_a = self.nodes[spec[0]]
        node_b = self.nodes[spec[1]]
        await node_a.chat_syncer.resubscribe(spec[1])
        await node_b.chat_syncer.resubscribe(spec[0])

    # -- publish / subscribe (the real product paths) ------------------------

    def publish_event(self, machine: str, level: str, category: str,
                      message: str, **meta) -> None:
        """Publish via the real EventBus.publish (the log-facade sink shape)."""
        self.nodes[machine].bus.publish(level, category, message, **meta)

    def publish_chat(self, machine: str, bot: str, chat_id: str, event: dict) -> None:
        """Publish a chat event via the real WebChannel._publish fan-out."""
        self.nodes[machine].channel(bot)._publish(chat_id, event)

    async def subscribe_chat(self, watcher_machine: str, owner_machine: str,
                            bot: str, chat_id: str) -> asyncio.Queue:
        """Subscribe watcher_machine to (owner_machine, bot, chat_id).

        Returns the queue. Local (owner==watcher) rides the WebChannel queue;
        remote rides ChatBus/ChatSyncer over the link.
        """
        node = self.nodes[watcher_machine]
        # The owner-side WebChannel must exist before the demand pump fires
        # (the pump subscribes to it). In production AgentManager creates one
        # per bot at startup; here we create it lazily on the owner node.
        owner_channel = self.nodes[owner_machine].channel(bot)
        queue = await node.chat_bus.subscribe(bot, chat_id, owner_machine)
        if owner_machine != watcher_machine:
            # Remote subscription: wait until the owner-side demand pump has
            # actually subscribed to the owner channel, so a subsequent
            # publish_chat is guaranteed to fan out to it (no lost-first-event
            # race). This is an observable condition, not a fixed sleep.
            await self._wait_channel_subscribed(owner_channel, chat_id)
        return queue

    async def _wait_channel_subscribed(self, channel, chat_id: str,
                                       *, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if channel._subscribers.get(chat_id):
                return
            await asyncio.sleep(0)
            await _drain_ready_callbacks()

    def owner_channel(self, machine: str, bot: str) -> WebChannel:
        return self.nodes[machine].channel(bot)

    # -- observation ---------------------------------------------------------

    def store_rows(self, machine: str, **query_filter):
        return self.nodes[machine].store.query(**query_filter)

    def store(self, machine: str) -> CountingEventStore:
        return self.nodes[machine].store

    # -- test-only delivery seams (own the syncer reach-ins so the frozen
    #    invariant file stays clean; only the harness updates on refactor) ----

    def record_event_frames(self, machine: str, peer_key: str) -> list[dict]:
        """Record every event frame `machine` sends toward `peer_key`.

        Installs a recording wrapper through the PUBLIC `attach_peer` seam
        (never touches `event_syncer._peers`). Returns a live list that
        accumulates frames as they are sent. The wrapper still forwards to the
        original peer so delivery is unaffected."""
        return self._install_recording_peer(
            self.nodes[machine].event_syncer, peer_key,
        )

    def record_chat_frames(self, machine: str, peer_key: str) -> list[dict]:
        """Record every chat frame `machine` sends toward `peer_key`.

        Installs a recording wrapper through the PUBLIC `attach_peer` seam
        (never touches `chat_syncer._peers`)."""
        return self._install_recording_peer(
            self.nodes[machine].chat_syncer, peer_key,
        )

    @staticmethod
    def _install_recording_peer(syncer, peer_key: str) -> list[dict]:
        recorded: list[dict] = []
        # attach_peer is the public seam both syncers expose; we read the
        # currently-attached send only to chain it, which the harness (test
        # infra) is permitted to do — the frozen invariants never do.
        original = syncer._peers.get(peer_key)

        async def recording(frame: dict) -> None:
            recorded.append(frame)
            if original is not None:
                await original(frame)

        syncer.attach_peer(peer_key, recording)
        return recorded

    async def redeliver_event_batch(self, target: str, peer_key: str,
                                    events: list) -> None:
        """Re-deliver a batch of already-known events to `target` as if the peer
        `peer_key` sent them again (the real duplicate-delivery path). Owns the
        `event_syncer.handle_frame` reach-in so INV-B3 stays clean."""
        frame = {"type": "event_batch",
                 "events": [event_to_dict(event) for event in events]}
        await self.nodes[target].event_syncer.handle_frame(peer_key, frame)

    def fail_event_peer(self, machine: str, peer_key: str) -> None:
        """Replace `machine`'s event send toward `peer_key` with one that
        raises, through the PUBLIC `attach_peer` seam (a real failing link).
        Owns INV-B8's reach-in."""
        async def failing(_frame: dict) -> None:
            raise RuntimeError("boom")

        self.nodes[machine].event_syncer.attach_peer(peer_key, failing)

    async def deliver_events_per_message(
        self, *, origin: str, target: str, messages: list[str],
        permute: Callable[[list], list] = lambda items: list(items),
    ) -> None:
        """Deliberately-broken replication path used by INV-E-RED.

        Insert each message locally on `origin`, wrap each in its OWN one-event
        `event_batch` frame (the create_task-per-message footgun, 坑#1), then
        deliver those frames to `target` in `permute`d order through the real
        `event_syncer.handle_frame`. Reversed permute ⇒ arrival order (store id
        order on `target`) is reversed, which makes the REAL arrival-order
        assertion in INV-E1/E3 go RED. Owns the reach-in so the invariant body
        stays clean."""
        origin_node = self.nodes[origin]
        target_node = self.nodes[target]
        frames: list[dict] = []
        for message in messages:
            event = origin_node.store.insert_local(origin, "info", "c", message)
            frames.append({"type": "event_batch",
                           "events": [event_to_dict(event)]})
        for frame in permute(frames):
            await target_node.event_syncer.handle_frame(origin, frame)

    # -- settle: poll observable conditions, never a bare sleep --------------

    async def settle(self, *, timeout: float = 2.0) -> None:
        """Wait until the observable system is quiescent:
        - every event syncer's outbound buffer is empty
        - no event syncer has a live flush task
        - the event loop has drained pending ready callbacks

        Polls; does NOT sleep a fixed duration. We yield control repeatedly so
        debounce timers fire and pump/relay tasks run, then check the buffers.
        """
        deadline = time.monotonic() + timeout
        stable_rounds = 0
        while time.monotonic() < deadline:
            # Yield so debounce sleeps expire and queued tasks run.
            await asyncio.sleep(self.DEBOUNCE * 1.5)
            await _drain_ready_callbacks()
            if self._quiescent():
                stable_rounds += 1
                # Require two consecutive quiescent observations so a flush that
                # re-schedules itself (cross-batch) is not missed.
                if stable_rounds >= 2:
                    return
            else:
                stable_rounds = 0
        # One last drain even on timeout so callers see best-effort state.
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

    async def wait_for_queue(self, queue: asyncio.Queue, count: int,
                            *, timeout: float = 2.0) -> None:
        """Poll until `queue` holds at least `count` items (observable
        condition), then return. Complements settle() for chat delivery."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if queue.qsize() >= count:
                return
            await asyncio.sleep(self.DEBOUNCE)
            await _drain_ready_callbacks()

    async def aclose(self) -> None:
        for a_machine, b_machine in list(self._links.keys()):
            await self._disconnect(a_machine, b_machine)
        for node in self.nodes.values():
            await node.chat_bus.aclose()
            node.close()


async def _drain_ready_callbacks() -> None:
    """Give the loop several turns so chained create_task/callbacks all run."""
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
    """Hub-and-spoke: gA <-> host <-> gB. host relays chat + gossips events."""

    def __init__(self, tmp_path) -> None:
        super().__init__(tmp_path)
        self._add_node("gA")
        self._add_node("host")
        self._add_node("gB")
        self._link_specs = [("gA", "host"), ("gB", "host")]
        self.link()
        # For chat two-hop relay, gA must know that gB is reachable via host and
        # vice-versa (the direct-link route table only knows immediate peers).
        self.nodes["gA"].register_route("gB", "host")
        self.nodes["gB"].register_route("gA", "host")
