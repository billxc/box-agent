"""Tests for events.store_subscriber: the durable subscriber that owns the
local store write and returns the enriched Event."""
from __future__ import annotations

import pytest

from boxagent.events.storage import EventStore
from boxagent.events.store_subscriber import StoreSubscriber


@pytest.fixture
def store(tmp_path):
    store = EventStore(tmp_path / "events.db")
    yield store
    store.close()


def test_write_local_persists_row_and_returns_enriched_event(store):
    event = StoreSubscriber(store, "m1").write_local(
        "info", "scheduler.run", "fired", {"task_id": "t1", "bot": "b1"}
    )

    # Returned Event is enriched with store-assigned id + origin_seq.
    assert event.id is not None
    assert event.origin_seq == 1
    assert event.origin_machine == "m1"
    assert event.level == "info"
    assert event.category == "scheduler.run"
    assert event.message == "fired"
    assert event.bot == "b1"
    assert event.meta == {"task_id": "t1"}

    # And the row is actually in the store (the write happened).
    rows = store.query()
    assert len(rows) == 1
    assert rows[0].id == event.id
    assert rows[0].bot == "b1"
    assert rows[0].meta == {"task_id": "t1"}


def test_write_local_extracts_bot_from_meta(store):
    event = StoreSubscriber(store, "m1").write_local(
        "info", "c", "m", {"bot": "bot_a", "extra": 1}
    )
    assert event.bot == "bot_a"
    assert "bot" not in event.meta
    assert event.meta == {"extra": 1}


def test_write_local_with_no_meta(store):
    event = StoreSubscriber(store, "m1").write_local("info", "c", "m")
    assert event.bot is None
    assert event.meta == {}


def test_write_local_does_not_mutate_caller_meta(store):
    meta = {"bot": "b1", "k": 1}
    StoreSubscriber(store, "m1").write_local("info", "c", "m", meta)
    # write_local copies before popping bot — caller's dict is untouched.
    assert meta == {"bot": "b1", "k": 1}


def test_write_local_mints_contiguous_origin_seq(store):
    subscriber = StoreSubscriber(store, "m1")
    first = subscriber.write_local("info", "c", "a")
    second = subscriber.write_local("info", "c", "b")
    assert first.origin_seq == 1
    assert second.origin_seq == 2


def test_machine_id_property(store):
    assert StoreSubscriber(store, "node_x").machine_id == "node_x"
