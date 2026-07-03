"""跨机 chat pub/sub，走 cluster WebSocket。

同机 chat 由 WebChannel 的 per-chat queue 在进程内 fan-out；本模块负责跨机那半，
把事件当结构化 dict 走既有 cluster WS（同 events/sync.py）—— 不再序列化成 SSE。
订阅式：一个节点只收浏览器正在看的 (bot, chat_id) 的事件。

    {"type":"chat_subscribe",   "target_machine": M, "bot": b, "chat_id": c}
    {"type":"chat_unsubscribe", "target_machine": M, "bot": b, "chat_id": c}
    {"type":"chat_event",       "origin_machine": M, "bot": b, "chat_id": c, "event": {...}}

一根 location-transparent 总线：每个订阅按 ``(owner_machine, bot, chat_id)`` 做 key，
"本机拥有"只是 ``machine == self`` 的特例。所以 owner 和 host-relay 共用 `_deliver`，
pump-local 和 subscribe-upstream 共用 `_toggle_source`。hub-and-spoke 拓扑 → 最多两跳，
由注入的 ``route(owner_machine)`` 决定走向。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .peer_transport import PeerTransport, SendFrame

logger = logging.getLogger(__name__)

Route = Callable[[str], "str | None"]  # owner machine -> 能到达它的 peer_key

QUEUE_MAXSIZE = 1024

Key = tuple[str, str, str]  # (owner_machine, bot, chat_id)


class ChatSyncer:
    def __init__(self, *, local_machine: str, route: Route) -> None:
        self._local = local_machine
        self._route = route
        self._transport = PeerTransport(log_prefix="chat")

        # 一个 key 的事件发给谁：下游 peer（host 中继 / owner 给远端 watcher）
        # + 本机浏览器 queue。
        self._downstream: dict[Key, set[str]] = {}
        self._queues: dict[Key, set[asyncio.Queue]] = {}
        # 每个 key 一个 source —— machine==self 时 pump 本地 WebChannel，否则往
        # 上游发 chat_subscribe —— 由 _downstream ∪ _queues refcount。
        self._sources: set[Key] = set()

        # 本机拥有的 chat 的 demand 边沿 → wiring 去 pump 该 bot 的 WebChannel
        # 喂给 on_local_publish。可设 hook；None = no-op。
        self.on_local_demand: "Callable[[str, str, bool], None] | None" = None

    # peer 注册表在 shared transport 里；暴露 live dict 让 test harness 读到同一对象。
    @property
    def _peers(self) -> dict[str, SendFrame]:
        return self._transport._peers

    # ── peer 生命周期 ──

    def attach_peer(self, peer_key: str, send_frame: SendFrame) -> None:
        self._transport.attach_peer(peer_key, send_frame)

    async def resubscribe(self, peer_key: str) -> None:
        """重连：把经此 peer 路由的 remote source 重发 chat_subscribe。"""
        for machine, bot, chat_id in list(self._sources):
            if machine != self._local and self._route(machine) == peer_key:
                await self._send_to(peer_key, _subscribe(machine, bot, chat_id))

    async def detach_peer(self, peer_key: str) -> None:
        self._transport.detach_peer(peer_key)
        # 把 peer 从它订的每个 key 摘掉；空了的 source 一并释放。
        emptied = []
        for key, peers in list(self._downstream.items()):
            if peer_key in peers:
                peers.discard(peer_key)
                if not peers:
                    del self._downstream[key]
                emptied.append(key)
        for key in emptied:
            await self._refresh_source(key)

    # ── 本机浏览器订阅一个（可能是远端的）chat ──

    async def remote_subscribe(self, machine: str, bot: str, chat_id: str) -> asyncio.Queue:
        key = (machine, bot, chat_id)
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._queues.setdefault(key, set()).add(queue)
        await self._refresh_source(key)
        return queue

    async def remote_unsubscribe(self, machine: str, bot: str, chat_id: str, queue: asyncio.Queue) -> None:
        key = (machine, bot, chat_id)
        queues = self._queues.get(key)
        if queues:
            queues.discard(queue)
            if not queues:
                del self._queues[key]
        await self._refresh_source(key)

    # ── 本机拥有的 bot 发出事件 ──

    async def on_local_publish(self, bot: str, chat_id: str, event: dict) -> None:
        await self._deliver((self._local, bot, chat_id), event)

    # ── 入站帧 ──

    async def handle_frame(self, peer_key: str, payload: dict) -> bool:
        kind = payload.get("type")
        if kind == "chat_subscribe":
            await self._on_subscribe(peer_key, payload, subscribed=True)
            return True
        if kind == "chat_unsubscribe":
            await self._on_subscribe(peer_key, payload, subscribed=False)
            return True
        if kind == "chat_event":
            key = _key(payload, "origin_machine")
            if key is not None:
                await self._deliver(key, payload.get("event") or {})
            return True
        return False

    async def _on_subscribe(self, peer_key: str, payload: dict, *, subscribed: bool) -> None:
        key = _key(payload, "target_machine")
        if key is None:
            return
        if subscribed:
            self._downstream.setdefault(key, set()).add(peer_key)
        else:
            peers = self._downstream.get(key)
            if peers:
                peers.discard(peer_key)
                if not peers:
                    del self._downstream[key]
        await self._refresh_source(key)

    # ── 分发：把一个事件 fan 给某 key 的订阅者 ──

    async def _deliver(self, key: Key, event: dict) -> None:
        for queue in self._queues.get(key, ()):  # 本机浏览器
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("chat: subscriber queue full (%s); dropping event", key)
        if self._downstream.get(key):  # 中继给下游 peer
            frame = _event(key, event)
            for peer_key in list(self._downstream[key]):
                await self._send_to(peer_key, frame)

    # ── source：喂某 key 的上游，每 key 一个，refcount ──

    async def _refresh_source(self, key: Key) -> None:
        want = bool(self._queues.get(key)) or bool(self._downstream.get(key))
        active = key in self._sources
        if want and not active:
            self._sources.add(key)
            await self._toggle_source(key, active=True)
        elif not want and active:
            self._sources.discard(key)
            await self._toggle_source(key, active=False)

    async def _toggle_source(self, key: Key, *, active: bool) -> None:
        machine, bot, chat_id = key
        if machine == self._local:
            self._fire_demand(bot, chat_id, active)  # 我拥有 → pump 本地 channel
        else:
            frame = _subscribe(machine, bot, chat_id) if active else _unsubscribe(machine, bot, chat_id)
            await self._send_toward(machine, frame)  # 否则 → 往上游 (un)subscribe

    def _fire_demand(self, bot: str, chat_id: str, active: bool) -> None:
        callback = self.on_local_demand
        if callback is None:
            return
        try:
            callback(bot, chat_id, active)
        except Exception as exception:
            logger.warning("chat: on_local_demand(%s, %s, %s) failed: %r",
                           bot, chat_id, active, exception)

    # ── 发送 helper ──

    async def _send_toward(self, machine: str, frame: dict) -> None:
        peer_key = self._route(machine)
        if peer_key is not None:
            await self._send_to(peer_key, frame)

    async def _send_to(self, peer_key: str, frame: dict) -> None:
        await self._transport.send_to(peer_key, frame)


# ── 帧构造 / 解析 ──

def _key(payload: dict, machine_field: str) -> Key | None:
    machine = str(payload.get(machine_field) or "")
    bot = str(payload.get("bot") or "")
    chat_id = str(payload.get("chat_id") or "")
    if not machine or not bot or not chat_id:
        return None
    return (machine, bot, chat_id)


def _subscribe(machine: str, bot: str, chat_id: str) -> dict:
    return {"type": "chat_subscribe", "target_machine": machine, "bot": bot, "chat_id": chat_id}


def _unsubscribe(machine: str, bot: str, chat_id: str) -> dict:
    return {"type": "chat_unsubscribe", "target_machine": machine, "bot": bot, "chat_id": chat_id}


def _event(key: Key, event: dict) -> dict:
    machine, bot, chat_id = key
    return {"type": "chat_event", "origin_machine": machine, "bot": bot, "chat_id": chat_id, "event": event}
