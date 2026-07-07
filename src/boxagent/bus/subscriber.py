"""总线 Subscriber 协议。

订阅者是"按 topic 有序接收 Packet 的东西"：总线在有序 fan-out 时调用的同步入口。
具体订阅者放在各自消费者旁边（events/bus.py 的 store + callback 适配器、WebChannel
的 per-chat 队列适配器）——每个都是 duck-type 此协议的小类。

本模块是中立叶子：不 import 任何项目内部代码。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

from boxagent.bus.message import Packet

logger = logging.getLogger(__name__)


@runtime_checkable
class Subscriber(Protocol):
    """总线在有序 fan-out 时调用的同步入口。

    `deliver` 必须非阻塞，且不能为控制流把异常抛回总线的 fan-out 循环（总线会隔离
    异常，但阻塞的订阅者会拖住同步 publish）。临时消费者在队列满时丢弃；持久的不能
    丢消息。
    """

    def deliver(self, packet: Packet) -> None: ...


class QueueSubscriber:
    """临时订阅者：把某 topic 的 payload 转发到 asyncio.Queue。

    每个"有人正经 SSE 看这个 chat"的消费者共用。给浏览器一个原始事件 dict 队列
    （只含 payload）。队列满时丢弃，而非阻塞同步的总线 fan-out。
    """

    def __init__(self, queue: asyncio.Queue, label: str = "") -> None:
        self._queue = queue
        self._label = label

    def deliver(self, packet: Packet) -> None:
        try:
            self._queue.put_nowait(packet.payload)
        except asyncio.QueueFull:
            logger.warning("bus queue subscriber full (%s); dropping event", self._label)


class TaggedQueueSubscriber:
    """类似 QueueSubscriber，但给每个 payload 盖上固定的路由 tag。

    多路复用 chat 流用它：一个 WebSocket 在共享队列上持有多个 chat 订阅，故每条推送
    的事件必须标明属于哪个 (machine, bot, chat_id)，浏览器才能解复用。该 tag 在
    subscribe 时已知（即 chat topic），故在此合并，而非下游重新解析 topic。发出
    ``{**tag, "event": payload}``，队列满时像兄弟类一样丢弃。
    """

    def __init__(self, queue: asyncio.Queue, tag: dict, label: str = "") -> None:
        self._queue = queue
        self._tag = tag
        self._label = label

    def deliver(self, packet: Packet) -> None:
        try:
            self._queue.put_nowait({**self._tag, "event": packet.payload})
        except asyncio.QueueFull:
            logger.warning("bus tagged queue subscriber full (%s); dropping event", self._label)
