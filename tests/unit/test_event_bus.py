"""Tests for events.bus: EventBus dispatches to store + subscribers."""
from __future__ import annotations

import pytest

from boxagent.events.bus import EventBus
from boxagent.events.models import Event, Level
from boxagent.events.storage import EventStore


@pytest.fixture
def bus(tmp_path):
    store = EventStore(tmp_path / "events.db")
    bus = EventBus(store=store, machine_id="m1")
    yield bus
    bus.close()


def test_publish_writes_to_store(bus):
    bus.publish("info", "scheduler.run", "fired", task_id="t1", bot="b1")

    rows = bus._store.query()
    assert len(rows) == 1
    assert rows[0].level == "info"
    assert rows[0].category == "scheduler.run"
    assert rows[0].message == "fired"
    assert rows[0].bot == "b1"
    assert rows[0].meta == {"task_id": "t1"}
    assert rows[0].origin_machine == "m1"


def test_publish_extracts_bot_from_kwargs(bus):
    """`bot` is a top-level column, not part of meta."""
    bus.publish("info", "c", "m", bot="bot_a", extra=1)

    rows = bus._store.query()
    assert rows[0].bot == "bot_a"
    assert "bot" not in rows[0].meta
    assert rows[0].meta == {"extra": 1}


def test_publish_with_no_meta(bus):
    bus.publish("info", "c", "m")

    rows = bus._store.query()
    assert rows[0].meta == {}
    assert rows[0].bot is None


def test_subscribers_receive_event(bus):
    received: list[Event] = []
    bus.subscribe(lambda evt: received.append(evt))

    bus.publish("info", "c", "m", k=1)

    assert len(received) == 1
    assert received[0].category == "c"
    assert received[0].meta == {"k": 1}
    assert received[0].id is not None  # store assigned id before notify


def test_multiple_subscribers_all_receive(bus):
    a: list = []
    b: list = []
    bus.subscribe(a.append)
    bus.subscribe(b.append)

    bus.publish("info", "c", "m")

    assert len(a) == 1
    assert len(b) == 1


def test_subscriber_exception_does_not_block_others(bus):
    received: list = []

    def boom(_evt):
        raise RuntimeError("subscriber boom")

    bus.subscribe(boom)
    bus.subscribe(received.append)

    bus.publish("info", "c", "m")  # must not raise

    assert len(received) == 1


def test_subscriber_exception_does_not_break_store_write(bus):
    bus.subscribe(lambda _: (_ for _ in ()).throw(RuntimeError("x")))

    bus.publish("info", "c", "m")

    assert len(bus._store.query()) == 1


def test_unsubscribe(bus):
    received: list = []
    callback = received.append
    bus.subscribe(callback)
    bus.publish("info", "c", "m")
    assert len(received) == 1

    bus.unsubscribe(callback)
    bus.publish("info", "c", "m2")
    assert len(received) == 1  # no new


def test_publish_implements_logsink_protocol():
    """EventBus must satisfy LogSink so log facade can bind it directly."""
    from boxagent.log import LogSink

    # Static check that EventBus has the right shape — runtime duck typing is
    # what matters, but verify the call signature matches.
    import inspect

    sig = inspect.signature(EventBus.publish)
    params = list(sig.parameters.keys())
    assert params[:4] == ["self", "level", "category", "message"]
    # **meta as last param
    assert any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )


def test_bus_can_be_bound_to_log_facade(tmp_path):
    """End-to-end: log.info → EventBus → SQLite."""
    from boxagent.log import LogFacade

    store = EventStore(tmp_path / "e.db")
    bus = EventBus(store=store, machine_id="m1")
    facade = LogFacade()
    facade.bind(bus)

    facade.info("scheduler.run", "via facade", bot="b1", task_id="t1")

    rows = store.query()
    assert len(rows) == 1
    assert rows[0].message == "via facade"
    assert rows[0].bot == "b1"
    assert rows[0].meta == {"task_id": "t1"}

    bus.close()
