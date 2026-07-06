"""The content-agnostic message bus core.

`MessageBus` routes on `topic` only. `payload` is opaque — the core never
inspects it. `publish` is SYNCHRONOUS: an ordered for-loop over the subscribers
whose pattern matches the topic. There is NO create_task per message here (坑
#1); all async lives in a `RemoteSubscriber`'s single pump task. A subscriber
registered FIRST is delivered FIRST (ordered slots — later phases rely on the
store-subscriber running first and synchronously).

Topic matching (kept deliberately simple — linear scan, no trie; the fleet is
3-4 nodes):
  - exact:  pattern "chat.m.b.c" matches only topic "chat.m.b.c"
  - prefix: a pattern ending in "." matches any topic starting with it, so
            "events." matches "events.scheduler.run", and
            "events.scheduler." matches "events.scheduler.run" but NOT
            "events.cluster.x".

This module is a neutral leaf: it imports nothing project-internal.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from boxagent.bus.message import Message

if TYPE_CHECKING:
    from boxagent.bus.subscriber import Subscriber

logger = logging.getLogger(__name__)


class Subscription:
    """Handle returned by `MessageBus.subscribe`. `close()` unsubscribes.

    Closing is idempotent and safe to call after the bus is gone. `order` is a
    process-monotonic sequence so fan-out can restore global subscription order
    even though subscriptions are indexed by topic.
    """

    def __init__(
        self,
        bus: "MessageBus",
        topic_pattern: str,
        subscriber: "Subscriber",
        order: int,
    ) -> None:
        self._bus = bus
        self.topic_pattern = topic_pattern
        self.subscriber = subscriber
        self.order = order
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus._remove(self)


class MessageBus:
    """Synchronous, ordered, content-agnostic publish/subscribe."""

    def __init__(self) -> None:
        # Indexed by topic so a publish touches only the relevant subscriptions,
        # not every subscription in the process (the whole node shares one bus,
        # so a chat stream_delta must not scan unrelated event/chat subs):
        #   _exact:  exact-topic patterns → subs, O(1) lookup (the chat hot path)
        #   _prefix: prefix patterns (ending in ".") → scanned with startswith
        #            (only the events.* family + a handful of /events SSE subs)
        # `order` restores global first-subscribed-first ordering across both.
        self._exact: dict[str, list[Subscription]] = {}
        self._prefix: list[Subscription] = []
        self._next_order = 0

    def subscribe(
        self,
        topic_pattern: str,
        subscriber: "Subscriber",
    ) -> Subscription:
        """Register a subscriber for a topic pattern.

        `topic_pattern` is either an exact topic ("chat.m.b.c") or a prefix
        ending in "." ("events." / "events.scheduler."). Returns a
        `Subscription`; call `.close()` to unsubscribe.
        """
        subscription = Subscription(self, topic_pattern, subscriber, self._next_order)
        self._next_order += 1
        if topic_pattern.endswith("."):
            self._prefix.append(subscription)
        else:
            self._exact.setdefault(topic_pattern, []).append(subscription)
        return subscription

    def publish(self, topic: str, payload: dict, ts: float) -> None:
        """Fan a message out to every matching subscriber, in order.

        SYNCHRONOUS and ordered: the store-subscriber-first / first-subscribed
        ordering is preserved by sorting the matched subscriptions by `order`.
        One subscriber's `deliver` raising is caught, logged, and MUST NOT stop
        the others (subscriber-exception isolation).
        """
        message = Message(topic=topic, payload=payload, ts=ts)
        # Gather matches: O(1) exact bucket + a scan of the (small) prefix list.
        matched = list(self._exact.get(topic, ()))
        for subscription in self._prefix:
            if topic.startswith(subscription.topic_pattern):
                matched.append(subscription)
        # Restore global registration order; the list is a snapshot, so a
        # subscriber closing its own (or another) subscription mid-fan-out is safe.
        if len(matched) > 1:
            matched.sort(key=lambda subscription: subscription.order)
        for subscription in matched:
            try:
                subscription.subscriber.deliver(message)
            except Exception:
                logger.warning(
                    "subscriber for pattern %s raised on topic %s; continuing",
                    subscription.topic_pattern,
                    topic,
                    exc_info=True,
                )

    def _remove(self, subscription: Subscription) -> None:
        if subscription.topic_pattern.endswith("."):
            try:
                self._prefix.remove(subscription)
            except ValueError:
                pass
        else:
            bucket = self._exact.get(subscription.topic_pattern)
            if bucket is not None:
                try:
                    bucket.remove(subscription)
                except ValueError:
                    pass
                if not bucket:
                    del self._exact[subscription.topic_pattern]
