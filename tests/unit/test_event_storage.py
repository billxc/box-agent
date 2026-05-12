"""Tests for events.storage: SQLite schema, insert (local/remote), query,
read-state, retention, sync cursor."""
from __future__ import annotations

import time

import pytest

from boxagent.events.models import Event, Level
from boxagent.events.storage import EventStore


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "events.db"
    store = EventStore(db_path)
    yield store
    store.close()


# ---------- schema / init ----------

def test_fresh_store_is_empty(store):
    assert store.query() == []


def test_max_origin_seq_for_unknown_machine_is_zero(store):
    assert store.max_origin_seq("never-seen") == 0


# ---------- insert_local ----------

def test_insert_local_returns_event_with_id_and_seq(store):
    event = store.insert_local("m1", Level.INFO, "scheduler.run", "fired")
    assert event.id is not None
    assert event.origin_machine == "m1"
    assert event.origin_seq == 1
    assert event.level == "info"
    assert event.category == "scheduler.run"
    assert event.message == "fired"


def test_insert_local_auto_increments_origin_seq_per_machine(store):
    a1 = store.insert_local("m1", Level.INFO, "c", "m")
    a2 = store.insert_local("m1", Level.INFO, "c", "m")
    a3 = store.insert_local("m1", Level.INFO, "c", "m")
    assert [a1.origin_seq, a2.origin_seq, a3.origin_seq] == [1, 2, 3]


def test_insert_local_seq_independent_per_machine(store):
    a = store.insert_local("m1", Level.INFO, "c", "m")
    b = store.insert_local("m2", Level.INFO, "c", "m")
    c = store.insert_local("m1", Level.INFO, "c", "m")
    assert a.origin_seq == 1
    assert b.origin_seq == 1
    assert c.origin_seq == 2


def test_insert_local_persists_meta_and_bot(store):
    event = store.insert_local(
        "m1", Level.NOTIFY, "agent.notify", "hello",
        bot="bot_a", meta={"task": "t1", "elapsed": 3.5, "nested": {"k": "v"}},
    )
    fetched = store.query()[0]
    assert fetched.bot == "bot_a"
    assert fetched.meta == {"task": "t1", "elapsed": 3.5, "nested": {"k": "v"}}


def test_insert_local_uses_provided_ts(store):
    fixed = 1700000000.0
    event = store.insert_local("m1", Level.INFO, "c", "m", ts=fixed)
    assert event.ts == fixed


def test_insert_local_defaults_ts_to_now(store):
    before = time.time()
    event = store.insert_local("m1", Level.INFO, "c", "m")
    after = time.time()
    assert before <= event.ts <= after


# ---------- insert_remote (dedup) ----------

def test_insert_remote_persists_event(store):
    incoming = Event(
        id=None, origin_machine="other", origin_seq=42,
        ts=1700000000.0, level="info", category="cluster.peer.up",
        message="hi", bot=None, meta={},
    )
    inserted = store.insert_remote(incoming)
    assert inserted is True
    rows = store.query()
    assert len(rows) == 1
    assert rows[0].origin_machine == "other"
    assert rows[0].origin_seq == 42


def test_insert_remote_dedups_on_origin_machine_seq(store):
    event = Event(
        id=None, origin_machine="other", origin_seq=42,
        ts=1700000000.0, level="info", category="c", message="m",
        bot=None, meta={},
    )
    assert store.insert_remote(event) is True
    assert store.insert_remote(event) is False
    assert len(store.query()) == 1


def test_insert_remote_does_not_collide_with_local_seq(store):
    """Local m1 has seq 1,2,3; remote m1 with seq 5 should also insert (same machine_id, but conceptually we don't expect this — still test it doesn't crash). Actually the contract: origin_machine identifies who wrote it. If two stores claim to be m1, last write wins on dedup. Verify no crash on different seq."""
    store.insert_local("m1", Level.INFO, "c", "m")
    store.insert_local("m1", Level.INFO, "c", "m")
    remote = Event(
        id=None, origin_machine="m1", origin_seq=99,
        ts=1700000000.0, level="info", category="c", message="m",
        bot=None, meta={},
    )
    assert store.insert_remote(remote) is True
    assert len(store.query()) == 3


