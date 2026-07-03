"""Subscriber abstractions for the bus.

A subscriber is "something that receives a Message for a topic, in order."

- `LocalSubscriber` wraps an in-process `asyncio.Queue`. `deliver()` is a
  non-blocking `put_nowait`; when the queue is full it DROPS the message and
  warns (ephemeral consumers — browser SSE, /events page — tolerate loss).

- `RemoteSubscriber` forwards each Message over a cluster link. It holds a
  bounded queue plus exactly ONE pump task that awaits `send(message)` per
  item. The single pump + `put_nowait` preserves per-subscriber order (坑 #1:
  NEVER create_task per message — that reorders stream_delta / event batches).
  Drop-on-full for the bounded queue; per-peer backpressure by bounding it.

This module is a neutral leaf: it imports nothing project-internal.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Protocol, runtime_checkable

from boxagent.bus.message import Message

logger = logging.getLogger(__name__)

# Bounded queue depth for a RemoteSubscriber's outbound link. Beyond this the
# link is treated as too slow and further messages are dropped (per-peer
# backpressure), never blocking the synchronous publish fan-out.
REMOTE_SUBSCRIBER_QUEUE_SIZE = 1024


@runtime_checkable
class Subscriber(Protocol):
    """The sync entry the bus calls during its ordered fan-out."""

    def deliver(self, message: Message) -> None: ...


class LocalSubscriber:
    """Delivers each Message into an in-process `asyncio.Queue`.

    The queue holds the `Message` itself (not the bare payload) so the
    consuming adapter can read `topic`/`ts` alongside `payload`. `deliver` is
    non-blocking: on a full queue it drops and warns, so one slow local
    consumer can never stall the synchronous publish loop.
    """

    def __init__(self, queue: "asyncio.Queue[Message]") -> None:
        self.queue = queue

    def deliver(self, message: Message) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            logger.warning(
                "LocalSubscriber queue full for topic %s; dropping message",
                message.topic,
            )


class RemoteSubscriber:
    """Forwards each Message over a cluster link via a single pump task.

    `send` is `async (Message) -> None`. `deliver` enqueues (non-blocking,
    drop-on-full); the single pump task drains the bounded queue in order and
    awaits `send` for each item. `start()` launches the pump; `aclose()`
    cancels it. Order is preserved because there is exactly one pump and
    exactly one queue.
    """

    def __init__(
        self,
        send: Callable[[Message], Awaitable[None]],
        queue_size: int = REMOTE_SUBSCRIBER_QUEUE_SIZE,
    ) -> None:
        self.send = send
        self.queue: "asyncio.Queue[Message]" = asyncio.Queue(maxsize=queue_size)
        self._pump_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Launch the single pump task. Idempotent."""
        if self._pump_task is None:
            self._pump_task = asyncio.create_task(self._pump())

    def deliver(self, message: Message) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            logger.warning(
                "RemoteSubscriber queue full for topic %s; dropping message",
                message.topic,
            )

    async def _pump(self) -> None:
        while True:
            message = await self.queue.get()
            try:
                await self.send(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A send failure to one peer must not kill the pump; the next
                # message still gets a chance. (Matches today's syncers, which
                # swallow per-frame send errors.)
                logger.warning(
                    "RemoteSubscriber send failed for topic %s",
                    message.topic,
                    exc_info=True,
                )

    async def aclose(self) -> None:
        """Cancel the pump task and wait for it to unwind."""
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
            self._pump_task = None
