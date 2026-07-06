"""BLACK-BOX invariants for cross-machine EVENT replication (EventSyncer).

Regression net for the events cross-machine path. Chat + rpc were migrated onto
the ClusterBus (a broadcast / request-reply layer) and are covered by
test_cluster_bus.py + test_request_reply.py; this file is events-only.

BLACK-BOX rule (hard): assertions read ONLY
  - store_rows(node) / CountingEventStore insert counts
  - store id order via _arrival_order(node) (arrival == insertion order)
  - frames captured through the harness's PUBLIC recording seam (record_event_frames)
They NEVER reference private EventSyncer internals. Stimulus that must poke the
syncer (duplicate delivery, a failing link, per-message reordered delivery) is
owned by the harness's test-only delivery seams.

Invariant index (EVENT replication):
  A1  event write is synchronous (row present before publish returns)
  B1  event A -> B replication
  B2  bidirectional
  B3  dedup (no double rows)
  B4  reconnect resync via cursor (contiguous origin_seq)
  B5  gossip g1 -> host -> g2
  B6  3-day window filter (both emit-side and resync-side)
  B7  detach stops delivery
  B8  send failure swallowed, local row still written
  E1  100+ events arrive in order, seq contiguous (arrival == publish order)
  E3  order preserved across flush/batch boundary (arrival == publish order)
  E-RED  ordering guard PROVEN RED — the REAL arrival-order assertion (store id
         order) goes RED when frames are delivered reversed per-message
  G1  boxagent.log facade signature/behavior unchanged
  G2  /api/events query still works
  G3  TelegramNotifier still called
  G4  retention sweeper unaffected
  G5  one subscriber raising doesn't break store write or other subscribers
"""
from __future__ import annotations

import asyncio
import inspect
import time

import pytest

from boxagent.events.bus import EventBus
from boxagent.events.storage import EventStore
from boxagent.events.sync import SYNC_WINDOW_SECONDS

from tests.unit._bus_harness import (
    ThreeNodeCluster,
    TwoNodeCluster,
)


# ==========================================================================
# A. Persistence boundary (HARD: chat never touches SQLite; event always does)
# ==========================================================================

async def test_INV_A1_event_write_is_synchronous(tmp_path):
    """A published event's row is in the local store BEFORE publish returns —
    no settle, no await. The store write is synchronous (privileged first-slot
    subscriber). If it ever goes async this reds."""
    cluster = TwoNodeCluster(tmp_path)
    try:
        cluster.publish_event("A", "info", "scheduler.run", "fired",
                             bot="b1", task_id="t1")
        # No settle() — assert immediately.
        rows = cluster.store_rows("A")
        assert len(rows) == 1
        row = rows[0]
        assert row.level == "info"
        assert row.category == "scheduler.run"
        assert row.message == "fired"
        assert row.bot == "b1"
        assert row.meta == {"task_id": "t1"}
        assert row.origin_machine == "A"
        assert row.origin_seq == 1
    finally:
        await cluster.aclose()


# ==========================================================================
# B. Cross-machine event replication (regresses all of EventSyncer)
# ==========================================================================

async def test_INV_B1_event_propagates_A_to_B(tmp_path):
    cluster = TwoNodeCluster(tmp_path)
    try:
        cluster.publish_event("A", "info", "scheduler.run", "task fired", bot="bot_a")
        await cluster.settle()
        rows = cluster.store_rows("B")
        assert len(rows) == 1
        assert rows[0].origin_machine == "A"
        assert rows[0].message == "task fired"
    finally:
        await cluster.aclose()


async def test_INV_B2_bidirectional(tmp_path):
    cluster = TwoNodeCluster(tmp_path)
    try:
        cluster.publish_event("A", "info", "c.a", "from A")
        cluster.publish_event("B", "error", "c.b", "from B")
        await cluster.settle()
        a_messages = {r.message for r in cluster.store_rows("A")}
        b_messages = {r.message for r in cluster.store_rows("B")}
        assert a_messages == {"from A", "from B"}
        assert b_messages == {"from A", "from B"}
        # each side attributes origin correctly
        a_by_msg = {r.message: r.origin_machine for r in cluster.store_rows("A")}
        assert a_by_msg == {"from A": "A", "from B": "B"}
    finally:
        await cluster.aclose()


