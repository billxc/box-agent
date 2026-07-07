"""The bus Subscriber protocol.

A subscriber is "something that receives a Packet for a topic, in order": the
sync entry the bus calls during its ordered fan-out. Concrete subscribers live
next to their consumers (events/bus.py's store + callback adapters, WebChannel's
per-chat queue adapter) — each is a tiny class that duck-types this protocol.

This module is a neutral leaf: it imports nothing project-internal.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

from boxagent.bus.message import Packet

logger = logging.getLogger(__name__)


@runtime_checkable
class Subscriber(Protocol):
    """The sync entry the bus calls during its ordered fan-out.

    `deliver` must be non-blocking and must not raise back into the bus's
    fan-out loop for control flow (the bus isolates exceptions, but a subscriber
    that blocks stalls the synchronous publish). Ephemeral consumers drop on a
    full queue; durable ones must not lose messages.
    """

    def deliver(self, packet: Packet) -> None: ...


class QueueSubscriber:
    """Ephemeral subscriber that forwards a topic's payloads to an asyncio.Queue.

    Shared by every "someone is watching this chat over SSE" consumer. Hands the
    browser an asyncio.Queue of raw event dicts (payload only). Drops on a full
    queue rather than blocking the synchronous bus fan-out.
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
    """Like QueueSubscriber, but stamps a fixed routing tag onto every payload.

    Used by the multiplexed chat stream: one WebSocket holds many chat
    subscriptions on a single shared queue, so each pushed event has to say which
    (machine, bot, chat_id) it belongs to for the browser to demux. That tag is
    known at subscribe time (it is the chat topic), so we merge it in here rather
    than re-parse the topic downstream. Emits ``{**tag, "event": payload}`` and
    drops on a full queue like its sibling.
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
