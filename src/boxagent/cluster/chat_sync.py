"""Cross-machine chat pub/sub over the cluster WebSocket.

The chat *message stream* (browser ← bot: message / stream_delta / tool_call /
…) is delivered same-machine by an in-process fan-out (WebChannel's per-chat
queues). This module carries the *cross-machine* case with the same shape the
EventStore already uses (see ``events/sync.py``): structured event dicts over
the existing cluster WS — NOT re-serialized SSE.

Unlike the EventStore (full replication), chat is **subscription-based**: a node
only receives events for the ``(bot, chat_id)`` that one of its browsers is
actively viewing. Wire frames (alongside the RPC envelope + event_* frames):

    {"type":"chat_subscribe",   "target_machine": M, "bot": b, "chat_id": c}
    {"type":"chat_unsubscribe", "target_machine": M, "bot": b, "chat_id": c}
    {"type":"chat_event",       "origin_machine": M, "bot": b, "chat_id": c, "event": {...}}

Topology is hub-and-spoke, so routing is at most two hops:

    guest A ──▶ host ──▶ guest B        (A watches B's bot)
    guest A ──▶ host                    (A watches host's bot)
    host    ──▶ guest B                 (host watches B's bot)

`ChatSyncer` is role-agnostic. The host attaches one peer per connected guest
(peer_key = machine_id); a guest attaches a single peer ("host"). Which peer a
subscribe travels toward is decided by the injected ``route(target)`` (guest →
"host"; host → the target's session key). All outbound sends go through the
injected per-peer ``send_frame`` coroutine, so the syncer is unit-testable with
fake peers (see test_chat_sync.py).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

SendFrame = Callable[[dict], Awaitable[None]]
Route = Callable[[str], "str | None"]  # target_machine -> peer_key that reaches it

QUEUE_MAXSIZE = 1024


class ChatSyncer:
    def __init__(self, *, local_machine: str, route: Route) -> None:
        self._local = local_machine
        self._route = route
        self._peers: dict[str, SendFrame] = {}

        # Owner-side demand edge: fired (bot, chat_id, active) when the first
        # remote peer starts watching a locally-owned chat (active=True) and
        # when the last one leaves (active=False). The wiring turns this into a
        # WebChannel subscription + ordered pump feeding on_local_publish, so
        # remote delivery reuses the same in-process fan-out (and stays ordered)
        # rather than a create_task per event. Settable hook; None = no-op.
        self.on_local_demand: "Callable[[str, str, bool], None] | None" = None

        # Owner side: peers that want events for a LOCALLY-owned (bot, chat_id).
        self._local_subs: dict[tuple[str, str], set[str]] = {}
        # Subscriber side: my browser queues waiting on (origin_machine, bot, chat_id).
        self._queues: dict[tuple[str, str, str], set[asyncio.Queue]] = {}
        # Host relay: downstream peers that want (target_machine, bot, chat_id).
        self._relay: dict[tuple[str, str, str], set[str]] = {}
        # Keys we've sent a chat_subscribe upstream for (dedup + reconnect resend).
        self._upstream: set[tuple[str, str, str]] = set()

    # ── peer lifecycle ──

    def attach_peer(self, peer_key: str, send_frame: SendFrame) -> None:
        self._peers[peer_key] = send_frame

    async def resubscribe(self, peer_key: str) -> None:
        """(Re)connect: resend chat_subscribe for every upstream key that routes
        through this peer (mirrors EventSyncer's resync-on-attach)."""
        for target, bot, chat_id in list(self._upstream):
            if self._route(target) == peer_key:
                await self._send_to(peer_key, {
                    "type": "chat_subscribe", "target_machine": target,
                    "bot": bot, "chat_id": chat_id,
                })

    async def detach_peer(self, peer_key: str) -> None:
        self._peers.pop(peer_key, None)
        # Drop the peer from owner-side subs (it can't receive anymore); a
        # now-empty (bot, chat_id) releases the local feed.
        emptied: list[tuple[str, str]] = []
        for key, peers in list(self._local_subs.items()):
            if peer_key in peers:
                peers.discard(peer_key)
                if not peers:
                    del self._local_subs[key]
                    emptied.append(key)
        for bot, chat_id in emptied:
            self._fire_demand(bot, chat_id, False)
        # Drop from relay; a now-empty relay may release an upstream sub.
        affected = [key for key, peers in self._relay.items() if peer_key in peers]
        for key in affected:
            self._relay[key].discard(peer_key)
            if not self._relay[key]:
                del self._relay[key]
            await self._refresh_upstream(*key)

    # ── subscriber side (called by ChatBus for a local browser watching a
    #    remote bot) ──

    async def remote_subscribe(self, machine: str, bot: str, chat_id: str) -> asyncio.Queue:
        key = (machine, bot, chat_id)
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._queues.setdefault(key, set()).add(q)
        await self._refresh_upstream(*key)
        return q

    async def remote_unsubscribe(self, machine: str, bot: str, chat_id: str, q: asyncio.Queue) -> None:
        key = (machine, bot, chat_id)
        queues = self._queues.get(key)
        if queues:
            queues.discard(q)
            if not queues:
                del self._queues[key]
        await self._refresh_upstream(*key)

    # ── owner side (called when a locally-owned bot publishes an event) ──

    async def on_local_publish(self, bot: str, chat_id: str, event: dict) -> None:
        peers = self._local_subs.get((bot, chat_id))
        if not peers:
            return
        frame = {
            "type": "chat_event", "origin_machine": self._local,
            "bot": bot, "chat_id": chat_id, "event": event,
        }
        for peer_key in list(peers):
            await self._send_to(peer_key, frame)

    # ── inbound frames ──

    async def handle_frame(self, peer_key: str, payload: dict) -> bool:
        kind = payload.get("type")
        if kind == "chat_subscribe":
            await self._on_subscribe(peer_key, payload)
            return True
        if kind == "chat_unsubscribe":
            await self._on_unsubscribe(peer_key, payload)
            return True
        if kind == "chat_event":
            await self._on_event(peer_key, payload)
            return True
        return False

    async def _on_subscribe(self, peer_key: str, payload: dict) -> None:
        target = str(payload.get("target_machine") or "")
        bot = str(payload.get("bot") or "")
        chat_id = str(payload.get("chat_id") or "")
        if not target or not bot or not chat_id:
            return
        if target == self._local:
            peers = self._local_subs.setdefault((bot, chat_id), set())  # I own it
            was_empty = not peers
            peers.add(peer_key)
            if was_empty:
                self._fire_demand(bot, chat_id, True)
        else:
            self._relay.setdefault((target, bot, chat_id), set()).add(peer_key)  # host relay
            await self._refresh_upstream(target, bot, chat_id)

    async def _on_unsubscribe(self, peer_key: str, payload: dict) -> None:
        target = str(payload.get("target_machine") or "")
        bot = str(payload.get("bot") or "")
        chat_id = str(payload.get("chat_id") or "")
        if target == self._local:
            peers = self._local_subs.get((bot, chat_id))
            if peers:
                peers.discard(peer_key)
                if not peers:
                    del self._local_subs[(bot, chat_id)]
                    self._fire_demand(bot, chat_id, False)
        else:
            key = (target, bot, chat_id)
            peers = self._relay.get(key)
            if peers:
                peers.discard(peer_key)
                if not peers:
                    del self._relay[key]
                await self._refresh_upstream(*key)

    async def _on_event(self, peer_key: str, payload: dict) -> None:
        origin = str(payload.get("origin_machine") or "")
        bot = str(payload.get("bot") or "")
        chat_id = str(payload.get("chat_id") or "")
        event = payload.get("event") or {}
        key = (origin, bot, chat_id)
        # Deliver to my own browser queues.
        for q in self._queues.get(key, ()):  # copy not needed: no mutation here
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("chat: subscriber queue full (%s); dropping event", key)
        # Relay to downstream peers (host forwarding back toward guests).
        for peer in list(self._relay.get(key, ())):
            await self._send_to(peer, {
                "type": "chat_event", "origin_machine": origin,
                "bot": bot, "chat_id": chat_id, "event": event,
            })

    # ── upstream refcount ──
    # We hold at most one upstream subscription per (target, bot, chat_id),
    # shared by all local browser queues + all relayed downstream peers. When
    # the last of those goes away, we release it.

    async def _refresh_upstream(self, target: str, bot: str, chat_id: str) -> None:
        key = (target, bot, chat_id)
        want = bool(self._queues.get(key)) or bool(self._relay.get(key))
        if want and key not in self._upstream:
            self._upstream.add(key)
            await self._send_toward(target, {
                "type": "chat_subscribe", "target_machine": target,
                "bot": bot, "chat_id": chat_id,
            })
        elif not want and key in self._upstream:
            self._upstream.discard(key)
            await self._send_toward(target, {
                "type": "chat_unsubscribe", "target_machine": target,
                "bot": bot, "chat_id": chat_id,
            })

    # ── send helpers ──

    def _fire_demand(self, bot: str, chat_id: str, active: bool) -> None:
        callback = self.on_local_demand
        if callback is None:
            return
        try:
            callback(bot, chat_id, active)
        except Exception as exception:
            logger.warning("chat: on_local_demand(%s, %s, %s) failed: %r",
                           bot, chat_id, active, exception)

    async def _send_toward(self, target: str, frame: dict) -> None:
        peer_key = self._route(target)
        if peer_key is not None:
            await self._send_to(peer_key, frame)

    async def _send_to(self, peer_key: str, frame: dict) -> None:
        send = self._peers.get(peer_key)
        if send is None:
            return
        try:
            await send(frame)
        except Exception as exception:
            logger.warning("chat: send to %s failed: %r", peer_key, exception)