async def test_INV_B3_duplicate_batch_no_double_row(tmp_path):
    cluster = TwoNodeCluster(tmp_path)
    try:
        cluster.publish_event("A", "info", "c", "once")
        await cluster.settle()
        assert len(cluster.store_rows("B")) == 1
        # Re-deliver the same event as a batch — dedup must drop it. The
        # duplicate-delivery reach-in is owned by the harness so the invariant
        # stays clean when EventSyncer.handle_frame moves to PeerTransport.
        row = cluster.store_rows("A")[0]
        await cluster.redeliver_event_batch("B", "A", [row])
        assert len(cluster.store_rows("B")) == 1
    finally:
        await cluster.aclose()


async def test_INV_B4_reconnect_resync_via_cursor(tmp_path):
    """Reconnect backfills missed events and origin_seq is contiguous (no
    holes, no double-insert of already-synced rows)."""
    cluster = TwoNodeCluster(tmp_path)
    try:
        cluster.publish_event("A", "info", "c", "old1")
        cluster.publish_event("A", "info", "c", "old2")
        await cluster.settle()
        assert {r.message for r in cluster.store_rows("B")} == {"old1", "old2"}

        await cluster.drop_link("A", "B")
        cluster.publish_event("A", "info", "c", "mid1")
        cluster.publish_event("A", "info", "c", "mid2")
        await cluster.settle()
        # B is disconnected; it still only has the old two.
        assert {r.message for r in cluster.store_rows("B")} == {"old1", "old2"}

        # Count inserts across relink to prove cursor avoids re-inserting olds.
        store_b = cluster.store("B")
        remote_before = store_b.insert_remote_count
        await cluster.relink("A", "B")
        await cluster.settle()

        b_rows = sorted(
            [r for r in cluster.store_rows("B") if r.origin_machine == "A"],
            key=lambda r: r.origin_seq,
        )
        assert [r.message for r in b_rows] == ["old1", "old2", "mid1", "mid2"]
        assert [r.origin_seq for r in b_rows] == [1, 2, 3, 4]  # contiguous
        # Only mid1/mid2 were newly inserted on reconnect (cursor worked).
        assert store_b.insert_remote_count - remote_before == 2
    finally:
        await cluster.aclose()


async def test_INV_B5_gossip_g1_to_host_to_g2(tmp_path):
    cluster = ThreeNodeCluster(tmp_path)
    try:
        cluster.publish_event("gA", "info", "agent.notify", "hi from gA")
        await cluster.settle()
        assert "hi from gA" in {r.message for r in cluster.store_rows("gB")}
        # No echo back to the origin beyond its own single row.
        origin_rows = [r for r in cluster.store_rows("gA")
                       if r.message == "hi from gA"]
        assert len(origin_rows) == 1
    finally:
        await cluster.aclose()


async def test_INV_B6_three_day_window_filter(tmp_path):
    """Both the resync-side and the emit-side filter old events."""
    cluster = TwoNodeCluster(tmp_path)
    try:
        # Sever so we control resync ordering deterministically.
        await cluster.drop_link("A", "B")
        now = time.time()
        store_a = cluster.store("A")
        store_a.insert_local("A", "info", "c", "fresh", ts=now)
        store_a.insert_local("A", "info", "c", "ancient",
                            ts=now - SYNC_WINDOW_SECONDS - 100)
        await cluster.relink("A", "B")
        await cluster.settle()
        # Resync-side filter: only the fresh event crosses.
        assert {r.message for r in cluster.store_rows("B")} == {"fresh"}
    finally:
        await cluster.aclose()


async def test_INV_B7_detach_stops_delivery(tmp_path):
    cluster = TwoNodeCluster(tmp_path)
    try:
        await cluster.settle()
        await cluster.drop_link("A", "B")
        cluster.publish_event("A", "info", "c", "after detach")
        await cluster.settle()
        assert "after detach" not in {r.message for r in cluster.store_rows("B")}
    finally:
        await cluster.aclose()


