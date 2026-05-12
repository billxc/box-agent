"""EventBus: log facade sink + in-process pub/sub.

Conforms to `boxagent.log.LogSink`. Wires the SQLite-backed EventStore to
in-process subscribers (web SSE, telegram notifier, syncer, ...).

Subscribers receive the persisted Event (with id assigned). A subscriber
exception is logged but does not affect the store write or other subscribers.
"""
from __future__ import annotations

import logging
from typing import Callable

from .models import Event
from .storage import EventStore

logger = logging.getLogger(__name__)

EventCallback = Callable[[Event], None]


class EventBus:
    def __init__(self, store: EventStore, machine_id: str) -> None:
        self._store = store
        self._machine_id = machine_id
        self._subscribers: list[EventCallback] = []

    @property
    def machine_id(self) -> str:
        return self._machine_id

    def subscribe(self, callback: EventCallback) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: EventCallback) -> None:
        try:
            self._subscribers.remove(callback)
        except ValueError:
            pass

    def publish(self, level: str, category: str, message: str, **meta) -> None:
        """LogSink protocol entry point.

        `bot` is pulled out of meta because it is a top-level column.
        """
        bot = meta.pop("bot", None)
        try:
            event = self._store.insert_local(
                self._machine_id, level, category, message,
                bot=bot, meta=meta or None,
            )
        except Exception:
            logger.exception("EventBus: failed to persist event")
            return

        for callback in list(self._subscribers):
            try:
                callback(event)
            except Exception:
                logger.exception("EventBus: subscriber raised")

    def close(self) -> None:
        self._store.close()
