"""FROZEN, append-only, BLACK-BOX invariants for the MessageBus migration.

This file is the regression net for unifying BoxAgent's event + chat delivery
onto one bus. It is written against TODAY's unmodified product code and must
stay GREEN through every migration phase. Later phases only ADD invariants; no
existing invariant's expected value may change (a changed expectation is a
behavior regression, not a test refactor — stop and ask the owner).

BLACK-BOX rule (hard): assertions read ONLY
  - store_rows(node) / CountingEventStore insert counts
  - store id order via _arrival_order(node) (arrival == insertion order)
  - chat subscriber queue contents
  - frames captured through the harness's PUBLIC recording seams
    (record_event_frames / record_chat_frames — installed via attach_peer)
They NEVER reference private state (_subscribers, _pumps, _buffer, _peers, or
any EventSyncer / ChatSyncer internals). Stimulus that must poke a syncer
(duplicate delivery, a failing link, per-message reordered delivery) is owned by
the harness's test-only delivery seams, so this file stays clean and only the
harness updates when the syncers move to PeerTransport in Phase 1. The one
tolerated exception is the harness's own settle()/quiescence poll, which is test
infrastructure, not an assertion.

Invariant index (this phase — EVENT + CHAT + RPC round-trip):
  A1  event write is synchronous (row present before publish returns)
  A2  chat stream_delta NEVER hits SQLite (negative test, the head no-go)
  A3  per-topic durability is declarative & enumerable (param table)
  B1  event A -> B replication
  B2  bidirectional
  B3  dedup (no double rows)
  B4  reconnect resync via cursor (contiguous origin_seq)
  B5  gossip g1 -> host -> g2
  B6  3-day window filter (both emit-side and resync-side)
  B7  detach stops delivery
  B8  send failure swallowed, local row still written
  C1  subscribed chat reaches queue cross-machine; unsubscribed does not
  C2  refcount: two local watchers -> one upstream subscribe
  C3  two-hop host relay
  C6  subscriber reconnect re-sends subscribe
  D1  ONE reconnect recovers BOTH event backfill AND chat re-subscribe
  D3  cross-machine chat never hits either store
  E1  100+ events arrive in order, seq contiguous (arrival == publish order)
  E2  100+ chat deltas arrive in order
  E3  order preserved across flush/batch boundary (arrival == publish order)
  E-RED  ordering guard PROVEN RED — the REAL arrival-order assertion (store id
         order) goes RED when frames are delivered reversed per-message
  F1  slow subscriber drops its own; fast subscriber unaffected
  G1  boxagent.log facade signature/behavior unchanged
  G2  /api/events query still works
  G3  TelegramNotifier still called
  G4  retention sweeper unaffected
  G5  one subscriber raising doesn't break store write or other subscribers
  R1  RPC single hop host->guest returns guest's real body, id-correlated
  R2  reverse RPC loopback re-issue hits the REAL host handler (spy-proven)
  R3  two-hop gA->host->gB returns correct body (nested pending pairs)
  R4  50 concurrent out-of-order replies never cross (id correlation) — key one
  R5  unreachable machine times out cleanly + no pending-future leak
  R6  RPC is concurrent, not serialized behind one pump
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


async def test_INV_A2_chat_stream_delta_never_hits_sqlite(tmp_path):
    """THE NEGATIVE TEST. 200 chat stream_delta publishes ->
    (a) CountingEventStore insert-delta == 0, and
    (b) before/after store snapshot equal.
    Chat physically cannot reach SQLite."""
    cluster = TwoNodeCluster(tmp_path)
    try:
        queue = await cluster.subscribe_chat("A", "A", "b", "c")
        store = cluster.store("A")

        before_rows = cluster.store_rows("A")
        before_local = store.insert_local_count
        before_remote = store.insert_remote_count

        for i in range(200):
            cluster.publish_chat("A", "b", "c", {
                "type": "stream_delta", "delta": "x", "message_id": "m1", "seq": i,
            })

        await cluster.settle()

        # (a) spy insert-delta is exactly zero across the whole burst.
        assert store.insert_local_count == before_local
        assert store.insert_remote_count == before_remote
        # (b) store snapshot identical (no rows in any category).
        after_rows = cluster.store_rows("A")
        assert after_rows == before_rows
        assert cluster.store_rows("A", category_prefix="chat") == []
        # And the deltas really did fan out to the subscriber (200 of them).
        assert queue.qsize() == 200
    finally:
        await cluster.aclose()


async def test_INV_A3_per_topic_durability_is_declarative(tmp_path):
    """Enumerated table: event topics increment the store by 1; chat topics
    (message / stream_delta / tool_call / typing) increment by 0. Today this
    is enforced by which code path is used (bus.publish vs channel._publish);
    post-unification it becomes the subscriber-list-is-the-policy fact."""
    cluster = TwoNodeCluster(tmp_path)
    try:
        # Ensure an owner-side subscriber so chat actually fans out.
        await cluster.subscribe_chat("A", "A", "b", "c")
        store = cluster.store("A")

        durable_cases = [
            ("scheduler.run", "info"),
            ("agent.notify", "info"),
            ("backend.crash", "error"),
        ]
        for category, level in durable_cases:
            before = store.total_inserts
            cluster.publish_event("A", level, category, "x")
            assert store.total_inserts == before + 1, f"{category} should be durable"

        ephemeral_cases = [
            {"type": "message", "text": "hi"},
            {"type": "stream_delta", "delta": "d", "message_id": "m"},
            {"type": "tool_call", "tool_id": "t", "name": "n", "args": {}},
            {"type": "typing"},
        ]
        for event in ephemeral_cases:
            before = store.total_inserts
            cluster.publish_chat("A", "b", "c", event)
            await cluster.settle()
            assert store.total_inserts == before, f"{event['type']} must be ephemeral"
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
# C. Cross-machine chat subscription (regresses ChatSyncer)
# ==========================================================================

async def test_INV_C1_subscribed_reaches_queue_unsubscribed_does_not(tmp_path):
    cluster = TwoNodeCluster(tmp_path)
    try:
        queue = await cluster.subscribe_chat("B", "A", "b", "c")
        cluster.publish_chat("A", "b", "c", {"type": "message", "text": "watched"})
        cluster.publish_chat("A", "b", "OTHER", {"type": "message", "text": "unwatched"})
        await cluster.wait_for_queue(queue, 1)
        await cluster.settle()
        items = _drain(queue)
        assert len(items) == 1
        assert items[0]["text"] == "watched"
    finally:
        await cluster.aclose()


async def test_INV_C2_refcount_two_watchers_one_upstream(tmp_path):
    """Two local browsers on the same remote chat produce exactly one upstream
    chat_subscribe frame; last-leave produces one chat_unsubscribe."""
    cluster = TwoNodeCluster(tmp_path)
    try:
        # Capture frames B sends toward A via the harness's public recording
        # seam (installed through attach_peer — no _peers reach-in).
        sent = cluster.record_chat_frames("B", "A")

        queue1 = await cluster.subscribe_chat("B", "A", "b", "c")
        queue2 = await cluster.subscribe_chat("B", "A", "b", "c")
        subs = [f for f in sent if f.get("type") == "chat_subscribe"]
        assert len(subs) == 1

        cluster.publish_chat("A", "b", "c", {"type": "message", "n": 1})
        await cluster.wait_for_queue(queue1, 1)
        await cluster.wait_for_queue(queue2, 1)
        assert queue1.qsize() == 1 and queue2.qsize() == 1

        node_b_bus = cluster.nodes["B"].chat_bus
        await node_b_bus.unsubscribe("b", "c", "A", queue1)
        await cluster.settle()  # upstream frames ride the ordered async drain
        unsubs = [f for f in sent if f.get("type") == "chat_unsubscribe"]
        assert unsubs == []  # one watcher left
        await node_b_bus.unsubscribe("b", "c", "A", queue2)
        await cluster.settle()
        unsubs = [f for f in sent if f.get("type") == "chat_unsubscribe"]
        assert len(unsubs) == 1
    finally:
        await cluster.aclose()


async def test_INV_C3_two_hop_host_relay(tmp_path):
    """gA subscribes to gB's bot; host relays subscribe to gB and events back."""
    cluster = ThreeNodeCluster(tmp_path)
    try:
        queue = await cluster.subscribe_chat("gA", "gB", "b", "c")
        cluster.publish_chat("gB", "b", "c", {"type": "message", "text": "relayed"})
        await cluster.wait_for_queue(queue, 1)
        items = _drain(queue)
        assert len(items) == 1
        assert items[0]["text"] == "relayed"
    finally:
        await cluster.aclose()


