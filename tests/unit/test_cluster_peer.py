"""Unit tests for cross-admin peer messaging via cluster RPC (yait #8)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from boxagent.gateway import Gateway, _parse_peer_message


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_gateway_with_routers(*router_names: str) -> Gateway:
    """Build a Gateway shell with a workgroup_mgr that owns named routers."""
    gw = Gateway.__new__(Gateway)  # bypass __init__
    gw._workgroup_mgr = SimpleNamespace(
        routers={name: AsyncMock(handle_message=AsyncMock()) for name in router_names},
    )
    gw._sat_registry = None
    return gw


class _FakeRequest:
    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return self._body


def _post(gw: Gateway, handler_name: str, body: dict):
    req = _FakeRequest(body)
    return asyncio.run(getattr(gw, handler_name)(req))


# ─── _dispatch_local_peer ────────────────────────────────────────────────────

def test_dispatch_local_peer_envelopes_raw_body():
    gw = _make_gateway_with_routers("admin-a")
    asyncio.run(gw._dispatch_local_peer("admin-a", "admin-b", "hello"))
    handler = gw._workgroup_mgr.routers["admin-a"].handle_message
    handler.assert_awaited_once()
    msg = handler.await_args.args[0]
    # Peer message routes to the bot's main chat_id (persisted via Storage,
    # or auto-minted as `main-<bot>-<ts>` when storage is absent — as in
    # this test). It lands in the admin's main session — same one heartbeat
    # dispatches into. NOT a `peer:<sender>` chat.
    assert msg.chat_id.startswith("main-admin-a")
    assert msg.user_id == "admin-b"
    assert msg.text.startswith("[Peer message from admin-b]\nhello")
    assert 'send_to_peer("admin-b"' in msg.text
    assert msg.trusted is True


def test_dispatch_local_peer_no_double_envelope_when_body_already_wrapped():
    """Body comes in raw (per RPC contract). Even if caller mistakenly
    sends an already-enveloped string, _dispatch_local_peer just wraps once
    more — that's caller's bug, not ours. We test the contract: the
    receiver wraps exactly the body it gets."""
    gw = _make_gateway_with_routers("admin-a")
    asyncio.run(gw._dispatch_local_peer("admin-a", "admin-b", "raw text"))
    text = gw._workgroup_mgr.routers["admin-a"].handle_message.await_args.args[0].text
    # Envelope appears exactly once.
    assert text.count("[Peer message from admin-b]") == 1


# ─── /api/wg/peer/recv ───────────────────────────────────────────────────────

def test_peer_recv_dispatches_to_local_admin():
    gw = _make_gateway_with_routers("admin-a")
    resp = _post(gw, "_handle_wg_peer_recv", {
        "target_workgroup": "admin-a", "sender": "admin-b", "body": "hi",
    })
    assert resp.status == 200
    gw._workgroup_mgr.routers["admin-a"].handle_message.assert_awaited_once()


def test_peer_recv_404_for_unknown_workgroup():
    gw = _make_gateway_with_routers("admin-a")
    resp = _post(gw, "_handle_wg_peer_recv", {
        "target_workgroup": "ghost", "sender": "admin-b", "body": "hi",
    })
    assert resp.status == 404


def test_peer_recv_400_on_missing_fields():
    gw = _make_gateway_with_routers("admin-a")
    resp = _post(gw, "_handle_wg_peer_recv", {"sender": "x", "body": "y"})
    assert resp.status == 400


# ─── /api/peer/send ──────────────────────────────────────────────────────────

def test_peer_send_local_admin_dispatches_in_process():
    gw = _make_gateway_with_routers("admin-a")
    resp = _post(gw, "_handle_peer_send", {
        "target": "admin-a", "from": "admin-b", "message": "hi there",
    })
    assert resp.status == 200
    gw._workgroup_mgr.routers["admin-a"].handle_message.assert_awaited_once()


def test_peer_send_remote_admin_routes_via_cluster_rpc():
    gw = _make_gateway_with_routers()  # nothing local
    sess = MagicMock()
    sess.call = AsyncMock(return_value={"status": 200, "body": {"ok": True}})
    bot = SimpleNamespace(name="admin-b", kind="workgroup")
    gw._sat_registry = MagicMock()
    gw._sat_registry.list_bots = MagicMock(return_value=[("sat1", bot)])
    gw._sat_registry.get = MagicMock(return_value=sess)

    resp = _post(gw, "_handle_peer_send", {
        "target": "admin-b", "from": "admin-a", "message": "hi",
    })
    assert resp.status == 200
    sess.call.assert_awaited_once()
    args, kwargs = sess.call.call_args
    assert args[:2] == ("POST", "/api/wg/peer/recv")
    assert kwargs["body"] == {
        "target_workgroup": "admin-b", "sender": "admin-a", "body": "hi",
    }


def test_peer_send_remote_skips_non_workgroup_kinds():
    """A regular bot named the same as the target should NOT be picked —
    only workgroup-kind remote bots can receive peer messages."""
    gw = _make_gateway_with_routers()
    sess = MagicMock()
    sess.call = AsyncMock()
    bot = SimpleNamespace(name="admin-b", kind="bot")  # wrong kind
    gw._sat_registry = MagicMock()
    gw._sat_registry.list_bots = MagicMock(return_value=[("sat1", bot)])
    gw._sat_registry.get = MagicMock(return_value=sess)

    resp = _post(gw, "_handle_peer_send", {
        "target": "admin-b", "from": "admin-a", "message": "hi",
    })
    assert resp.status == 404
    sess.call.assert_not_awaited()


def test_peer_send_404_when_target_nowhere():
    gw = _make_gateway_with_routers()
    resp = _post(gw, "_handle_peer_send", {
        "target": "ghost", "from": "admin-a", "message": "hi",
    })
    assert resp.status == 404


def test_peer_send_400_on_missing_fields():
    gw = _make_gateway_with_routers("admin-a")
    resp = _post(gw, "_handle_peer_send", {"target": "admin-a", "from": ""})
    assert resp.status == 400


def test_peer_send_502_on_rpc_failure():
    gw = _make_gateway_with_routers()
    sess = MagicMock()
    sess.call = AsyncMock(side_effect=RuntimeError("ws closed"))
    bot = SimpleNamespace(name="admin-b", kind="workgroup")
    gw._sat_registry = MagicMock()
    gw._sat_registry.list_bots = MagicMock(return_value=[("sat1", bot)])
    gw._sat_registry.get = MagicMock(return_value=sess)

    resp = _post(gw, "_handle_peer_send", {
        "target": "admin-b", "from": "admin-a", "message": "hi",
    })
    assert resp.status == 502


def test_peer_send_local_takes_priority_over_remote_with_same_name():
    """If a workgroup with the target name exists locally AND in the cluster,
    local wins (no RPC needed, faster + less ambiguous)."""
    gw = _make_gateway_with_routers("admin-a")
    sess = MagicMock()
    sess.call = AsyncMock()
    bot = SimpleNamespace(name="admin-a", kind="workgroup")
    gw._sat_registry = MagicMock()
    gw._sat_registry.list_bots = MagicMock(return_value=[("sat1", bot)])
    gw._sat_registry.get = MagicMock(return_value=sess)

    resp = _post(gw, "_handle_peer_send", {
        "target": "admin-a", "from": "admin-b", "message": "hi",
    })
    assert resp.status == 200
    sess.call.assert_not_awaited()  # remote not consulted
    gw._workgroup_mgr.routers["admin-a"].handle_message.assert_awaited_once()
