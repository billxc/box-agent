"""Cross-machine event sync over the cluster WebSocket.

Wire frames (in addition to the existing cluster RPC envelope):

    {"type": "event_batch", "events": [<event_dict>, ...]}
    {"type": "event_resync", "cursors": {<machine_id>: <last_seen_seq>, ...}}

Both directions: host ↔ guest. On connect, each side asks the other for
events newer than its local cursors. On every locally-published event, the
syncer debounces 200ms then broadcasts to all attached peers.

Master-slave full replication: every machine ends up with every event.
Sync window is 3 days (events older than that on origin only — by design).
Local retention is 30 days, handled by retention sweeper (separate commit).

`(origin_machine, origin_seq)` is the cross-cluster natural key; insert_remote
uses INSERT OR IGNORE so duplicates from gossip cycles are harmless.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from .bus import EventBus
from .models import Event
from .storage import EventStore

logger = logging.getLogger(__name__)

SYNC_WINDOW_SECONDS = 3 * 86400  # only sync events newer than this
DEBOUNCE_SECONDS = 0.2
MAX_BATCH = 500

SendFrame = Callable[[dict], Awaitable[None]]


def event_to_dict(event: Event) -> dict:
    return {
        "origin_machine": event.origin_machine,
        "origin_seq": event.origin_seq,
        "ts": event.ts,
        "level": event.level,
        "category": event.category,
        "message": event.message,
        "bot": event.bot,
        "meta": event.meta or {},
    }


def event_from_dict(data: dict) -> Event:
    return Event(
        id=None,
        origin_machine=str(data.get("origin_machine") or ""),
        origin_seq=int(data.get("origin_seq") or 0),
        ts=float(data.get("ts") or 0.0),
        level=str(data.get("level") or "info"),
        category=str(data.get("category") or "unknown"),
        message=str(data.get("message") or ""),
        bot=data.get("bot"),
        meta=data.get("meta") or {},
    )


class EventSyncer:
    """Bridges the local EventBus to peers via injected send-callables.

    The syncer is role-agnostic: host attaches one peer per connected guest,
    guest attaches a single peer ("host"). Caller supplies the send_frame
    coroutine. Inbound frames are dispatched via :meth:`handle_frame`.
    """

    def __init__(
        self,
        store: EventStore,
        bus: EventBus,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        sync_window_seconds: float = SYNC_WINDOW_SECONDS,
        debounce_seconds: float = DEBOUNCE_SECONDS,
    ) -> None:
        self._store = store
        self._bus = bus
        self._loop = loop
        self._window = sync_window_seconds
        self._debounce = debounce_seconds
        self._peers: dict[str, SendFrame] = {}
        self._buffer: list[Event] = []
        self._flush_task: asyncio.Task | None = None
        bus.subscribe(self._on_local_event)

    # ---------- peer lifecycle ----------

    def attach_peer(self, peer_key: str, send_frame: SendFrame) -> None:
        """Register a peer and schedule an initial resync request to it."""
        self._peers[peer_key] = send_frame
        cursors = self._store.max_seq_per_machine()
        self._spawn(self._send_to(peer_key, {
            "type": "event_resync",
            "cursors": cursors,
        }))

    def detach_peer(self, peer_key: str) -> None:
        self._peers.pop(peer_key, None)

    def close(self) -> None:
        try:
            self._bus.unsubscribe(self._on_local_event)
        except Exception:
            pass
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
        self._peers.clear()
        self._buffer.clear()

    # ---------- inbound ----------

    async def handle_frame(self, peer_key: str, payload: dict) -> bool:
        """Return True if the frame was consumed by the syncer."""
        kind = payload.get("type")
        if kind == "event_batch":
            await self._handle_batch(peer_key, payload.get("events") or [])
            return True
        if kind == "event_resync":
            await self._handle_resync(peer_key, payload.get("cursors") or {})
            return True
        return False

    async def _handle_batch(self, source_peer: str, events_raw: list) -> None:
        forwarded: list[Event] = []
        for raw in events_raw:
            if not isinstance(raw, dict):
                continue
            try:
                event = event_from_dict(raw)
            except Exception:
                logger.exception("syncer: bad event in batch")
                continue
            if not event.origin_machine or event.origin_seq <= 0:
                continue
            try:
                inserted = self._store.insert_remote(event)
            except Exception:
                logger.exception("syncer: insert_remote failed")
                continue
            if inserted:
                forwarded.append(event)
        if not forwarded:
            return
        # Gossip to other peers (not back to source). Only the host has >1 peer
        # in the typical hub-and-spoke; on a guest this loop is a no-op.
        others = [p for p in self._peers if p != source_peer]
        if not others:
            return
        frame = {
            "type": "event_batch",
            "events": [event_to_dict(e) for e in forwarded],
        }
        for peer_key in others:
            await self._send_to(peer_key, frame)

    async def _handle_resync(self, peer_key: str, cursors: dict) -> None:
        send = self._peers.get(peer_key)
        if send is None:
            return
        since_ts = time.time() - self._window
        peer_cursors = {str(k): int(v) for k, v in cursors.items() if isinstance(v, (int, float))}
        machines = self._store.known_machines()
        out: list[dict] = []
        for machine in machines:
            after = peer_cursors.get(machine, 0)
            local_max = self._store.max_origin_seq(machine)
            if local_max <= after:
                continue
            events = self._store.events_after_seq(
                machine, after, since_ts=since_ts, limit=MAX_BATCH,
            )
            out.extend(event_to_dict(e) for e in events)
            if len(out) >= MAX_BATCH:
                break
        if out:
            await self._send_to(peer_key, {"type": "event_batch", "events": out})

    # ---------- outbound (debounced) ----------

    def _on_local_event(self, event: Event) -> None:
        # Bus calls subscribers synchronously; we just buffer + schedule flush.
        # Older-than-window events are not synced (sender-side filter).
        if event.ts < time.time() - self._window:
            return
        self._buffer.append(event)
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        loop = self._loop or self._running_loop()
        if loop is None:
            return
        if self._flush_task is not None and not self._flush_task.done():
            return
        self._flush_task = loop.create_task(self._flush_after_debounce())

    async def _flush_after_debounce(self) -> None:
        try:
            await asyncio.sleep(self._debounce)
        except asyncio.CancelledError:
            return
        await self._flush()

    async def _flush(self) -> None:
        if not self._buffer or not self._peers:
            self._buffer.clear()
            return
        events = self._buffer[:MAX_BATCH]
        self._buffer = self._buffer[MAX_BATCH:]
        frame = {
            "type": "event_batch",
            "events": [event_to_dict(e) for e in events],
        }
        for peer_key in list(self._peers):
            await self._send_to(peer_key, frame)
        if self._buffer:
            self._schedule_flush()

    # ---------- helpers ----------

    async def _send_to(self, peer_key: str, frame: dict) -> None:
        send = self._peers.get(peer_key)
        if send is None:
            return
        try:
            await send(frame)
        except Exception as exc:
            logger.warning("syncer: send to %s failed: %r", peer_key, exc)

    @staticmethod
    def _running_loop() -> asyncio.AbstractEventLoop | None:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def _spawn(self, coro) -> None:
        loop = self._loop or self._running_loop()
        if loop is None:
            coro.close()
            return
        loop.create_task(coro)
