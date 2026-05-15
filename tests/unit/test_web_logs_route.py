"""Tests for the /api/logs HTTP route — direct handler invocation."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from boxagent.transports.web.server import WebHttpServer


def _make_server(tmp_path, log_file=None) -> WebHttpServer:
    config = SimpleNamespace(
        web_token="", web_trust_header="X-Trusted",
        web_host="127.0.0.1", web_port=0, bots={}, workgroups={},
        log_file=log_file,
    )
    cluster_rpc = MagicMock()
    cluster_rpc.dispatch_machine_request = AsyncMock(return_value=None)
    return WebHttpServer(
        config=config,
        local_dir=tmp_path,
        config_dir=tmp_path,
        storage=None,
        web_channels={},
        pools={},
        topology=MagicMock(),
        cluster_rpc=cluster_rpc,
        cluster_routes=None,
    )


def _make_request(query: dict | None = None):
    req = MagicMock()
    req.query = query or {}
    req.remote = "127.0.0.1"
    req.transport = None
    req.headers = {}
    req.match_info = {}
    req.body_exists = False
    return req


def _read_response(response) -> dict:
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_logs_query_no_log_file_returns_empty(tmp_path):
    server = _make_server(tmp_path, log_file=None)
    response = await server._handle_logs_query(_make_request())
    body = _read_response(response)
    assert body["ok"] is True
    assert body["lines"] == []
    assert body["log_file"] is None


@pytest.mark.asyncio
async def test_logs_query_local_reads_file(tmp_path):
    log = tmp_path / "boxagent.log"
    entries = [
        {"time": "t0", "level": "INFO", "logger": "x", "msg": "alpha"},
        {"time": "t1", "level": "ERROR", "logger": "x", "msg": "boom"},
    ]
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    server = _make_server(tmp_path, log_file=log)
    response = await server._handle_logs_query(_make_request())
    body = _read_response(response)
    assert body["ok"] is True
    assert body["log_file"] == str(log)
    assert [line["msg"] for line in body["lines"]] == ["boom", "alpha"]


@pytest.mark.asyncio
async def test_logs_query_filters_by_level(tmp_path):
    log = tmp_path / "boxagent.log"
    entries = [
        {"time": "t0", "level": "INFO", "msg": "a"},
        {"time": "t1", "level": "ERROR", "msg": "b"},
    ]
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    server = _make_server(tmp_path, log_file=log)
    response = await server._handle_logs_query(_make_request({"levels": "error"}))
    body = _read_response(response)
    assert [line["msg"] for line in body["lines"]] == ["b"]


@pytest.mark.asyncio
async def test_logs_query_pagination(tmp_path):
    log = tmp_path / "boxagent.log"
    entries = [{"time": f"t{i}", "level": "INFO", "msg": f"m{i}"} for i in range(10)]
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    server = _make_server(tmp_path, log_file=log)
    response = await server._handle_logs_query(_make_request({"limit": "3", "offset": "2"}))
    body = _read_response(response)
    assert [line["msg"] for line in body["lines"]] == ["m7", "m6", "m5"]
    assert body["has_more"] is True


@pytest.mark.asyncio
async def test_logs_query_forwards_to_remote_machine(tmp_path):
    log = tmp_path / "boxagent.log"
    log.write_text(json.dumps({"time": "t", "level": "INFO", "msg": "local"}) + "\n", encoding="utf-8")
    server = _make_server(tmp_path, log_file=log)

    forwarded = MagicMock()
    forwarded.body = b'{"ok": true, "lines": [{"msg": "from-remote"}], "has_more": false, "log_file": "/remote.log"}'
    server.cluster_rpc.dispatch_machine_request = AsyncMock(return_value=forwarded)

    response = await server._handle_logs_query(_make_request({"machine": "remote-1"}))
    server.cluster_rpc.dispatch_machine_request.assert_awaited_once()
    args = server.cluster_rpc.dispatch_machine_request.await_args
    assert args.args[0] == "remote-1"
    assert args.args[1] == "GET"
    assert args.args[2] == "/api/logs"
    assert response is forwarded


@pytest.mark.asyncio
async def test_logs_query_local_when_no_machine(tmp_path):
    log = tmp_path / "boxagent.log"
    log.write_text(json.dumps({"time": "t", "level": "INFO", "msg": "local"}) + "\n", encoding="utf-8")
    server = _make_server(tmp_path, log_file=log)
    server.cluster_rpc.dispatch_machine_request = AsyncMock(return_value=None)

    response = await server._handle_logs_query(_make_request())
    body = _read_response(response)
    assert body["lines"][0]["msg"] == "local"
    server.cluster_rpc.dispatch_machine_request.assert_not_awaited()
