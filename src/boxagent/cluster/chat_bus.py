"""location-transparent chat 订阅门面。

SSE handler 订阅某 ``machine`` 上的 ``(bot, chat_id)``，拿回一个普通 ``asyncio.Queue``
（元素是 event dict）—— 本地还是远端**同一条路径**：都是 ``bus.subscribe`` 一个
``chat.<owner>.<bot>.<chat_id>`` topic：

    local  owner → 该 bot 的 WebChannel 会 publish 到这个 topic（进程内 fan-out）
    remote owner → ChatSyncer 的 demand observer 看到这个订阅，往上游发 chat_subscribe；
                   远端来的 chat_event 帧被 ChatSyncer 重新 publish 回同一 topic

没有本地/远端分叉，没有 owner-side pump —— 那些都塌进了 ChatSyncer（一根 bus 上的
bridge）。本门面只负责建 queue、退订、shutdown 时给在看的 queue 发 ``_close``。
"""
from __future__ import annotations

import asyncio
from typing import Callable

from boxagent.bus.core import MessageBus, Subscription
from boxagent.bus.subscriber import QueueSubscriber

from .chat_sync import QUEUE_MAXSIZE, _topic

# bot_name -> 本机 WebChannel（或 None）—— 只用来判断本机 bot 是否 web-enabled。
ChannelFor = Callable[[str], "object | None"]


class ChatBus:
    def __init__(self, *, local_machine: str, message_bus: MessageBus, channel_for: ChannelFor) -> None:
        self._local = local_machine
        self._bus = message_bus
        self._channel_for = channel_for
        self._subscriptions: dict[asyncio.Queue, Subscription] = {}

    def _is_local(self, machine: str | None) -> bool:
        return machine is None or machine == self._local

    async def subscribe(self, bot: str, chat_id: str, machine: str | None = None) -> asyncio.Queue | None:
        owner = machine or self._local
        # 本机 bot 不存在/未启用 web → None（→ 404）。远端 bot 不在本机校验范围内。
        if self._is_local(machine) and self._channel_for(bot) is None:
            return None
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        subscription = self._bus.subscribe(
            _topic((owner, bot, chat_id)), QueueSubscriber(queue, f"{owner}/{bot}/{chat_id}"),
        )
        self._subscriptions[queue] = subscription
        return queue

    async def unsubscribe(self, bot: str, chat_id: str, machine: str | None, queue: asyncio.Queue) -> None:
        subscription = self._subscriptions.pop(queue, None)
        if subscription is not None:
            subscription.close()  # 触发 ChatSyncer demand observer（refcount 归 0 → 上游退订）

    async def aclose(self) -> None:
        for queue, subscription in list(self._subscriptions.items()):
            try:
                queue.put_nowait({"type": "_close"})
            except asyncio.QueueFull:
                pass
            subscription.close()
        self._subscriptions.clear()
