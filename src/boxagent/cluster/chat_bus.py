"""location-transparent chat 订阅门面。

调用方订阅某 ``machine`` 上的 ``(bot, chat_id)``，拿回一个普通 ``asyncio.Queue``
（元素是 event dict）—— 本地还是远端形状一致，于是 web server 的 SSE handler
不再按 machine 分叉：

    local  machine → 该 bot 的进程内 WebChannel queue（不变）
    remote machine → ChatSyncer.remote_subscribe（结构化帧走 cluster WS）

owner 侧：远端 peer 开始看我本机 bot 时，ChatSyncer fire ``on_local_demand``；
我们跑一个 per-chat pump，把该 bot 的 WebChannel 事件经 ``on_local_publish`` 转发 ——
顺序、复用同一份进程内 fan-out，不用 create_task-per-event（避免乱序）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .chat_sync import ChatSyncer

logger = logging.getLogger(__name__)

# bot_name -> 本机 WebChannel（duck-typed subscribe/unsubscribe）或 None，
# 这样 cluster/ 不必 import transport 层。
ChannelFor = Callable[[str], "object | None"]


class ChatBus:
    def __init__(self, *, local_machine: str, syncer: ChatSyncer, channel_for: ChannelFor) -> None:
        self._local = local_machine
        self._syncer = syncer
        self._channel_for = channel_for
        self._pumps: dict[tuple[str, str], asyncio.Task] = {}
        syncer.on_local_demand = self._on_local_demand

    def _is_local(self, machine: str | None) -> bool:
        return machine is None or machine == self._local

    # ── 订阅（browser ← bot stream）──

    async def subscribe(self, bot: str, chat_id: str, machine: str | None = None) -> asyncio.Queue | None:
        if self._is_local(machine):
            channel = self._channel_for(bot)
            return channel.subscribe(chat_id) if channel is not None else None
        return await self._syncer.remote_subscribe(machine, bot, chat_id)

    async def unsubscribe(self, bot: str, chat_id: str, machine: str | None, queue: asyncio.Queue) -> None:
        if self._is_local(machine):
            channel = self._channel_for(bot)
            if channel is not None:
                channel.unsubscribe(chat_id, queue)
            return
        await self._syncer.remote_unsubscribe(machine, bot, chat_id, queue)

    # ── owner 侧 pump：把本机拥有的 bot 的事件喂给远端 peer ──

    def _on_local_demand(self, bot: str, chat_id: str, active: bool) -> None:
        key = (bot, chat_id)
        if active:
            if key not in self._pumps:
                self._pumps[key] = asyncio.create_task(self._pump(bot, chat_id))
        else:
            task = self._pumps.pop(key, None)
            if task is not None:
                task.cancel()

    async def _pump(self, bot: str, chat_id: str) -> None:
        channel = self._channel_for(bot)
        if channel is None:
            return
        queue = channel.subscribe(chat_id)
        try:
            while True:
                event = await queue.get()
                await self._syncer.on_local_publish(bot, chat_id, event)
        except asyncio.CancelledError:
            pass
        except Exception as exception:
            logger.warning("chat: owner pump (%s, %s) crashed: %r", bot, chat_id, exception)
        finally:
            channel.unsubscribe(chat_id, queue)

    async def aclose(self) -> None:
        for task in list(self._pumps.values()):
            task.cancel()
        self._pumps.clear()
