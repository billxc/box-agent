"""Location-transparent chat subscription façade.

Before ChatBus, the web server special-cased every handler: `if machine ==
local: use the in-process WebChannel; else: proxy an SSE request over the
cluster WS and re-frame the `data:` lines by hand`. ChatBus removes that fork.
A caller asks to subscribe to ``(bot, chat_id)`` on some ``machine`` and gets
back a plain ``asyncio.Queue`` of event dicts — identical shape whether the bot
lives here or three hops away. Under the hood:

    local  machine → the bot's in-process WebChannel queue (unchanged)
    remote machine → ChatSyncer.remote_subscribe (structured frames over the WS)

The owner side is bridged by a **pump**: when a remote peer starts watching one
of our local bots, ChatSyncer fires ``on_local_demand(bot, chat_id, True)``,
which subscribes to that bot's WebChannel and runs a single per-chat task
forwarding each event through ``ChatSyncer.on_local_publish`` — in order, reusing
the same in-process fan-out browsers use, with no create_task-per-event race.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .chat_sync import ChatSyncer

logger = logging.getLogger(__name__)

# bot_name -> the local WebChannel for that bot (or None if not hosted here).
# WebChannel is duck-typed here (subscribe/unsubscribe/inject) to avoid importing
# the transport layer into cluster/.
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

    # ── subscription (browser ← bot stream) ──

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

    # ── owner-side pump: feed a locally-owned bot's events to remote peers ──

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

    # ── send path (browser → bot) ──
    # Local injection reuses the WebChannel echo+dispatch path. Cross-machine
    # send still rides the existing POST proxy (dispatch_machine_request), so the
    # bus only owns the local case here.

    async def inject(self, bot: str, chat_id: str, text: str, machine: str | None = None) -> bool:
        if not self._is_local(machine):
            raise NotImplementedError("cross-machine inject rides the POST proxy, not ChatBus")
        channel = self._channel_for(bot)
        if channel is None:
            return False
        await channel.inject(chat_id, text)
        return True