async def test_INV_C6_subscriber_reconnect_resends_subscribe(tmp_path):
    """After a WS reconnect, the subscriber re-establishes its subscription and
    resumes receiving NEW deltas."""
    cluster = TwoNodeCluster(tmp_path)
    try:
        queue = await cluster.subscribe_chat("B", "A", "b", "c")
        cluster.publish_chat("A", "b", "c", {"type": "message", "n": 1})
        await cluster.wait_for_queue(queue, 1)
        assert _drain(queue)[0]["n"] == 1

        await cluster.drop_link("A", "B")
        await cluster.relink("A", "B")
        # Give the owner-side pump time to re-subscribe after re-subscribe frame.
        await cluster.settle()
        cluster.publish_chat("A", "b", "c", {"type": "message", "n": 2})
        await cluster.wait_for_queue(queue, 1)
        items = _drain(queue)
        assert items and items[-1]["n"] == 2
    finally:
        await cluster.aclose()


# ==========================================================================
# D. Cross invariants (the risk unification introduces)
# ==========================================================================

async def test_INV_D1_one_reconnect_recovers_events_and_chat(tmp_path):
    """The single most important new test. In ONE reconnect:
      (a) event: B.store backfills the 2 events dropped during the outage
      (b) chat: B's queue resumes receiving NEW deltas after re-subscribe
    The 3 deltas dropped during the outage may be lost (chat is live)."""
    cluster = TwoNodeCluster(tmp_path)
    try:
        queue = await cluster.subscribe_chat("B", "A", "b", "c")
        # Baseline: chat works, event works.
        cluster.publish_chat("A", "b", "c", {"type": "message", "n": 0})
        cluster.publish_event("A", "info", "c", "e0")
        await cluster.wait_for_queue(queue, 1)
        await cluster.settle()
        assert _drain(queue)[0]["n"] == 0
        assert "e0" in {r.message for r in cluster.store_rows("B")}

        # Outage.
        await cluster.drop_link("A", "B")
        cluster.publish_event("A", "info", "c", "e1")
        cluster.publish_event("A", "info", "c", "e2")
        cluster.publish_chat("A", "b", "c", {"type": "message", "n": 1})
        cluster.publish_chat("A", "b", "c", {"type": "message", "n": 2})
        cluster.publish_chat("A", "b", "c", {"type": "message", "n": 3})
        await cluster.settle()
        # B saw none of it while disconnected.
        assert {r.message for r in cluster.store_rows("B")} == {"e0"}

        # Reconnect: events backfill via cursor, chat re-subscribes.
        await cluster.relink("A", "B")
        await cluster.settle()

        # (a) events backfilled.
        b_messages = {r.message for r in cluster.store_rows("B")}
        assert {"e0", "e1", "e2"} <= b_messages

        # (b) chat resumes for NEW deltas.
        cluster.publish_chat("A", "b", "c", {"type": "message", "n": 4})
        await cluster.wait_for_queue(queue, 1)
        new_items = _drain(queue)
        assert any(item.get("n") == 4 for item in new_items)
    finally:
        await cluster.aclose()