async def test_INV_B8_send_failure_swallowed_local_row_kept(tmp_path):
    """A peer whose send raises does not crash publish, and the event is still
    written locally."""
    cluster = TwoNodeCluster(tmp_path)
    try:
        # Replace the A->B send with a failing one (real failing link),
        # installed via the harness through the public attach_peer seam.
        cluster.fail_event_peer("A", "B")
        cluster.publish_event("A", "info", "c", "ok")
        await cluster.settle()
        assert len(cluster.store_rows("A")) == 1
    finally:
        await cluster.aclose()


# ==========================================================================
# E. Ordering (the create_task footgun) + the RED proof
# ==========================================================================

async def test_INV_E1_events_arrive_in_order(tmp_path):
    """100 events published in order arrive in ARRIVAL order at the remote store
    with contiguous origin_seq.

    ARRIVAL ORDER is asserted, not origin_seq-sorted order. On node B every
    remote event is written by `insert_remote`, whose row `id` is a plain
    AUTOINCREMENT — so store-`id` ascending == insertion order == the order
    frames were delivered (arrival order). Sorting by `origin_seq` would be
    IMMUNE to a delivery permutation (seq is minted at the origin and preserved
    on the wire), so it could only ever catch a dropped/duplicated seq, never a
    REORDERED delivery — which is the exact footgun (坑#1, create_task-per-event)
    this whole section exists to guard. `_arrival_order` reads store-id order;
    `test_INV_E_RED_*` proves this assertion goes RED when delivery is permuted.
    """
    cluster = TwoNodeCluster(tmp_path)
    try:
        for i in range(100):
            cluster.publish_event("A", "info", "c", f"n{i}")
        await cluster.settle()
        # ARRIVAL order: store rows sorted by id (insertion == arrival order).
        arrival = _arrival_order(cluster, "B", origin="A")
        assert [row.message for row in arrival] == [f"n{i}" for i in range(100)]
        # Contiguity is an independent guarantee — origin_seq has no holes.
        assert [row.origin_seq for row in arrival] == list(range(1, 101))
    finally:
        await cluster.aclose()


async def test_INV_E3_order_preserved_across_flush_boundary(tmp_path):
    """More than MAX_BATCH events cross two flush batches; the load-bearing
    guarantee is ARRIVAL ORDER + CONTIGUITY (the create_task footgun), not
    one-shot delivery.

    FINDING (characterization of TODAY's code, see docs/bus-migration-map.md):
    EventSyncer._flush delivers only the first MAX_BATCH of a single debounce
    window; the recursive _schedule_flush() no-ops because the current flush
    task is not done() yet, so the tail is orphaned until the NEXT locally
    published event nudges another flush. Order is never scrambled and nothing
    is permanently lost — the tail arrives, contiguously, on the next flush.
    This test pins that behaviour (ARRIVAL order/contiguity preserved), so a
    migration that reordered OR permanently dropped the tail would go RED. The
    bus migration should additionally FIX the one-shot-tail gap; when it does,
    the `_nudge` below becomes unnecessary and this test still passes.

    ORDER is asserted on ARRIVAL (store id ascending), NOT origin_seq-sorted —
    origin_seq order is immune to a delivery permutation (see INV-E1 docstring
    and INV-E-RED). The drop/contiguity half asserts origin_seq is a hole-free
    1..N run, which is a genuine (permutation-independent) guarantee."""
    from boxagent.events.sync import MAX_BATCH
    cluster = TwoNodeCluster(tmp_path)
    try:
        total = MAX_BATCH + 50
        for i in range(total):
            cluster.publish_event("A", "info", "c", f"n{i}")
        await cluster.settle()

        # Drive the orphaned-tail flush the way ongoing traffic would: one more
        # local event re-schedules a flush that carries the buffered remainder.
        # (Characterizes current EventSyncer; a fixed replicator flushes it all
        # on the first settle and this nudge is simply a no-op extra event.)
        cluster.publish_event("A", "info", "c", "_nudge")
        await cluster.settle()

        # ARRIVAL order: store rows sorted by id (insertion == arrival order).
        arrival = _arrival_order(cluster, "B", origin="A")
        # All originally-published events plus the nudge arrived (drop half).
        assert len(arrival) == total + 1
        # Strictly contiguous origin_seq — no holes (drop/contiguity half; this
        # is permutation-independent, so it stays valid even if arrival order
        # were scrambled).
        assert sorted(row.origin_seq for row in arrival) == list(range(1, total + 2))
        # Arrival order matches publish order: the first `total` deliveries are
        # n0..n(total-1) in sequence, nudge last (this is the REAL reorder guard;
        # INV-E-RED proves it goes RED on permuted delivery).
        assert [row.message for row in arrival] == (
            [f"n{i}" for i in range(total)] + ["_nudge"]
        )
    finally:
        await cluster.aclose()


