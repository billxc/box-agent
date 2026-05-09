"""Tests for ClusterRpc — composition replacement for ClusterRpcMixin."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from boxagent.cluster.cluster_rpc import ClusterRpc


class _Req:
    def __init__(self, query=None):
        self.query = query or {}


def _make(*, local_mid="local", guest_registry=None, guest_client=None):
    topo = MagicMock()
    topo.local_machine_id = MagicMock(return_value=local_mid)
    topo.guest_registry = guest_registry
    topo.guest_client = guest_client
    return ClusterRpc(topology=topo)


class TestClusterRpcConstruction:
    def test_init_takes_topology(self):
        topo = MagicMock()
        rpc = ClusterRpc(topology=topo)
        assert rpc.topology is topo


class TestDispatchMachineRequest:
    @pytest.mark.asyncio
    async def test_local_returns_none(self):
        rpc = _make(local_mid="me")
        result = await rpc.dispatch_machine_request(
            "me", "GET", "/api/x", _Req(),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_remote_unknown_machine_404(self):
        reg = MagicMock()
        reg.get = MagicMock(return_value=None)
        rpc = _make(local_mid="me", guest_registry=reg)
        resp = await rpc.dispatch_machine_request(
            "guest-x", "GET", "/api/x", _Req(),
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_no_routing_returns_503(self):
        rpc = _make(local_mid="me")  # no registry, no client
        resp = await rpc.dispatch_machine_request(
            "guest-x", "GET", "/api/x", _Req(),
        )
        assert resp.status == 503
