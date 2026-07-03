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
    LocalSubscriber,
    Message,
    MessageBus,
    RemoteSubscriber,
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


# --------------------------------------------------------------------------- #
# LocalSubscriber                                                              #
# --------------------------------------------------------------------------- #


async def test_local_subscriber_delivers_message_into_queue():
    queue: asyncio.Queue[Message] = asyncio.Queue()
    subscriber = LocalSubscriber(queue)

    message = Message(topic="chat.m.b.c", payload={"n": 1}, ts=1.0)
    subscriber.deliver(message)

    got = await queue.get()
    assert got is message


async def test_local_subscriber_drop_on_full():
    queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=2)
    subscriber = LocalSubscriber(queue)

    for index in range(5):
        subscriber.deliver(Message(topic="t", payload={"i": index}, ts=float(index)))

    # Only the first two fit; the rest were dropped, and deliver never raised.
    assert queue.qsize() == 2
    first = await queue.get()
    second = await queue.get()
    assert [first.payload["i"], second.payload["i"]] == [0, 1]


async def test_local_subscriber_on_bus_fanout():
    bus = MessageBus()
    queue: asyncio.Queue[Message] = asyncio.Queue()
    bus.subscribe("chat.m.b.c", LocalSubscriber(queue))

    bus.publish("chat.m.b.c", {"text": "hi"}, ts=1.0)

    message = await queue.get()
    assert message.payload == {"text": "hi"}


# --------------------------------------------------------------------------- #
# RemoteSubscriber                                                             #
# --------------------------------------------------------------------------- #


async def test_remote_subscriber_single_pump_preserves_order():
    sent: list[int] = []

    async def send(message: Message) -> None:
        # Yield control so a naive create_task-per-message impl WOULD reorder;
        # the single pump must still forward in publish order.
        await asyncio.sleep(0)
        sent.append(message.payload["i"])

    subscriber = RemoteSubscriber(send)
    subscriber.start()
    try:
        for index in range(100):
            subscriber.deliver(
                Message(topic="t", payload={"i": index}, ts=float(index))
            )
        # Wait until all 100 have been forwarded.
        while len(sent) < 100:
            await asyncio.sleep(0)
        assert sent == list(range(100))
    finally:
        await subscriber.aclose()


async def test_remote_subscriber_drop_on_full():
    released = asyncio.Event()
    sent: list[int] = []

    async def send(message: Message) -> None:
        # Block on the very first item so the queue fills up behind it.
        await released.wait()
        sent.append(message.payload["i"])

    subscriber = RemoteSubscriber(send, queue_size=2)
    subscriber.start()
    try:
        # Let the pump pick up the first item (it then blocks on `released`).
        subscriber.deliver(Message(topic="t", payload={"i": 0}, ts=0.0))
        await asyncio.sleep(0)  # pump dequeues item 0, now blocked

        # Queue capacity is 2; fill it and overflow.
        for index in range(1, 6):
            subscriber.deliver(
                Message(topic="t", payload={"i": index}, ts=float(index))
            )
        # Two are buffered (1, 2); the rest (3, 4, 5) were dropped.
        assert subscriber.queue.qsize() == 2

        released.set()
        while len(sent) < 3:
            await asyncio.sleep(0)
        assert sent == [0, 1, 2]
    finally:
        released.set()
        await subscriber.aclose()


async def test_remote_subscriber_send_failure_does_not_kill_pump():
    sent: list[int] = []

    async def send(message: Message) -> None:
        if message.payload["i"] == 1:
            raise RuntimeError("transient link error")
        sent.append(message.payload["i"])

    subscriber = RemoteSubscriber(send)
    subscriber.start()
    try:
        for index in range(3):
            subscriber.deliver(
                Message(topic="t", payload={"i": index}, ts=float(index))
            )
        while len(sent) < 2:
            await asyncio.sleep(0)
        # Item 1 raised and was skipped; 0 and 2 still forwarded, in order.
        assert sent == [0, 2]
    finally:
        await subscriber.aclose()


async def test_remote_subscriber_aclose_cancels_pump():
    async def send(message: Message) -> None:
        await asyncio.sleep(3600)  # would hang forever if not cancelled

    subscriber = RemoteSubscriber(send)
    subscriber.start()
    subscriber.deliver(Message(topic="t", payload={}, ts=0.0))
    await asyncio.sleep(0)

    pump_task = subscriber._pump_task
    assert pump_task is not None
    await subscriber.aclose()
    assert pump_task.cancelled() or pump_task.done()
    assert subscriber._pump_task is None


async def test_remote_subscriber_start_is_idempotent():
    async def send(message: Message) -> None:
        return None

    subscriber = RemoteSubscriber(send)
    subscriber.start()
    first_task = subscriber._pump_task
    subscriber.start()  # second start must not spawn a new pump
    assert subscriber._pump_task is first_task
    await subscriber.aclose()


async def test_remote_subscriber_on_bus_fanout_order():
    # End-to-end through the bus: publish 100, remote pump forwards 100 in order.
    forwarded: list[int] = []

    async def send(message: Message) -> None:
        await asyncio.sleep(0)
        forwarded.append(message.payload["i"])

    bus = MessageBus()
    subscriber = RemoteSubscriber(send)
    subscriber.start()
    bus.subscribe("events.", subscriber)
    try:
        for index in range(100):
            bus.publish("events.x", {"i": index}, ts=float(index))
        while len(forwarded) < 100:
            await asyncio.sleep(0)
        assert forwarded == list(range(100))
    finally:
        await subscriber.aclose()