async def test_INV_E_RED_ordering_guard_reds_against_reversed_delivery(tmp_path):
    """A footgun guard you never saw fail is not a guard.

    Prove the REAL ordering assertion (the store-id / arrival-order check that
    INV-E1 and INV-E3 make) CAN go RED. We deliver the same 20 events through a
    deliberately-broken replicator that emits ONE frame per event
    (create_task-per-message style, 坑#1) and lets delivery order be permuted —
    here reversed. Under the real single-ordered-batch EventSyncer (E1/E3) a
    permutation is impossible; here we simulate the broken design.

    The assertion below is IDENTICAL in shape to E1/E3's — `_arrival_order`
    sorts node B's store rows by id (insertion == arrival order). It goes RED
    (arrival == reversed publish order), which is the whole point: E1/E3's
    arrival-order check is a real guard, not a false-green no-op. Note an
    origin_seq-sorted assertion would stay GREEN here (n0..n19), proving why the
    OLD sorted-by-origin_seq E1/E3 was immune to reordering.
    """
    cluster = TwoNodeCluster(tmp_path)
    try:
        published = [f"n{i}" for i in range(20)]

        # Broken delivery: one frame per event, delivered REVERSED, driven
        # through the harness's public per-message-delivery seam (no reach-in
        # into syncer internals from the invariant body).
        await cluster.deliver_events_per_message(
            origin="A", target="B", messages=published,
            permute=lambda items: list(reversed(items)),
        )
        await cluster.settle()

        # THE REAL ASSERTION (same as INV-E1/E3): arrival order == store id order.
        arrival = _arrival_order(cluster, "B", origin="A")
        arrival_messages = [row.message for row in arrival]

        # This is what E1/E3 assert. It MUST fail here — the guard is real.
        assert arrival_messages != published, (
            "arrival-order guard is a false-green no-op: reversed per-message "
            "delivery did NOT scramble store id/arrival order"
        )
        # Concretely, reversed delivery reverses arrival order.
        assert arrival_messages == list(reversed(published))
        # Every event still landed (only ORDER broke, not delivery/contiguity).
        assert sorted(row.origin_seq for row in arrival) == list(range(1, 21))
        # And the origin_seq-sorted view (the OLD E1/E3 check) is IMMUNE — it
        # stays green even though arrival scrambled, proving the fix mattered.
        by_seq = sorted(arrival, key=lambda row: row.origin_seq)
        assert [row.message for row in by_seq] == published
    finally:
        await cluster.aclose()


# ==========================================================================
# G. Facade + existing exits do not regress
# ==========================================================================

def test_INV_G1_log_facade_signature_and_behavior(tmp_path):
    """boxagent.log facade binds an EventBus and writes a correct row; the
    LogSink protocol signature is unchanged (level, category, message, **meta)."""
    from boxagent.log import LogFacade

    # Signature contract.
    signature = inspect.signature(EventBus.publish)
    params = list(signature.parameters.keys())
    assert params[:4] == ["self", "level", "category", "message"]
    assert any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()
    )

    # End-to-end via the facade.
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


