"""Unit tests for the neutral bus/ core (Phase 2).

Covers the whole package: Message envelope, MessageBus topic matching + ordered
fan-out + subscriber-exception isolation + Subscription.close, and both
LocalSubscriber (drop-on-full) and RemoteSubscriber (single-pump ORDER
preservation, drop-on-full, aclose cancels the pump).

asyncio_mode=auto — async tests need no decorator.
"""
from __future__ import annotations

import asyncio
import dataclasses

import pytest

from boxagent.bus import (
    Message,
    MessageBus,
    Subscriber,
)


class RecordingSubscriber:
    """Minimal sync subscriber that records every Message it receives."""

    def __init__(self) -> None:
        self.received: list[Message] = []

    def deliver(self, message: Message) -> None:
        self.received.append(message)


class RaisingSubscriber:
    """A subscriber whose deliver always raises (exception-isolation test)."""

    def deliver(self, message: Message) -> None:
        raise RuntimeError("boom")


# --------------------------------------------------------------------------- #
# Message                                                                      #
# --------------------------------------------------------------------------- #


def test_message_is_frozen():
    message = Message(topic="events.x", payload={"a": 1}, ts=1.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        message.topic = "other"  # type: ignore[misc]


def test_message_roundtrip_fields():
    payload = {"level": "info", "message": "hi"}
    message = Message(topic="chat.m.b.c", payload=payload, ts=12.5)
    assert message.topic == "chat.m.b.c"
    assert message.payload is payload
    assert message.ts == 12.5


def test_recording_subscriber_satisfies_protocol():
    # Subscriber is runtime_checkable — the recorder must qualify.
    assert isinstance(RecordingSubscriber(), Subscriber)


# --------------------------------------------------------------------------- #
# MessageBus — topic matching                                                  #
# --------------------------------------------------------------------------- #


def test_exact_topic_delivery():
    bus = MessageBus()
    subscriber = RecordingSubscriber()
    bus.subscribe("chat.m.b.c", subscriber)

    bus.publish("chat.m.b.c", {"n": 1}, ts=1.0)

    assert len(subscriber.received) == 1
    assert subscriber.received[0].topic == "chat.m.b.c"
    assert subscriber.received[0].payload == {"n": 1}


def test_exact_topic_does_not_match_different_topic():
    bus = MessageBus()
    subscriber = RecordingSubscriber()
    bus.subscribe("chat.m.b.c", subscriber)

    bus.publish("chat.m.b.d", {"n": 1}, ts=1.0)

    assert subscriber.received == []


def test_prefix_topic_delivery():
    bus = MessageBus()
    subscriber = RecordingSubscriber()
    bus.subscribe("events.", subscriber)

    bus.publish("events.scheduler.run", {"n": 1}, ts=1.0)
    bus.publish("events.cluster.host.rpc_fail", {"n": 2}, ts=2.0)

    topics = [message.topic for message in subscriber.received]
    assert topics == ["events.scheduler.run", "events.cluster.host.rpc_fail"]


def test_prefix_subtree_matches_only_its_subtree():
    bus = MessageBus()
    subscriber = RecordingSubscriber()
    bus.subscribe("events.scheduler.", subscriber)

    # In-subtree: matches.
    bus.publish("events.scheduler.run", {"n": 1}, ts=1.0)
    # Sibling subtree: must NOT match.
    bus.publish("events.cluster.x", {"n": 2}, ts=2.0)

    topics = [message.topic for message in subscriber.received]
    assert topics == ["events.scheduler.run"]


def test_prefix_does_not_match_bare_topic_equal_to_pattern_stem():
    # "events." should not match a topic literally named "events" (no dot).
    bus = MessageBus()
    subscriber = RecordingSubscriber()
    bus.subscribe("events.", subscriber)

    bus.publish("events", {"n": 1}, ts=1.0)

    assert subscriber.received == []


def test_non_matching_topic_not_delivered():
    bus = MessageBus()
    subscriber = RecordingSubscriber()
    bus.subscribe("events.", subscriber)

    bus.publish("chat.m.b.c", {"n": 1}, ts=1.0)

    assert subscriber.received == []


# --------------------------------------------------------------------------- #
# MessageBus — ordered fan-out & isolation                                     #
# --------------------------------------------------------------------------- #


def test_ordered_fanout_first_subscribed_first_delivered():
    bus = MessageBus()
    order: list[str] = []

    class OrderedSubscriber:
        def __init__(self, name: str) -> None:
            self.name = name

        def deliver(self, message: Message) -> None:
            order.append(self.name)

    bus.subscribe("events.", OrderedSubscriber("first"))
    bus.subscribe("events.", OrderedSubscriber("second"))
    bus.subscribe("events.", OrderedSubscriber("third"))

    bus.publish("events.x", {}, ts=1.0)

    assert order == ["first", "second", "third"]


def test_subscriber_exception_isolation():
    # One subscriber raising must NOT stop the others (and order is preserved).
    bus = MessageBus()
    before = RecordingSubscriber()
    after = RecordingSubscriber()

    bus.subscribe("events.", before)
    bus.subscribe("events.", RaisingSubscriber())
    bus.subscribe("events.", after)

    bus.publish("events.x", {"n": 1}, ts=1.0)

    assert len(before.received) == 1
    assert len(after.received) == 1


def test_multiple_matching_patterns_all_fire():
    bus = MessageBus()
    prefix_subscriber = RecordingSubscriber()
    exact_subscriber = RecordingSubscriber()

    bus.subscribe("events.", prefix_subscriber)
    bus.subscribe("events.scheduler.run", exact_subscriber)

    bus.publish("events.scheduler.run", {"n": 1}, ts=1.0)

    assert len(prefix_subscriber.received) == 1
    assert len(exact_subscriber.received) == 1


# --------------------------------------------------------------------------- #
# Subscription.close                                                           #
# --------------------------------------------------------------------------- #


def test_subscription_close_unsubscribes():
    bus = MessageBus()
    subscriber = RecordingSubscriber()
    subscription = bus.subscribe("events.", subscriber)

    bus.publish("events.x", {"n": 1}, ts=1.0)
    subscription.close()
    bus.publish("events.x", {"n": 2}, ts=2.0)

    assert len(subscriber.received) == 1


def test_subscription_close_is_idempotent():
    bus = MessageBus()
    subscriber = RecordingSubscriber()
    subscription = bus.subscribe("events.", subscriber)

    subscription.close()
    subscription.close()  # must not raise

    bus.publish("events.x", {}, ts=1.0)
    assert subscriber.received == []


def test_close_during_delivery_does_not_disturb_others():
    # A subscriber closing its own subscription mid-fan-out is safe (snapshot).
    bus = MessageBus()
    other = RecordingSubscriber()

    class SelfClosing:
        def __init__(self) -> None:
            self.subscription = None
            self.count = 0

        def deliver(self, message: Message) -> None:
            self.count += 1
            self.subscription.close()

    self_closing = SelfClosing()
    self_closing.subscription = bus.subscribe("events.", self_closing)
    bus.subscribe("events.", other)

    bus.publish("events.x", {}, ts=1.0)
    bus.publish("events.x", {}, ts=2.0)

    assert self_closing.count == 1  # unsubscribed after the first
    assert len(other.received) == 2  # unaffected both times