async def test_INV_D3_cross_machine_chat_never_hits_either_store(tmp_path):
    """The cross-machine version of A2: a delta published on A and watched from
    B increments NEITHER store's insert count."""
    cluster = TwoNodeCluster(tmp_path)
    try:
        queue = await cluster.subscribe_chat("B", "A", "b", "c")
        store_a = cluster.store("A")
        store_b = cluster.store("B")
        a_before = store_a.total_inserts
        b_before = store_b.total_inserts
        for i in range(50):
            cluster.publish_chat("A", "b", "c", {
                "type": "stream_delta", "delta": str(i), "message_id": "m",
            })
        await cluster.wait_for_queue(queue, 50)
        await cluster.settle()
        assert store_a.total_inserts == a_before
        assert store_b.total_inserts == b_before
        assert queue.qsize() == 50
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


async def test_INV_E2_chat_deltas_arrive_in_order(tmp_path):
    """100 stream_delta published in order arrive in order at the remote
    subscriber queue."""
    cluster = TwoNodeCluster(tmp_path)
    try:
        queue = await cluster.subscribe_chat("B", "A", "b", "c")
        for i in range(100):
            cluster.publish_chat("A", "b", "c", {
                "type": "stream_delta", "delta": str(i), "message_id": "m",
            })
        await cluster.wait_for_queue(queue, 100)
        await cluster.settle()
        deltas = [item["delta"] for item in _drain(queue)]
        assert deltas == [str(i) for i in range(100)]
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
# F. Backpressure / slow subscriber isolation
# ==========================================================================

