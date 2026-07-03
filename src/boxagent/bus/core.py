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

    Closing is idempotent and safe to call after the bus is gone.
    """

    def __init__(
        self,
        bus: "MessageBus",
        topic_pattern: str,
        subscriber: "Subscriber",
    ) -> None:
        self._bus = bus
        self.topic_pattern = topic_pattern
        self.subscriber = subscriber
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus._remove(self)


class MessageBus:
    """Synchronous, ordered, content-agnostic publish/subscribe."""

    def __init__(self) -> None:
        # Insertion-ordered list of live subscriptions. First-subscribed is
        # first in the list, therefore first delivered. A list (not a dict) is
        # what preserves the ordered-slot guarantee.
        self._subscriptions: list[Subscription] = []

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
        subscription = Subscription(self, topic_pattern, subscriber)
        self._subscriptions.append(subscription)
        return subscription

    def publish(self, topic: str, payload: dict, ts: float) -> None:
        """Fan a message out to every matching subscriber, in order.

        SYNCHRONOUS and ordered: a plain for-loop over the subscriptions whose
        pattern matches `topic`, in subscription order. One subscriber's
        `deliver` raising is caught, logged, and MUST NOT stop the others
        (subscriber-exception isolation).
        """
        message = Message(topic=topic, payload=payload, ts=ts)
        # Snapshot: a subscriber's deliver() may close its own subscription (or
        # another) during fan-out; iterate over a copy so that mutation is safe.
        for subscription in list(self._subscriptions):
            if not _topic_matches(subscription.topic_pattern, topic):
                continue
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
        try:
            self._subscriptions.remove(subscription)
        except ValueError:
            pass


def _topic_matches(topic_pattern: str, topic: str) -> bool:
    """True if `topic_pattern` selects `topic`.

    A pattern ending in "." is a prefix match; otherwise it is an exact match.
    """
    if topic_pattern.endswith("."):
        return topic.startswith(topic_pattern)
    return topic == topic_pattern
