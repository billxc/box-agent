"""EventBus: log facade sink + in-process pub/sub.

Conforms to `boxagent.log.LogSink`. Wires the SQLite-backed EventStore to
in-process subscribers (web SSE, telegram notifier, syncer, ...).

Internally `EventBus` owns a `MessageBus` (bus/core.py). `publish` builds a
payload dict and hands it to the bus on the `events.<category>` topic; the bus
fans it out to its subscribers in registration order, synchronously. The FIRST
bus subscriber is the durable `StoreSubscriber` adapter: it performs the local
SQLite write, minting the event's id + origin_seq, and stashes the enriched
`Event` back into the message payload under `"event"`. Every later subscriber
reads that same enriched `Event` object — so the object handed to EventSyncer /
EventStreamSubscriber / TelegramNotifier is byte-identical to what they received
before the bus existed.

`EventBus.subscribe(callback)` remains a compatibility shim: it registers a
callback-adapter on the bus that invokes `callback(payload["event"])`. Because
StoreSubscriber is registered first, every such callback sees the enriched
`Event`. Ordering (store first, then callbacks in subscription order) and
subscriber-exception isolation are provided by the bus core.

The `/api/events` read path is unchanged (it reads `store.query` directly).
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from boxagent.bus.core import MessageBus, Subscription
from boxagent.bus.message import Packet

from .models import Event
from .storage import EventStore
from .store_subscriber import StoreSubscriber

logger = logging.getLogger(__name__)

EventCallback = Callable[[Event], None]

# Every locally published event is routed on the "events." topic prefix. The
# category is appended so a future subscriber can select by category prefix;
# today's subscribers all listen on the whole "events." prefix.
EVENT_TOPIC_PREFIX = "events."

# Payload key under which StoreSubscriber stashes the enriched Event so later
# subscribers receive the exact same object.
ENRICHED_EVENT_KEY = "event"


class _StoreBusSubscriber:
    """First bus subscriber: performs the local store write and stashes the
    enriched `Event` into the message payload for the subscribers that follow.

    Wraps `StoreSubscriber.write_local`. Registered FIRST on the bus so it runs
    first and synchronously — INV-A1 (the store row exists before `publish`
    returns) depends on this ordering plus the bus's synchronous fan-out.
    """

    def __init__(self, store_subscriber: StoreSubscriber) -> None:
        self._store_subscriber = store_subscriber

    def deliver(self, packet: Packet) -> None:
        payload = packet.payload
        event = self._store_subscriber.write_local(
            payload["level"],
            payload["category"],
            payload["message"],
            payload.get("meta"),
        )
        # Stash the enriched Event so the remaining subscribers receive the
        # exact same object. Packet is frozen but its payload dict is mutable.
        payload[ENRICHED_EVENT_KEY] = event


class _CallbackBusSubscriber:
    """Adapts a legacy `EventCallback` to a bus `Subscriber`.

    Reads the enriched `Event` stashed by `_StoreBusSubscriber` and invokes the
    callback with it — preserving the historical `bus.subscribe(callback)` API
    where the callback receives the persisted `Event`.
    """

    def __init__(self, callback: EventCallback) -> None:
        self.callback = callback

    def deliver(self, packet: Packet) -> None:
        event = packet.payload.get(ENRICHED_EVENT_KEY)
        if event is None:
            # Store write failed (or was skipped); nothing enriched to hand on.
            return
        self.callback(event)


class EventBus:
    def __init__(self, store: EventStore, machine_id: str, bus: MessageBus | None = None) -> None:
        self._store = store
        self._machine_id = machine_id
        # The shared, process-wide MessageBus (events + chat ride the same
        # instance in production, injected by the gateway). Defaults to a private
        # instance so tests can construct EventBus(store, machine_id) standalone.
        self._bus = bus if bus is not None else MessageBus()
        # The durable subscriber owns the local store write. It is registered
        # FIRST so it runs first and synchronously (see publish); the enriched
        # Event it stashes into the payload is what the remaining subscribers
        # receive.
        self._store_subscriber = StoreSubscriber(store, machine_id)
        self._bus.subscribe(
            EVENT_TOPIC_PREFIX, _StoreBusSubscriber(self._store_subscriber),
        )
        # Legacy callback subscribers registered via `subscribe`, kept so
        # `unsubscribe` can close the matching bus subscription. Keyed by list
        # position (not id/hash) because callbacks are often bound methods,
        # which are re-created on each attribute access — equal by value but
        # not identity — so `unsubscribe` matches by `==` like the old list did.
        self._callback_subscriptions: list[tuple[EventCallback, Subscription]] = []

    @property
    def machine_id(self) -> str:
        return self._machine_id

    def subscribe(self, callback: EventCallback) -> None:
        """Register a callback to receive every enriched `Event`.

        Compatibility shim: wraps `callback` in a bus subscriber on the
        "events." prefix, registered AFTER the store subscriber so it sees the
        enriched Event via `payload["event"]`.
        """
        subscription = self._bus.subscribe(
            EVENT_TOPIC_PREFIX, _CallbackBusSubscriber(callback),
        )
        self._callback_subscriptions.append((callback, subscription))

    def unsubscribe(self, callback: EventCallback) -> None:
        for index, (registered, subscription) in enumerate(
            self._callback_subscriptions
        ):
            if registered == callback:
                subscription.close()
                del self._callback_subscriptions[index]
                return

    def publish(self, level: str, category: str, message: str, **meta) -> None:
        """LogSink protocol entry point.

        Builds a payload dict and publishes it on the "events.<category>" topic.
        The bus delivers it synchronously and in order: the store subscriber
        writes the row first (minting id + origin_seq) and stashes the enriched
        Event, then the remaining subscribers receive that same Event. `bot` is
        a top-level store column, extracted from meta by the store write.
        """
        payload = {
            "level": level,
            "category": category,
            "message": message,
            "meta": meta,
        }
        self._bus.publish(f"{EVENT_TOPIC_PREFIX}{category}", payload, time.time())

    def close(self) -> None:
        self._store.close()