async def test_INV_F1_slow_subscriber_drops_without_stalling_fast(tmp_path):
    """Two subscribers on the same chat: the fast one drains everything, the
    slow one never drains. The slow one's bounded queue caps at maxsize and
    drops the overflow WITHOUT raising and WITHOUT starving the fast one."""
    from boxagent.transports.web.channel import WebChannel
    from boxagent.cluster.chat_sync import QUEUE_MAXSIZE
    cluster = TwoNodeCluster(tmp_path)
    try:
        # Local same-machine fan-out: two subscribers on one chat, via the real
        # ChatBus subscribe path (bus.subscribe + bounded QueueSubscriber).
        channel: WebChannel = cluster.owner_channel("A", "b")
        fast_queue = await cluster.subscribe_chat("A", "A", "b", "c")
        slow_queue = await cluster.subscribe_chat("A", "A", "b", "c")

        overflow = QUEUE_MAXSIZE + 100
        for i in range(overflow):
            channel._publish("c", {"type": "stream_delta", "delta": str(i)})
            # Fast subscriber drains as it goes.
            while not fast_queue.empty():
                fast_queue.get_nowait()

        # Slow queue capped, never raised.
        assert slow_queue.qsize() <= slow_queue.maxsize
        # Fast subscriber was never blocked — it received the last publish.
        channel._publish("c", {"type": "stream_delta", "delta": "final"})
        assert fast_queue.get_nowait()["delta"] == "final"
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
# RPC round-trip invariants (R1..R6) — correlated request/reply.
#
# RPC is the OTHER delivery semantic (the fan-out shuttle above is
# broadcast/subscribe). Its load-bearing detail is the LOOPBACK RE-ISSUE: an
# inbound RPC frame replays against the node's OWN web port and re-runs the REAL
# `_handle_web_*` handler (`GuestRegistry._serve_inbound_rpc` on the host,
# `GuestClient._handle_rpc` on the guest). A pure in-process fake would defeat
# INV-R2 ("the loopback hits the REAL handler"), so these run against
# `tests/unit/_rpc_bus_harness.py`: a real `aiohttp` server per node linked over
# a real `GuestRegistry`/`GuestClient` WebSocket (production wiring minus the
# devtunnel dial).
#
# BLACK-BOX: assertions read ONLY the returned body dict, the per-node spy
# records (method/path/query/body of the REAL handler), and pending_rpc_count.
# They are GREEN against CURRENT code (registry._serve_inbound_rpc +
# guest_client._handle_rpc + GuestSession.call / GuestClient.call). These
# collapse the host/guest RPC mirror at Phase 1.5; this net exists so that
# refactor has a guard.
# ==========================================================================