# ---------- query: filters ----------

def _seed(store):
    store.insert_local("m1", Level.INFO, "scheduler.run", "a", bot="bot_a", ts=100.0)
    store.insert_local("m1", Level.ERROR, "scheduler.fail", "b", bot="bot_a", ts=200.0)
    store.insert_local("m1", Level.INFO, "agent.turn", "c", bot="bot_b", ts=300.0)
    store.insert_local("m2", Level.WARNING, "backend.crash", "d", bot="bot_b", ts=400.0)
    store.insert_local("m2", Level.NOTIFY, "agent.notify", "e", bot="bot_a", ts=500.0)


def test_query_returns_all_when_no_filter(store):
    _seed(store)
    assert len(store.query()) == 5


def test_query_orders_by_ts_desc_by_default(store):
    _seed(store)
    rows = store.query()
    assert [r.ts for r in rows] == [500.0, 400.0, 300.0, 200.0, 100.0]


def test_query_filter_by_bot(store):
    _seed(store)
    rows = store.query(bot="bot_a")
    assert {r.message for r in rows} == {"a", "b", "e"}


def test_query_filter_by_levels(store):
    _seed(store)
    rows = store.query(levels=["error", "warning"])
    assert {r.message for r in rows} == {"b", "d"}


def test_query_filter_by_machine(store):
    _seed(store)
    rows = store.query(machines=["m2"])
    assert {r.message for r in rows} == {"d", "e"}


def test_query_filter_by_category_prefix(store):
    _seed(store)
    rows = store.query(category_prefix="scheduler")
    assert {r.message for r in rows} == {"a", "b"}


def test_query_filter_by_category_prefix_does_not_match_substring(store):
    """`scheduler` should not match `agent.scheduler.x`."""
    store.insert_local("m1", Level.INFO, "scheduler.run", "yes", ts=1.0)
    store.insert_local("m1", Level.INFO, "agent.scheduler.x", "no", ts=2.0)
    rows = store.query(category_prefix="scheduler")
    assert {r.message for r in rows} == {"yes"}


def test_query_filter_by_time_window(store):
    _seed(store)
    rows = store.query(since=200.0, until=400.0)
    assert {r.message for r in rows} == {"b", "c", "d"}


def test_query_search_message_substring(store):
    _seed(store)
    store.insert_local("m1", Level.INFO, "c", "the quick brown fox", ts=600.0)
    rows = store.query(search="brown")
    assert {r.message for r in rows} == {"the quick brown fox"}


def test_query_limit_and_pagination_via_before_id(store):
    for i in range(10):
        store.insert_local("m1", Level.INFO, "c", f"msg{i}", ts=float(i))
    page1 = store.query(limit=4)
    assert len(page1) == 4
    assert [r.message for r in page1] == ["msg9", "msg8", "msg7", "msg6"]

    page2 = store.query(limit=4, before_id=page1[-1].id)
    assert [r.message for r in page2] == ["msg5", "msg4", "msg3", "msg2"]


# ---------- read state ----------

def test_new_events_have_no_read_at(store):
    store.insert_local("m1", Level.INFO, "c", "m")
    assert store.query()[0].read_at is None


def test_mark_read_sets_read_at(store):
    e = store.insert_local("m1", Level.INFO, "c", "m")
    n = store.mark_read([e.id])
    assert n == 1
    assert store.query()[0].read_at is not None


def test_query_unread_only(store):
    e1 = store.insert_local("m1", Level.INFO, "c", "m1")
    e2 = store.insert_local("m1", Level.INFO, "c", "m2")
    store.mark_read([e1.id])
    rows = store.query(unread_only=True)
    assert {r.message for r in rows} == {"m2"}


def test_mark_read_idempotent(store):
    e = store.insert_local("m1", Level.INFO, "c", "m")
    store.mark_read([e.id])
    first = store.query()[0].read_at
    time.sleep(0.001)
    n = store.mark_read([e.id])
    assert n == 0  # no-op for already-read
    assert store.query()[0].read_at == first


# ---------- retention ----------

