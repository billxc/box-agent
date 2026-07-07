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
import uuid
from typing import TYPE_CHECKING, Callable

from boxagent.bus.message import Packet

if TYPE_CHECKING:
    from boxagent.bus.subscriber import Subscriber

logger = logging.getLogger(__name__)

# 订阅观察者：一个 (prefix, on_add, on_remove) 三元组。当有人 subscribe / 退订一个
# 前缀匹配的 EXACT topic 时被通知，用来把"本机有人在看某远端 chat"这种 demand
# 沿 WS 往上游传播。只报告 exact-topic 订阅——bridge 自己的前缀订阅（如 "chat."）
# 不算 demand，故不上报。
SubscriptionWatcher = tuple[str, "Callable[[str], None]", "Callable[[str], None]"]


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

    def __init__(
        self,
        *,
        machine_id: str = "",
        id_factory: "Callable[[], str] | None" = None,
    ) -> None:
        # machine_id stamps every packet's `sender`; id_factory mints `message_id`
        # at the send() seam (injectable so tests get deterministic ids — never
        # uuid4() deep in the fan-out, which would break the clock-free/testable
        # contract, same reasoning as caller-supplied ts).
        self._machine_id = machine_id
        self._id_factory: "Callable[[], str]" = (
            id_factory if id_factory is not None else (lambda: uuid.uuid4().hex)
        )
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
        # 订阅观察者（chat bridge 用来传播 demand）。见 SubscriptionWatcher。
        self._watchers: list[SubscriptionWatcher] = []

    def watch_subscriptions(
        self,
        topic_prefix: str,
        on_add: "Callable[[str], None]",
        on_remove: "Callable[[str], None]",
    ) -> None:
        """注册一个订阅观察者：当有人 subscribe / 退订一个以 ``topic_prefix`` 开头
        的 EXACT topic 时，分别调用 ``on_add(topic)`` / ``on_remove(topic)``。

        每次 add/remove 都触发（不去重）；调用方自行 refcount。只报告 exact-topic
        订阅——bridge 自己在同一 prefix 上的前缀订阅不会被当成 demand。"""
        self._watchers.append((topic_prefix, on_add, on_remove))

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
            self._notify_watchers(topic_pattern, added=True)
        return subscription

    def has_subscribers(self, topic: str) -> bool:
        """Whether any live subscription would receive a publish to ``topic``
        (exact bucket or a matching prefix). Read-only introspection."""
        if self._exact.get(topic):
            return True
        return any(topic.startswith(sub.topic_pattern) for sub in self._prefix)

    def send(self, *, receiver: str, topic: str, payload: dict, ts: float) -> str:
        """Location-unified send. Stamp `message_id` (UUID) + `sender` (this
        machine), then deliver to local subscribers when the packet is addressed
        here — `receiver == ""` (broadcast) or `receiver ==` this machine. Return
        the stamped `message_id`.

        A LocalBus reaches only this machine: a packet addressed to a *different*
        machine is stamped and its id returned, but has nowhere to go here — the
        ClusterBus is what ships it over a link.
        """
        packet = Packet(
            message_id=self._id_factory(),
            sender=self._machine_id,
            receiver=receiver,
            topic=topic,
            payload=payload,
            ts=ts,
        )
        if receiver == "" or receiver == self._machine_id:
            self._deliver_local(packet)
        return packet.message_id

    def publish(self, topic: str, payload: dict, ts: float) -> None:
        """Broadcast shim over `send()` — retained until callers migrate to send."""
        self.send(receiver="", topic=topic, payload=payload, ts=ts)

    def _deliver_local(self, packet: Packet) -> None:
        """Fan a packet out to every matching local subscriber, in order.

        SYNCHRONOUS and ordered: the store-subscriber-first / first-subscribed
        ordering is preserved by sorting the matched subscriptions by `order`.
        One subscriber's `deliver` raising is caught, logged, and MUST NOT stop
        the others (subscriber-exception isolation).
        """
        topic = packet.topic
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
                subscription.subscriber.deliver(packet)
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
            self._notify_watchers(subscription.topic_pattern, added=False)

    def _notify_watchers(self, topic: str, *, added: bool) -> None:
        for prefix, on_add, on_remove in self._watchers:
            if topic.startswith(prefix):
                callback = on_add if added else on_remove
                try:
                    callback(topic)
                except Exception:
                    logger.warning(
                        "subscription watcher for prefix %s raised on topic %s",
                        prefix, topic, exc_info=True,
                    )