from tests.unit._rpc_bus_harness import (  # noqa: E402
    build_three_node as _build_three_node_rpc,
    build_two_node as _build_two_node_rpc,
)


async def test_INV_R1_single_hop_returns_real_body_correlated_by_id():
    """host → guest gB: the reply is gB's REAL response body, correlated to the
    request's rpc id (GuestSession.call resolves the right pending future)."""
    cluster = await _build_two_node_rpc()
    try:
        host = cluster.nodes["host"]
        guest = cluster.nodes["gB"]
        cluster.set_history(guest, [{"role": "user", "text": "gB-real-row"}])

        result = await cluster.rpc(host, "gB", "GET", "/api/history",
                                   query={"chat_id": "c1"})
        assert result["status"] == 200
        # gB's REAL handler produced the body (its machine id + controlled rows).
        assert result["body"]["machine"] == "gB"
        assert result["body"]["rows"] == [{"role": "user", "text": "gB-real-row"}]
        # No leaked pending futures after the reply resolved.
        assert cluster.pending_rpc_count(host) == 0
    finally:
        await cluster.aclose()


async def test_INV_R2_loopback_reissue_hits_the_real_handler():
    """guest gB → host: the reverse RPC's loopback re-issue actually runs the
    host's REAL /api/history handler (spy proves method/path/query/body), and
    returns real rows — not a shell."""
    cluster = await _build_two_node_rpc()
    try:
        host = cluster.nodes["host"]
        guest = cluster.nodes["gB"]
        cluster.set_history(host, [{"role": "assistant", "text": "host-real-row"}])

        result = await cluster.rpc(guest, "host", "GET", "/api/history",
                                   query={"chat_id": "cX", "bot": "b1"})
        assert result["status"] == 200
        assert result["body"]["machine"] == "host"
        assert result["body"]["rows"] == [{"role": "assistant", "text": "host-real-row"}]

        # The REAL host handler ran on loopback (spy captured it verbatim).
        host_history_calls = [
            call for call in cluster.spy(host) if call.path == "/api/history"
        ]
        assert len(host_history_calls) == 1
        call = host_history_calls[0]
        assert call.method == "GET"
        assert call.query.get("chat_id") == "cX"
        assert call.query.get("bot") == "b1"
        assert cluster.pending_rpc_count(guest) == 0
    finally:
        await cluster.aclose()


async def test_INV_R3_two_hop_gA_host_gB_returns_correct_body():
    """gA → host → gB: nested (rpc_id, _pending) pairs. gA's reverse RPC lands on
    the host loopback, whose REAL handler forwards to gB; gB's REAL body flows
    back through both hops uncrossed."""
    cluster = await _build_three_node_rpc()
    try:
        gA = cluster.nodes["gA"]
        host = cluster.nodes["host"]
        gB = cluster.nodes["gB"]
        cluster.set_session_info(gB, {"session_id": "gB-sess", "message_count": 7})

        result = await cluster.rpc(gA, "gB", "GET", "/api/session_info",
                                   query={"bot": "botB"})
        assert result["status"] == 200
        # The body originated at gB's REAL handler.
        assert result["body"]["machine"] == "gB"
        assert result["body"]["info"] == {"session_id": "gB-sess", "message_count": 7}

        # gB's real handler ran (second hop).
        assert any(
            call.path == "/api/session_info" and call.query.get("bot") == "botB"
            for call in cluster.spy(gB)
        )
        # The host's real handler ran too (first hop, the forwarding one).
        assert any(call.path == "/api/session_info" for call in cluster.spy(host))
        # No pending leaks on any originating node.
        assert cluster.pending_rpc_count(gA) == 0
        assert cluster.pending_rpc_count(host) == 0
    finally:
        await cluster.aclose()


