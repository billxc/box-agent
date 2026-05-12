"""SSE subscriber for the Web UI events page.

Each connected browser gets one EventStreamSubscriber: an asyncio.Queue
fed by EventBus subscriptions. Filters are applied at enqueue time so
slow clients drop their own irrelevant traffic.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .bus import EventBus
from .models import Event

logger = logging.getLogger(__name__)


@dataclass
class EventStreamSubscriber:
    bus: EventBus
    levels: list[str] | None = None
    machines: list[str] | None = None
    bot: str | None = None
    category_prefix: str | None = None
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1000))
    _loop: asyncio.AbstractEventLoop | None = None

    def __post_init__(self):
        self._loop = asyncio.get_event_loop()
        self.bus.subscribe(self._on_event)

    def close(self):
        self.bus.unsubscribe(self._on_event)

    def _matches(self, event: Event) -> bool:
        if self.levels and event.level not in self.levels:
            return False
        if self.machines and event.origin_machine not in self.machines:
            return False
        if self.bot and event.bot != self.bot:
            return False
        if self.category_prefix:
            cat = event.category
            if not (cat == self.category_prefix or cat.startswith(self.category_prefix + ".")):
                return False
        return True

    def _on_event(self, event: Event) -> None:
        if not self._matches(event):
            return
        try:
            self._loop.call_soon_threadsafe(self._enqueue, event)
        except RuntimeError:
            pass

    def _enqueue(self, event: Event) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("EventStreamSubscriber: queue full, dropping event")
