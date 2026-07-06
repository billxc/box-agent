"""跨机 chat —— 作为一根共享 MessageBus 上的 bridge。

本机 chat 由 WebChannel `publish` 到 ``chat.<machine>.<bot>.<chat_id>`` topic，浏览器
`bus.subscribe` 同一个 topic —— 本地/远端**同一个 publish/subscribe API**。ChatSyncer
是这根 bus 的一个公民，负责把本机 topic 的流量搭到 cluster WS 上、再把远端来的帧
重新 ``bus.publish`` 回本机：

    outbound  订 ``chat.`` 前缀 → 本机每次 publish 转发给下游 peer（owner→watcher / host 中继）
    demand    watch ``chat.`` 订阅 → 本机订了某个「远端拥有」的 chat 时，往上游发 chat_subscribe
    inbound   收到 chat_event 帧 → ``bus.publish`` 回本机 topic（本机 browser queue + 下游中继一次搞定）

一根 location-transparent 总线：订阅按 ``(owner_machine, bot, chat_id)`` 做 key，
「本机拥有」只是 ``owner == self`` 的特例（此时 WebChannel 直接 publish，bridge 只管
把它转发给下游 peer，无需 upstream）。hub-and-spoke 拓扑 → 最多两跳，由注入的
``route(owner_machine)`` 决定走向。

    {"type":"chat_subscribe",   "target_machine": M, "bot": b, "chat_id": c}
    {"type":"chat_unsubscribe", "target_machine": M, "bot": b, "chat_id": c}
    {"type":"chat_event",       "origin_machine": M, "bot": b, "chat_id": c, "event": {...}}

sync bus → async WS 的边界：`bus.publish` / `bus.subscribe` 是同步的，而 `ws.send_json`
是异步的。所有出站 peer 帧走**单条有序发送队列** ``_sendq`` + 一个 drain task —— FIFO
保证同一 chat 的帧不乱序（坑#1 禁止 create_task-per-event）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from boxagent.bus.core import MessageBus
from boxagent.bus.message import Message

from .peer_transport import PeerTransport, SendFrame

logger = logging.getLogger(__name__)

Route = Callable[[str], "str | None"]  # owner machine -> 能到达它的 peer_key

QUEUE_MAXSIZE = 1024

Key = tuple[str, str, str]  # (owner_machine, bot, chat_id)

CHAT_TOPIC_PREFIX = "chat."


class _OutboundBridge:
    """``chat.`` 前缀上的 bus subscriber：本机每次 chat publish 转发给下游 peer。"""

    def __init__(self, syncer: "ChatSyncer") -> None:
        self._syncer = syncer

    def deliver(self, message: Message) -> None:
        self._syncer._forward_downstream(message.topic, message.payload)


class ChatSyncer:
    def __init__(self, *, local_machine: str, route: Route, message_bus: MessageBus) -> None:
        self._local = local_machine
        self._route = route
        self._bus = message_bus
        self._transport = PeerTransport(log_prefix="chat")

        # 一个 key 有哪些下游 peer 在收（host 中继 / owner 给远端 watcher）。
        self._downstream: dict[Key, set[str]] = {}
        # 一个「远端拥有」的 key 有多少本机 bus 订阅（demand refcount）。
        self._local_subs: dict[Key, int] = {}
        # 已对哪些「远端拥有」的 key 发过 upstream chat_subscribe（refcount 边沿）。
        self._sources: set[Key] = set()

        # sync→async 出站队列（见模块 docstring）。
        self._sendq: "asyncio.Queue[tuple[str, dict]] | None" = None
        self._send_task: asyncio.Task | None = None

        # outbound + demand：把自己挂进 bus。
        self._bus.subscribe(CHAT_TOPIC_PREFIX, _OutboundBridge(self))
        self._bus.watch_subscriptions(
            CHAT_TOPIC_PREFIX, self._on_local_sub_added, self._on_local_sub_removed,
        )

    # peer 注册表在 shared transport 里；暴露 live dict 让 test harness 读到同一对象。
    @property
    def _peers(self) -> dict[str, SendFrame]:
        return self._transport._peers

    # ── 出站有序发送队列（sync→async 边界）──

    def _enqueue(self, peer_key: str, frame: dict) -> None:
        queue = self._ensure_send_task()
        if queue is None:
            return
        try:
            queue.put_nowait((peer_key, frame))
        except asyncio.QueueFull:
            logger.warning("chat: outbound send queue full; dropping frame to %s", peer_key)

    def _ensure_send_task(self) -> "asyncio.Queue | None":
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None  # 没有 loop（不该发生在生产路径）—— 丢弃而非崩
        if self._sendq is None:
            self._sendq = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        if self._send_task is None or self._send_task.done():
            self._send_task = loop.create_task(self._drain())
        return self._sendq

    async def _drain(self) -> None:
        assert self._sendq is not None
        while True:
            peer_key, frame = await self._sendq.get()
            await self._transport.send_to(peer_key, frame)

    # ── peer 生命周期 ──

    def attach_peer(self, peer_key: str, send_frame: SendFrame) -> None:
        self._transport.attach_peer(peer_key, send_frame)

    async def resubscribe(self, peer_key: str) -> None:
        """重连：把经此 peer 路由的 remote source 重发 chat_subscribe。"""
        for machine, bot, chat_id in list(self._sources):
            if self._route(machine) == peer_key:
                self._enqueue(peer_key, _subscribe(machine, bot, chat_id))

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
            self._refresh_source(key)

    # ── inbound 帧 ──

    async def handle_frame(self, peer_key: str, payload: dict) -> bool:
        kind = payload.get("type")
        if kind == "chat_subscribe":
            self._on_peer_subscribe(peer_key, payload, subscribed=True)
            return True
        if kind == "chat_unsubscribe":
            self._on_peer_subscribe(peer_key, payload, subscribed=False)
            return True
        if kind == "chat_event":
            key = _key(payload, "origin_machine")
            if key is not None:
                # 重新注入本机 bus：本机 browser queue + 下游中继（_OutboundBridge）一次搞定。
                self._bus.publish(_topic(key), payload.get("event") or {}, 0.0)
            return True
        return False

    def _on_peer_subscribe(self, peer_key: str, payload: dict, *, subscribed: bool) -> None:
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
        self._refresh_source(key)

    # ── outbound：把本机 publish 转发给下游 peer ──

    def _forward_downstream(self, topic: str, event: dict) -> None:
        key = _key_from_topic(topic)
        if key is None:
            return
        peers = self._downstream.get(key)
        if not peers:
            return
        frame = _event(key, event)
        for peer_key in list(peers):
            self._enqueue(peer_key, frame)

    # ── demand：本机 bus 订阅 add/remove（来自 watch_subscriptions）──

    def _on_local_sub_added(self, topic: str) -> None:
        key = _key_from_topic(topic)
        if key is None or key[0] == self._local:
            return  # 本机拥有的 chat：WebChannel 直接 publish，无需 upstream
        self._local_subs[key] = self._local_subs.get(key, 0) + 1
        self._refresh_source(key)

    def _on_local_sub_removed(self, topic: str) -> None:
        key = _key_from_topic(topic)
        if key is None or key[0] == self._local:
            return
        remaining = self._local_subs.get(key, 0) - 1
        if remaining <= 0:
            self._local_subs.pop(key, None)
        else:
            self._local_subs[key] = remaining
        self._refresh_source(key)

    # ── source：某「远端拥有」的 key 要不要往上游订，refcount 边沿 ──

    def _refresh_source(self, key: Key) -> None:
        if key[0] == self._local:
            return  # 本机拥有：没有 upstream 的概念
        want = self._local_subs.get(key, 0) > 0 or bool(self._downstream.get(key))
        active = key in self._sources
        machine, bot, chat_id = key
        if want and not active:
            self._sources.add(key)
            self._send_toward(machine, _subscribe(machine, bot, chat_id))
        elif not want and active:
            self._sources.discard(key)
            self._send_toward(machine, _unsubscribe(machine, bot, chat_id))

    # ── 发送 helper ──

    def _send_toward(self, machine: str, frame: dict) -> None:
        peer_key = self._route(machine)
        if peer_key is not None:
            self._enqueue(peer_key, frame)


# ── topic / 帧 构造 / 解析 ──

def _topic(key: Key) -> str:
    machine, bot, chat_id = key
    return f"{CHAT_TOPIC_PREFIX}{machine}.{bot}.{chat_id}"


def _key_from_topic(topic: str) -> Key | None:
    if not topic.startswith(CHAT_TOPIC_PREFIX):
        return None
    rest = topic[len(CHAT_TOPIC_PREFIX):]
    parts = rest.split(".", 2)  # machine.bot.chat_id —— chat_id 可能含 "."
    if len(parts) != 3 or not all(parts):
        return None
    return (parts[0], parts[1], parts[2])


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
