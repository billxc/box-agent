"""Self-tests for the RPC bus harness (Phase 0).

These are NOT the frozen RPC invariants — they prove the RPC harness plumbing
itself works (real aiohttp servers, real GuestRegistry/GuestClient WS link, the
loopback re-issue, the reply gate), so the R1..R6 invariants in
test_message_bus_invariants.py are not falsely green because the wiring is
broken. Mirrors test_bus_harness.py for the fan-out shuttle.
"""
from __future__ import annotations

import asyncio

import pytest

from tests.unit._rpc_bus_harness import build_three_node, build_two_node


async def test_two_node_link_established():
    """The guest actually registers on the host over a real WS."""
    cluster = await build_two_node()
    try:
        host = cluster.nodes["host"]
        assert host.registry is not None
        assert host.registry.get("gB") is not None
    finally:
        await cluster.aclose()


async def test_host_to_guest_reaches_real_handler():
    cluster = await build_two_node()
    try:
        host = cluster.nodes["host"]
        guest = cluster.nodes["gB"]
        result = await cluster.rpc(host, "gB", "GET", "/api/echo", query={"n": "7"})
        assert result["status"] == 200
        assert result["body"]["machine"] == "gB"
        assert result["body"]["n"] == "7"
        # The guest's real handler ran (spy captured it).
        assert any(call.path == "/api/echo" for call in cluster.spy(guest))
    finally:
        await cluster.aclose()


async def test_guest_to_host_loopback_runs_real_host_handler():
    cluster = await build_two_node()
    try:
        host = cluster.nodes["host"]
        guest = cluster.nodes["gB"]
        cluster.set_history(host, [{"role": "assistant", "text": "H"}])
        result = await cluster.rpc(guest, "host", "GET", "/api/history")
        assert result["body"]["machine"] == "host"
        assert result["body"]["rows"] == [{"role": "assistant", "text": "H"}]
        assert any(call.path == "/api/history" for call in cluster.spy(host))
    finally:
        await cluster.aclose()


async def test_reply_gate_holds_and_releases():
    """The gate really buffers rpc_resp frames until released."""
    cluster = await build_two_node()
    try:
        host = cluster.nodes["host"]
        guest = cluster.nodes["gB"]
        cluster.hold_replies(guest, lambda frame: frame.get("type") == "rpc_resp")

        task = asyncio.create_task(
            cluster.rpc(host, "gB", "GET", "/api/echo", query={"n": "1"}, timeout=5.0)
        )
        # Wait until the reply is queued at the gate.
        for _ in range(500):
            if cluster.held_reply_count(guest) >= 1:
                break
            await asyncio.sleep(0)
        assert cluster.held_reply_count(guest) == 1
        assert not task.done()  # blocked on the gate

        cluster.release_replies(guest)
        result = await task
        assert result["body"]["n"] == "1"
    finally:
        await cluster.aclose()


async def test_pending_count_is_zero_after_reply():
    cluster = await build_two_node()
    try:
        host = cluster.nodes["host"]
        assert cluster.pending_rpc_count(host) == 0
        await cluster.rpc(host, "gB", "GET", "/api/echo", query={"n": "1"})
        assert cluster.pending_rpc_count(host) == 0
    finally:
        await cluster.aclose()


async def test_three_node_two_hop_link():
    cluster = await build_three_node()
    try:
        host = cluster.nodes["host"]
        assert host.registry is not None
        assert host.registry.get("gA") is not None
        assert host.registry.get("gB") is not None
        cluster.set_session_info(cluster.nodes["gB"], {"session_id": "z"})
        result = await cluster.rpc(cluster.nodes["gA"], "gB", "GET",
                                   "/api/session_info")
        assert result["body"]["machine"] == "gB"
        assert result["body"]["info"]["session_id"] == "z"
    finally:
        await cluster.aclose()
