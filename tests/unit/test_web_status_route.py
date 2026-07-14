"""Tests for the GET /api/status HTTP route — local, non-bus node self-report."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from boxagent.transports.web.server import WebHttpServer


def _make_server(topology) -> WebHttpServer:
    config = SimpleNamespace(
        web_token="", web_trust_header="X-Trusted",
        web_host="127.0.0.1", web_port=0, bots={},
    )
    cluster_rpc = MagicMock()
    cluster_rpc.dispatch_machine_request = AsyncMock(return_value=None)
    return WebHttpServer(
        config=config, local_dir=None, config_dir=None, storage=None,
        web_channels={}, pools={}, topology=topology,
        cluster_rpc=cluster_rpc, cluster_routes=None,
    )


def _guest_topology(host_machine_id="devbox-xl", host_version=3):
    topology = MagicMock()
    topology.local_machine_id.return_value = "mbp"
    topology.local_role.return_value = "guest"
    topology.local_bot_descriptors.return_value = [{"name": "claude", "backend": "claude-cli"}]
    topology.guest_client = SimpleNamespace(
        host_machine_id=host_machine_id, host_version=host_version,
    )
    topology.guest_registry = None
    return topology


def _host_topology():
    topology = MagicMock()
    topology.local_machine_id.return_value = "devbox-xl"
    topology.local_role.return_value = "host"
    topology.local_bot_descriptors.return_value = [{"name": "codex", "backend": "codex-cli"}]
    topology.guest_client = None
    topology.guest_registry = SimpleNamespace(sessions={
        "mbp": SimpleNamespace(version=3),
        "macmini": SimpleNamespace(version=3),
    })
    return topology


def _request(host="127.0.0.1", headers=None):
    req = MagicMock()
    req.client = SimpleNamespace(host=host)
    req.headers = headers or {}
    req.query_params = {}
    return req


def _read(response) -> dict:
    return json.loads(response.body)


async def test_status_guest_reports_host_link():
    server = _make_server(_guest_topology())
    body = _read(await server._handle_status(_request()))
    assert body["ok"] is True
    assert body["machine_id"] == "mbp"
    assert body["role"] == "guest"
    assert "version" in body and "commit" in body
    assert isinstance(body["uptime_seconds"], int) and body["uptime_seconds"] >= 0
    assert body["bots"] == [{"name": "claude", "backend": "claude-cli"}]
    assert body["cluster"]["host_connected"] is True
    assert body["cluster"]["host_machine_id"] == "devbox-xl"
    assert body["cluster"]["host_version"] == 3
    # 关键：本地直答，绝不触发跨机 RPC
    server.cluster_rpc.dispatch_machine_request.assert_not_awaited()


async def test_status_guest_disconnected_from_host():
    # host_machine_id 断连时被清空 → host_connected False
    server = _make_server(_guest_topology(host_machine_id="", host_version=0))
    body = _read(await server._handle_status(_request()))
    assert body["cluster"]["host_connected"] is False
    assert body["cluster"]["host_version"] == 0


async def test_status_host_reports_guests():
    server = _make_server(_host_topology())
    body = _read(await server._handle_status(_request()))
    assert body["role"] == "host"
    assert body["cluster"]["guest_count"] == 2
    machine_ids = {g["machine_id"] for g in body["cluster"]["guests"]}
    assert machine_ids == {"mbp", "macmini"}


async def test_status_unauthorized_non_localhost():
    server = _make_server(_guest_topology())
    server.config.web_token = "secret"
    response = await server._handle_status(_request(host="8.8.8.8", headers={}))
    assert response.status_code == 401
