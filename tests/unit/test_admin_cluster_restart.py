"""Tests for /api/admin/cluster_restart — focused on the guest-mode
forwarding path that lets the per-machine Restart button work from any
node's UI (not just the host)."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from boxagent.transports.web.server import WebHttpServer


def _make_server(tmp_path, *, guest_registry, guest_client):
    config = SimpleNamespace(
        web_token="", web_trust_header="X-Trusted",
        web_host="127.0.0.1", web_port=0, bots={}, workgroups={},
        node_id="node-a",
    )
    topology = MagicMock()
    topology.guest_registry = guest_registry
    topology.guest_client = guest_client
    topology.local_machine_id = MagicMock(return_value="node-a")
    server = WebHttpServer(
        config=config,
        local_dir=tmp_path,
        config_dir=tmp_path,
        storage=None,
        web_channels={},
        pools={},
        topology=topology,
        cluster_rpc=MagicMock(),
        cluster_routes=None,
    )
    return server


def _make_request(body):
    req = MagicMock()
    req.query = {}
    req.remote = "127.0.0.1"
    req.transport = None
    req.headers = {}
    req.json = AsyncMock(return_value=body)
    return req


@pytest.mark.asyncio
async def test_cluster_restart_in_guest_mode_forwards_to_host(tmp_path):
    """Guest mode: handler forwards POST to host's cluster_restart endpoint
    via guest_client.fetch_host_json and surfaces host's response body."""
    host_response = {
        "ok": True,
        "results": {"node-b": {"scheduled": True, "delay_seconds": 1.0}},
    }
    guest_client = MagicMock()
    guest_client.fetch_host_json = AsyncMock(return_value=host_response)
    server = _make_server(tmp_path, guest_registry=None, guest_client=guest_client)

    request = _make_request({"machines": ["node-b"], "include_self": True})
    response = await server._handle_admin_cluster_restart(request)
    assert response.status == 200
    assert json.loads(response.body) == host_response

    guest_client.fetch_host_json.assert_awaited_once()
    call_kwargs = guest_client.fetch_host_json.await_args.kwargs
    assert call_kwargs["method"] == "POST"
    assert call_kwargs["body"] == {"machines": ["node-b"], "include_self": True}
    args = guest_client.fetch_host_json.await_args.args
    assert args[0] == "/api/admin/cluster_restart"


@pytest.mark.asyncio
async def test_cluster_restart_without_host_connection_returns_503(tmp_path):
    """Guest mode but no host connection (single mode): 503, not crash."""
    server = _make_server(tmp_path, guest_registry=None, guest_client=None)
    request = _make_request({})
    response = await server._handle_admin_cluster_restart(request)
    assert response.status == 503
    body = json.loads(response.body)
    assert body["ok"] is False
    assert "no host connection" in body["error"]


@pytest.mark.asyncio
async def test_cluster_restart_in_guest_mode_surfaces_host_error(tmp_path):
    """If fetch_host_json raises, guest returns 502 with the error string."""
    guest_client = MagicMock()
    guest_client.fetch_host_json = AsyncMock(side_effect=RuntimeError("ws closed"))
    server = _make_server(tmp_path, guest_registry=None, guest_client=guest_client)

    request = _make_request({})
    response = await server._handle_admin_cluster_restart(request)
    assert response.status == 502
    body = json.loads(response.body)
    assert body["ok"] is False
    assert "ws closed" in body["error"]
