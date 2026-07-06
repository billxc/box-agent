"""Self-tests for the bus harness (Phase 0).

These are NOT the frozen invariants — they prove the harness itself works, so
that the real invariants in test_message_bus_invariants.py are not falsely
green because the plumbing is broken. Mirrors the tester's demand for
`test_bus_harness.py::test_link_delivers_frame_both_directions` etc.
"""
from __future__ import annotations

from tests.unit._bus_harness import (
    ThreeNodeCluster,
    TwoNodeCluster,
    CountingEventStore,
)


async def test_link_delivers_event_both_directions(tmp_path):
    cluster = TwoNodeCluster(tmp_path)
    try:
        cluster.publish_event("A", "info", "c", "from A")
        cluster.publish_event("B", "info", "c", "from B")
        await cluster.settle()
        a_messages = {row.message for row in cluster.store_rows("A")}
        b_messages = {row.message for row in cluster.store_rows("B")}
        assert a_messages == {"from A", "from B"}
        assert b_messages == {"from A", "from B"}
    finally:
        await cluster.aclose()


async def test_settle_waits_for_debounce(tmp_path):
    cluster = TwoNodeCluster(tmp_path)
    try:
        cluster.publish_event("A", "info", "c", "delayed")
        # Before settle the peer may not have it yet (debounce not flushed).
        await cluster.settle()
        # After settle it must be there.
        assert any(r.message == "delayed" for r in cluster.store_rows("B"))
    finally:
        await cluster.aclose()


async def test_counting_store_counts_inserts(tmp_path):
    store = CountingEventStore(tmp_path / "s.db")
    assert store.total_inserts == 0
    store.insert_local("m", "info", "c", "x")
    assert store.insert_local_count == 1
    assert store.total_inserts == 1
    store.close()


async def test_three_node_gossip(tmp_path):
    cluster = ThreeNodeCluster(tmp_path)
    try:
        cluster.publish_event("gA", "info", "agent.notify", "hello from gA")
        await cluster.settle()
        gb_messages = {row.message for row in cluster.store_rows("gB")}
        assert "hello from gA" in gb_messages
    finally:
        await cluster.aclose()


async def test_reorder_hook_permutes(tmp_path):
    """Proves the reorder controller actually buffers + permutes deliveries."""
    cluster = TwoNodeCluster(tmp_path)
    try:
        cluster.reorder.armed = True
        cluster.reorder.permute = lambda items: list(reversed(items))
        # Publish several; deliveries are buffered while armed.
        for i in range(5):
            cluster.publish_event("A", "info", "c", f"n{i}")
        await cluster.settle()
        # Buffered — B has nothing yet.
        assert cluster.store_rows("B") == []
        await cluster.reorder.release()
        await cluster.settle()
        assert len({r.message for r in cluster.store_rows("B")}) == 5
    finally:
        await cluster.aclose()
