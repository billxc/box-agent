"""ClusterBus — the cross-machine Bus implementation.

A `ClusterBus` is a `MessageBus` (in-process fan-out) that ALSO forwards packets
across machines over WebSocket links. Same `send()` / `subscribe()` surface as
the local bus — callers never branch on local vs remote; `receiver` decides.

It subsumes what the three old cross-machine mechanisms (chat sync / event sync /
rpc) each did separately: one `_forward` moves every packet, one wire frame
(`{"v", "packet"}`), one version gate. Hub-and-spoke topology (guests connect only
to the host) makes routing trivial and loop-free:

  - guest: one link (the host). `route(any machine) = host-link`.
  - host:  one link per guest. `route(guest_X) = X's link`; `route(self) = None`.

The host is therefore the only relay + broadcast fan-out point; a guest just sends
to / receives from the host. Because the topology is a tree, `from_link` exclusion
alone prevents loops — no seen-set needed (message_id is kept for trace only).
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Awaitable, Callable

from boxagent.bus.core import MessageBus
from boxagent.bus.message import Packet

logger = logging.getLogger(__name__)

# Cluster wire-protocol version. Bumped from the old mechanisms' 2 → 3 for the
# hard-cut: missing/mismatched version is DROPPED (not accepted). The whole fleet
# upgrades in lockstep (big-bang restart).
WIRE_VERSION = 3

QUEUE_MAXSIZE = 1024

SendFrame = Callable[[dict], Awaitable[None]]   # a link's async WS send
Route = Callable[[str], "str | None"]           # machine id -> link key (or None)


class ClusterBus(MessageBus):
    """In-process bus + cross-machine forwarding. Drop-in for MessageBus."""

    def __init__(
        self,
        *,
        machine_id: str,
        route: Route,
        on_unreachable: "Callable[[str], None] | None" = None,
        id_factory: "Callable[[], str] | None" = None,
    ) -> None:
        super().__init__(machine_id=machine_id, id_factory=id_factory)
        self._route = route
        # Fired with a target machine id when a point-to-point packet cannot be
        # delivered (no link / link down / version-incompatible). The request/reply
        # helper listens and fails its pending requests to that machine fast, so a
        # caller does not hang the full timeout.
        self._on_unreachable = on_unreachable
        self._links: dict[str, SendFrame] = {}
        self._link_version: dict[str, int] = {}
        # sync send() → async ws.send: a single ordered queue + drain task, so
        # send() returns immediately and same-link frames never reorder (坑#1).
        self._sendq: "asyncio.Queue[tuple[str, dict]] | None" = None
        self._send_task: "asyncio.Task | None" = None

    # ── link lifecycle ─────────────────────────────────────────────────────

    def attach_link(self, link_key: str, send_frame: SendFrame, *, version: int = WIRE_VERSION) -> None:
        """Register (or replace) a link and the version negotiated at handshake."""
        self._links[link_key] = send_frame
        self._link_version[link_key] = version

    def detach_link(self, link_key: str) -> None:
        self._links.pop(link_key, None)
        self._link_version.pop(link_key, None)

    def link_keys(self) -> list[str]:
        return list(self._links)

    # ── Bus.send (override) ────────────────────────────────────────────────

    def send(self, *, receiver: str, topic: str, payload: dict, ts: float) -> str:
        packet = Packet(
            message_id=self._id_factory(),
            sender=self._machine_id,
            receiver=receiver,
            topic=topic,
            payload=payload,
            ts=ts,
        )
        self._forward(packet, from_link=None)
        return packet.message_id

    # ── inbound (WS read loop calls this for `packet` frames) ──────────────

    def on_inbound(self, from_link: str, frame: dict) -> None:
        """Handle one inbound wire frame `{"v", "packet"}` from `from_link`.

        Version gate (hard-cut): a frame whose `v` is missing or != ours is
        dropped, not misparsed. Then reconstruct the Packet and forward it."""
        if frame.get("v") != WIRE_VERSION:
            logger.warning(
                "cluster_bus: drop frame from %s: wire version %r (this node speaks %d)",
                from_link, frame.get("v"), WIRE_VERSION,
            )
            return
        raw = frame.get("packet") or {}
        packet = Packet(
            message_id=str(raw.get("message_id") or ""),
            sender=str(raw.get("sender") or ""),
            receiver=str(raw.get("receiver") or ""),
            topic=str(raw.get("topic") or ""),
            payload=raw.get("payload") or {},
            ts=float(raw.get("ts") or 0.0),
        )
        self._forward(packet, from_link=from_link)

    # ── the one forward (outbound = from_link None; inbound = the source link)

    def _forward(self, packet: Packet, from_link: "str | None") -> None:
        # 1. is it for me? → local fan-out (inherited from MessageBus)
        if packet.receiver in ("", self._machine_id):
            self._deliver_local(packet)
        # 2. forward onward
        if packet.receiver == "":                         # broadcast → all other links
            for link_key in list(self._links):
                if link_key != from_link:
                    self._enqueue(link_key, packet)
        elif packet.receiver != self._machine_id:           # point-to-point to another machine
            link_key = self._route(packet.receiver)
            if (
                link_key is not None
                and link_key != from_link
                and self._usable(link_key)
            ):
                self._enqueue(link_key, packet)
            else:
                self._signal_unreachable(packet.receiver)

    def _usable(self, link_key: str) -> bool:
        return link_key in self._links and self._link_version.get(link_key) == WIRE_VERSION

    def _signal_unreachable(self, machine: str) -> None:
        if self._on_unreachable is None:
            return
        try:
            self._on_unreachable(machine)
        except Exception:
            logger.warning("cluster_bus: on_unreachable(%s) raised", machine, exc_info=True)

    # ── sync→async send queue ──────────────────────────────────────────────

    def _enqueue(self, link_key: str, packet: Packet) -> None:
        queue = self._ensure_send_task()
        if queue is None:
            return
        frame = {"v": WIRE_VERSION, "packet": dataclasses.asdict(packet)}
        try:
            queue.put_nowait((link_key, frame))
        except asyncio.QueueFull:
            logger.warning("cluster_bus: send queue full; dropping frame to %s", link_key)

    def _ensure_send_task(self) -> "asyncio.Queue | None":
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None  # no loop (should not happen on the production path) — drop, don't crash
        if self._sendq is None:
            self._sendq = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        if self._send_task is None or self._send_task.done():
            self._send_task = loop.create_task(self._drain())
        return self._sendq

    async def _drain(self) -> None:
        assert self._sendq is not None
        while True:
            link_key, frame = await self._sendq.get()
            send = self._links.get(link_key)
            if send is None:
                continue
            try:
                await send(frame)
            except Exception as exception:
                logger.warning("cluster_bus: send to %s failed: %r", link_key, exception)

    async def aclose(self) -> None:
        if self._send_task is not None and not self._send_task.done():
            self._send_task.cancel()
            try:
                await self._send_task
            except (asyncio.CancelledError, Exception):
                pass
        self._send_task = None
