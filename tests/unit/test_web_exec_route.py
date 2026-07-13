"""Tests for the POST /api/exec HTTP route — direct handler invocation."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from boxagent.transports.web.server import WebHttpServer


def _make_server(tmp_path, local_machine="local") -> WebHttpServer:
    config = SimpleNamespace(
        web_token="", web_trust_header="X-Trusted",
        web_host="127.0.0.1", web_port=0, bots={},
    )
    cluster_rpc = MagicMock()
    cluster_rpc.dispatch_machine_request = AsyncMock(return_value=None)
    topology = MagicMock()
    topology.local_machine_id.return_value = local_machine
    return WebHttpServer(
        config=config,
        local_dir=tmp_path,
        config_dir=tmp_path,
        storage=None,
        web_channels={},
        pools={},
        topology=topology,
        cluster_rpc=cluster_rpc,
        cluster_routes=None,
    )


def _make_request(body: dict, host: str = "127.0.0.1", headers: dict | None = None):
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.client = SimpleNamespace(host=host)
    req.headers = headers or {}
    req.query_params = {}
    req.path_params = {}
    return req


def _read(response) -> dict:
    return json.loads(response.body)


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell commands")


async def test_exec_local_runs_and_returns_output(tmp_path):
    server = _make_server(tmp_path, local_machine="local")
    request = _make_request({"machine": "local", "command": "echo hi", "workspace": str(tmp_path)})
    response = await server._handle_web_exec(request)
    body = _read(response)
    assert body["ok"] is True
    assert body["machine"] == "local"
    assert body["exit_code"] == 0
    assert body["output"] == "hi"
    server.cluster_rpc.dispatch_machine_request.assert_not_awaited()


async def test_exec_missing_command_returns_400(tmp_path):
    server = _make_server(tmp_path)
    response = await server._handle_web_exec(_make_request({"machine": "local"}))
    assert response.status_code == 400
    assert _read(response)["ok"] is False


async def test_exec_nonzero_exit_code(tmp_path):
    server = _make_server(tmp_path, local_machine="local")
    request = _make_request({"machine": "local", "command": "exit 7", "workspace": str(tmp_path)})
    body = _read(await server._handle_web_exec(request))
    assert body["ok"] is True
    assert body["exit_code"] == 7


async def test_exec_forwards_to_remote_machine(tmp_path):
    server = _make_server(tmp_path, local_machine="local")
    forwarded = MagicMock()
    forwarded.body = b'{"ok": true, "machine": "remote-1", "exit_code": 0, "output": "from-remote"}'
    server.cluster_rpc.dispatch_machine_request = AsyncMock(return_value=forwarded)

    request = _make_request({"machine": "remote-1", "command": "echo hi"})
    response = await server._handle_web_exec(request)

    server.cluster_rpc.dispatch_machine_request.assert_awaited_once()
    args = server.cluster_rpc.dispatch_machine_request.await_args
    assert args.args[0] == "remote-1"
    assert args.args[1] == "POST"
    assert args.args[2] == "/api/exec"
    assert response is forwarded


async def test_exec_unauthorized_non_localhost(tmp_path):
    server = _make_server(tmp_path)
    server.config.web_token = "secret"
    request = _make_request(
        {"machine": "local", "command": "echo hi"}, host="8.8.8.8", headers={},
    )
    response = await server._handle_web_exec(request)
    assert response.status_code == 401