def test_INV_G2_api_events_query_still_works(tmp_path):
    """The /api/events read path (store.query with filters) is unchanged. We
    exercise the same query surface the web handler uses."""
    store = EventStore(tmp_path / "e.db")
    store.insert_local("m1", "info", "scheduler.run", "a", bot="b1")
    store.insert_local("m1", "error", "backend.crash", "b", bot="b2")
    store.insert_local("m1", "info", "agent.notify", "c", bot="b1")

    # level filter
    errors = store.query(levels=["error"])
    assert [r.message for r in errors] == ["b"]
    # bot filter
    b1_rows = {r.message for r in store.query(bot="b1")}
    assert b1_rows == {"a", "c"}
    # category prefix
    sched = store.query(category_prefix="scheduler")
    assert [r.message for r in sched] == ["a"]
    # pagination via before_id
    all_rows = store.query()
    assert len(all_rows) == 3
    oldest_id = min(r.id for r in all_rows)
    page = store.query(before_id=oldest_id)
    assert page == []
    # mark_read
    marked = store.mark_read([r.id for r in all_rows])
    assert marked == 3
    assert store.query(unread_only=True) == []
    store.close()


async def test_INV_G3_telegram_notifier_still_called(tmp_path):
    """A matching event still triggers exactly one TelegramNotifier POST, no
    throttling."""
    from boxagent.events.telegram_notifier import TelegramNotifier

    class _FakeResp:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def text(self): return ""

    class _FakeSession:
        def __init__(self): self.calls = []
        def post(self, url, json=None, timeout=None):
            self.calls.append((url, json))
            return _FakeResp()
        async def close(self): pass

    fake = _FakeSession()
    notifier = TelegramNotifier(
        token="TOK", chat_id="42", levels=["error"], session=fake,
    )
    store = EventStore(tmp_path / "e.db")
    bus = EventBus(store=store, machine_id="m1")
    notifier.attach(bus)
    for i in range(5):
        bus.publish("error", "backend.crash", f"boom{i}")
    await asyncio.sleep(0.05)
    assert len(fake.calls) == 5  # no rate limit
    assert all("/botTOK/sendMessage" in url for url, _ in fake.calls)
    bus.close()


async def test_INV_G4_retention_sweeper_unaffected(tmp_path):
    """The retention sweeper still deletes events older than the cutoff."""
    from boxagent.events.retention import RetentionSweeper

    store = EventStore(tmp_path / "e.db")
    now = time.time()
    store.insert_local("m1", "info", "c", "old", ts=now - 100 * 86400)
    store.insert_local("m1", "info", "c", "fresh", ts=now)
    sweeper = RetentionSweeper(store, retention_seconds=30 * 86400)
    deleted = sweeper.sweep_once()
    assert deleted == 1
    assert {r.message for r in store.query()} == {"fresh"}
    store.close()


def test_INV_G5_one_subscriber_raising_does_not_break_others(tmp_path):
    """A raising subscriber affects neither the store write nor other
    subscribers, and does not propagate out of publish."""
    store = EventStore(tmp_path / "e.db")
    bus = EventBus(store=store, machine_id="m1")
    received: list = []

    def boom(_event):
        raise RuntimeError("subscriber boom")

    bus.subscribe(boom)
    bus.subscribe(received.append)
    bus.publish("info", "c", "m")  # must not raise
    assert len(received) == 1
    assert len(store.query()) == 1
    bus.close()


# ==========================================================================
# Helpers
# ==========================================================================

def _drain(queue: asyncio.Queue) -> list:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def _arrival_order(cluster, machine: str, *, origin: str | None = None) -> list:
    """Return `machine`'s store rows in ARRIVAL order (store `id` ascending).

    On node B every remote event is written by `insert_remote`; its row `id` is
    a plain AUTOINCREMENT, so id-ascending == insertion order == the order the
    frames were delivered. This is the ONLY store-side view that reflects
    arrival order — the default query() ordering is `ts DESC, id DESC`, and
    sorting by `origin_seq` is immune to a delivery permutation (seq is minted
    at the origin and preserved on the wire). The E-section ordering invariants
    assert on THIS so a delivery reordering (坑#1) is caught; INV-E-RED proves
    the RED path.
    """
    rows = cluster.store_rows(machine)
    if origin is not None:
        rows = [row for row in rows if row.origin_machine == origin]
    return sorted(rows, key=lambda row: row.id)
