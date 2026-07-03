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
from .store_subscriber import StoreSubscriber

logger = logging.getLogger(__name__)

EventCallback = Callable[[Event], None]


class EventBus:
    def __init__(self, store: EventStore, machine_id: str) -> None:
        self._store = store
        self._machine_id = machine_id
        # The durable subscriber owns the local store write. It runs first and
        # synchronously (see publish); the enriched Event it returns is what the
        # remaining subscribers receive.
        self._store_subscriber = StoreSubscriber(store, machine_id)
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

        The durable subscriber (StoreSubscriber) performs the local store write
        first and synchronously, minting the event's id + origin_seq. The
        enriched Event is then fanned out to the remaining subscribers unchanged.
        `bot` is a top-level column, extracted from meta by the store write.
        """
        try:
            event = self._store_subscriber.write_local(
                level, category, message, meta,
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
