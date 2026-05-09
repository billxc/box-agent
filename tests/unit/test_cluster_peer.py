"""Unit tests for cross-admin peer messaging via cluster RPC (yait #8)."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from boxagent.cluster.peer import PeerService
from boxagent.cluster.topology import TopologyService


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_peer(*router_names: str, host_election=None) -> PeerService:
    """Build a PeerService with a workgroup_mgr that owns named routers."""
    cfg = MagicMock()
    cfg.machine_id = ""
    cfg.node_id = ""
    cfg.cluster_tunnel = False
    topo = TopologyService(config=cfg, web_channels={})
    if host_election is not None:
        topo.set_host_election(host_election)
    ps = PeerService(
        topology=topo,
        main_chat_id_provider=lambda b: f"main-{b}-{int(time.time())}",
    )
    if router_names:
        ps.set_workgroup_mgr(SimpleNamespace(
            routers={n: AsyncMock(handle_message=AsyncMock()) for n in router_names},
        ))
    return ps


class _FakeRequest:
    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return self._body


def _post(handler, body: dict):
    return asyncio.run(handler(_FakeRequest(body)))


# ─── _dispatch_local_peer ────────────────────────────────────────────────────

def test_dispatch_local_peer_envelopes_raw_body():
    ps = _make_peer("admin-a")
    asyncio.run(ps._dispatch_local_peer("admin-a", "admin-b", "hello"))
    handler = ps.workgroup_mgr.routers["admin-a"].handle_message
    handler.assert_awaited_once()
    msg = handler.await_args.args[0]
    # Peer message routes to the bot's main chat_id (provider hook). Lands in
    # the admin's main session — same one heartbeat dispatches into.
    assert msg.chat_id.startswith("main-admin-a")
    assert msg.user_id == "admin-b"
    assert msg.text.startswith("[Peer message from admin-b]\nhello")
    assert 'send_to_peer("admin-b"' in msg.text
    assert msg.trusted is True


def test_dispatch_local_peer_no_double_envelope_when_body_already_wrapped():
    ps = _make_peer("admin-a")
    asyncio.run(ps._dispatch_local_peer("admin-a", "admin-b", "raw text"))
    text = ps.workgroup_mgr.routers["admin-a"].handle_message.await_args.args[0].text
    assert text.count("[Peer message from admin-b]") == 1


# ─── /api/wg/peer/recv ───────────────────────────────────────────────────────

def test_peer_recv_dispatches_to_local_admin():
    ps = _make_peer("admin-a")
    resp = _post(ps.handle_wg_peer_recv, {
        "target_workgroup": "admin-a", "sender": "admin-b", "body": "hi",
    })
    assert resp.status == 200
    ps.workgroup_mgr.routers["admin-a"].handle_message.assert_awaited_once()


def test_peer_recv_404_for_unknown_workgroup():
    ps = _make_peer("admin-a")
    resp = _post(ps.handle_wg_peer_recv, {
        "target_workgroup": "ghost", "sender": "admin-b", "body": "hi",
    })
    assert resp.status == 404


def test_peer_recv_400_on_missing_fields():
    ps = _make_peer("admin-a")
    resp = _post(ps.handle_wg_peer_recv, {"sender": "x", "body": "y"})
    assert resp.status == 400


# ─── /api/peer/send ──────────────────────────────────────────────────────────

def test_peer_send_local_admin_dispatches_in_process():
    ps = _make_peer("admin-a")
    resp = _post(ps.handle_peer_send, {
        "target": "admin-a", "from": "admin-b", "message": "hi there",
    })
    assert resp.status == 200
    ps.workgroup_mgr.routers["admin-a"].handle_message.assert_awaited_once()


def _he_with_bots(bots: list[tuple[str, object]], sess):
    he = SimpleNamespace(registry=MagicMock(), client=None, tunnel=None)
    he.registry.list_bots = MagicMock(return_value=bots)
    he.registry.get = MagicMock(return_value=sess)
    return he


def test_peer_send_remote_admin_routes_via_cluster_rpc():
    sess = MagicMock()
    sess.call = AsyncMock(return_value={"status": 200, "body": {"ok": True}})
    bot = SimpleNamespace(name="admin-b", kind="workgroup")
    ps = _make_peer(host_election=_he_with_bots([("sat1", bot)], sess))

    resp = _post(ps.handle_peer_send, {
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
    sess = MagicMock()
    sess.call = AsyncMock()
    bot = SimpleNamespace(name="admin-b", kind="bot")
    ps = _make_peer(host_election=_he_with_bots([("sat1", bot)], sess))

    resp = _post(ps.handle_peer_send, {
        "target": "admin-b", "from": "admin-a", "message": "hi",
    })
    assert resp.status == 404
    sess.call.assert_not_awaited()


def test_peer_send_404_when_target_nowhere():
    ps = _make_peer()
    resp = _post(ps.handle_peer_send, {
        "target": "ghost", "from": "admin-a", "message": "hi",
    })
    assert resp.status == 404


def test_peer_send_400_on_missing_fields():
    ps = _make_peer("admin-a")
    resp = _post(ps.handle_peer_send, {"target": "admin-a", "from": ""})
    assert resp.status == 400


def test_peer_send_502_on_rpc_failure():
    sess = MagicMock()
    sess.call = AsyncMock(side_effect=RuntimeError("ws closed"))
    bot = SimpleNamespace(name="admin-b", kind="workgroup")
    ps = _make_peer(host_election=_he_with_bots([("sat1", bot)], sess))

    resp = _post(ps.handle_peer_send, {
        "target": "admin-b", "from": "admin-a", "message": "hi",
    })
    assert resp.status == 502


def test_peer_send_local_takes_priority_over_remote_with_same_name():
    """If a workgroup with the target name exists locally AND in the cluster,
    local wins (no RPC needed, faster + less ambiguous)."""
    sess = MagicMock()
    sess.call = AsyncMock()
    bot = SimpleNamespace(name="admin-a", kind="workgroup")
    ps = _make_peer("admin-a", host_election=_he_with_bots([("sat1", bot)], sess))

    resp = _post(ps.handle_peer_send, {
        "target": "admin-a", "from": "admin-b", "message": "hi",
    })
    assert resp.status == 200
    sess.call.assert_not_awaited()
    ps.workgroup_mgr.routers["admin-a"].handle_message.assert_awaited_once()