async def test_INV_R4_fifty_concurrent_out_of_order_replies_never_cross():
    """THE MOST IMPORTANT ONE. 50 concurrent host→gB RPCs, each with a distinct
    payload, whose replies are held then released in REVERSED order. Every rpc
    id must resolve to ITS OWN reply — correlation by id, no crossing."""
    cluster = await _build_two_node_rpc()
    try:
        host = cluster.nodes["host"]
        guest = cluster.nodes["gB"]

        # Hold every rpc_resp gB is about to send, so all 50 are in-flight.
        cluster.hold_replies(guest, lambda frame: frame.get("type") == "rpc_resp")

        async def one(index: int) -> tuple[int, dict]:
            result = await cluster.rpc(host, "gB", "GET", "/api/echo",
                                       query={"n": str(index)}, timeout=10.0)
            return index, result

        tasks = [asyncio.create_task(one(i)) for i in range(50)]

        # Wait until all 50 replies are queued at the gate (all concurrently
        # in-flight — proves they are NOT serialized behind one pump).
        for _ in range(500):
            if cluster.held_reply_count(guest) >= 50:
                break
            await asyncio.sleep(0)
        assert cluster.held_reply_count(guest) == 50
        assert cluster.pending_rpc_count(host) == 50

        # Release in REVERSED order — the worst case for id-crossing.
        cluster.release_replies(guest, reverse=True)
        results = await asyncio.gather(*tasks)

        # Each request's own index came back in its own reply body.
        for index, result in results:
            assert result["status"] == 200
            assert result["body"]["machine"] == "gB"
            assert result["body"]["n"] == str(index), (
                f"reply crossed: request {index} got n={result['body']['n']}"
            )
        assert cluster.pending_rpc_count(host) == 0
    finally:
        await cluster.aclose()


async def test_INV_R5_unreachable_machine_times_out_and_cleans_pending():
    """R5 / NEG-R1: an RPC to a machine the host cannot reach times out CLEANLY
    (a 504-shaped error, not a hang) AND leaves no leaked pending future
    (pending_rpc_count returns to 0)."""
    cluster = await _build_two_node_rpc()
    try:
        host = cluster.nodes["host"]
        guest = cluster.nodes["gB"]

        # Hold gB's reply forever so the caller must hit its own timeout.
        cluster.hold_replies(guest, lambda frame: frame.get("type") == "rpc_resp")

        with pytest.raises(asyncio.TimeoutError):
            await cluster.rpc(host, "gB", "GET", "/api/echo",
                              query={"n": "1"}, timeout=0.2)

        # The pending future was cleaned up (GuestSession.call's finally pops it).
        assert cluster.pending_rpc_count(host) == 0

        # And the session is still usable once replies flow again.
        cluster.release_replies(guest)
    finally:
        await cluster.aclose()


async def test_INV_R6_rpc_is_concurrent_not_serialized():
    """Two slow RPCs to the same guest OVERLAP — RPC is concurrent, not
    serialized behind a single pump. If they were serialized, total wall time
    would be ~2x a single handler delay; concurrent, it is ~1x."""
    cluster = await _build_two_node_rpc()
    try:
        host = cluster.nodes["host"]
        guest = cluster.nodes["gB"]
        # gB's echo handler sleeps 0.3s before replying.
        cluster.set_handler_delay(guest, "/api/echo", 0.3)

        start = time.monotonic()
        results = await asyncio.gather(
            cluster.rpc(host, "gB", "GET", "/api/echo", query={"n": "a"}, timeout=5.0),
            cluster.rpc(host, "gB", "GET", "/api/echo", query={"n": "b"}, timeout=5.0),
        )
        elapsed = time.monotonic() - start

        assert {r["body"]["n"] for r in results} == {"a", "b"}
        # Concurrent: well under the 0.6s a serialized pump would take. Generous
        # upper bound to avoid CI flakiness while still excluding serialization.
        assert elapsed < 0.55, f"RPCs appear serialized (elapsed={elapsed:.2f}s)"
        assert cluster.pending_rpc_count(host) == 0
    finally:
        await cluster.aclose()

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