def test_delete_older_than(store):
    store.insert_local("m1", Level.INFO, "c", "old", ts=100.0)
    store.insert_local("m1", Level.INFO, "c", "new", ts=200.0)
    n = store.delete_older_than(cutoff_ts=150.0)
    assert n == 1
    rows = store.query()
    assert {r.message for r in rows} == {"new"}


# ---------- sync cursor ----------

def test_cursor_default_is_zero(store):
    assert store.get_cursor("peer-x") == 0


def test_cursor_set_and_get(store):
    store.set_cursor("peer-x", 42)
    assert store.get_cursor("peer-x") == 42
    store.set_cursor("peer-x", 100)
    assert store.get_cursor("peer-x") == 100


def test_cursors_independent_per_peer(store):
    store.set_cursor("peer-a", 10)
    store.set_cursor("peer-b", 20)
    assert store.get_cursor("peer-a") == 10
    assert store.get_cursor("peer-b") == 20


# ---------- max_origin_seq (resume on restart) ----------

def test_max_origin_seq_after_local_inserts(store):
    store.insert_local("m1", Level.INFO, "c", "m")
    store.insert_local("m1", Level.INFO, "c", "m")
    assert store.max_origin_seq("m1") == 2


def test_max_origin_seq_survives_reopen(tmp_path):
    db_path = tmp_path / "events.db"
    s1 = EventStore(db_path)
    s1.insert_local("m1", Level.INFO, "c", "m")
    s1.insert_local("m1", Level.INFO, "c", "m")
    s1.insert_local("m1", Level.INFO, "c", "m")
    s1.close()

    s2 = EventStore(db_path)
    assert s2.max_origin_seq("m1") == 3
    next_event = s2.insert_local("m1", Level.INFO, "c", "m")
    assert next_event.origin_seq == 4
    s2.close()


# ---------- sync helpers ----------

def test_known_machines_lists_all_origins(store):
    store.insert_local("m1", Level.INFO, "c", "x")
    store.insert_local("m2", Level.INFO, "c", "y")
    store.insert_local("m1", Level.INFO, "c", "z")
    assert set(store.known_machines()) == {"m1", "m2"}


def test_known_machines_empty_on_fresh_store(store):
    assert store.known_machines() == []


def test_max_seq_per_machine_returns_top_per_origin(store):
    store.insert_local("m1", Level.INFO, "c", "x")
    store.insert_local("m1", Level.INFO, "c", "y")
    store.insert_local("m2", Level.INFO, "c", "z")
    assert store.max_seq_per_machine() == {"m1": 2, "m2": 1}


def test_events_after_seq_returns_only_newer(store):
    store.insert_local("m1", Level.INFO, "c", "a")
    store.insert_local("m1", Level.INFO, "c", "b")
    store.insert_local("m1", Level.INFO, "c", "c")
    out = store.events_after_seq("m1", 1)
    assert [e.message for e in out] == ["b", "c"]
    assert all(e.origin_seq > 1 for e in out)


def test_events_after_seq_orders_ascending_by_seq(store):
    store.insert_local("m1", Level.INFO, "c", "a")
    store.insert_local("m1", Level.INFO, "c", "b")
    out = store.events_after_seq("m1", 0)
    assert [e.origin_seq for e in out] == [1, 2]


def test_events_after_seq_filters_by_since_ts(store):
    now = time.time()
    store.insert_local("m1", Level.INFO, "c", "old", ts=now - 10000)
    store.insert_local("m1", Level.INFO, "c", "new", ts=now)
    out = store.events_after_seq("m1", 0, since_ts=now - 1)
    assert [e.message for e in out] == ["new"]


def test_events_after_seq_respects_limit(store):
    for i in range(5):
        store.insert_local("m1", Level.INFO, "c", f"e{i}")
    out = store.events_after_seq("m1", 0, limit=2)
    assert len(out) == 2
    assert [e.origin_seq for e in out] == [1, 2]


def test_events_after_seq_isolates_by_machine(store):
    store.insert_local("m1", Level.INFO, "c", "x")
    store.insert_local("m2", Level.INFO, "c", "y")
    out = store.events_after_seq("m1", 0)
    assert [e.origin_machine for e in out] == ["m1"]
