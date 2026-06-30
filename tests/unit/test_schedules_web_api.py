"""Tests for /api/schedules* HTTP routes — direct handler invocation
with mocked aiohttp Request objects (no real server)."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml


from boxagent.transports.web.server import WebHttpServer


def _make_server(tmp_path):
    config = SimpleNamespace(
        web_token="", web_trust_header="X-Trusted",
        web_host="127.0.0.1", web_port=0, bots={},
        node_id="node-a",
    )
    cluster_rpc = MagicMock()
    cluster_rpc.dispatch_machine_request = AsyncMock(return_value=None)
    server = WebHttpServer(
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
    return server


def _make_request(query=None, match_info=None):
    req = MagicMock()
    req.query = query or {}
    req.remote = "127.0.0.1"
    req.transport = None
    req.headers = {}
    req.match_info = match_info or {}
    req.path = "/api/schedules/runs"
    return req


def _seed_run_log(local_dir, task_id, records):
    p = local_dir / "schedule-runs" / f"{task_id}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _seed_schedules_yaml(config_dir, entries):
    (config_dir / "schedules.yaml").write_text(
        yaml.safe_dump(entries), encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_schedules_list_returns_yaml_entries(tmp_path):
    _seed_schedules_yaml(tmp_path, {
        "daily-summary": {"cron": "0 9 * * *", "mode": "isolate", "enabled": True, "prompt": "hi"},
    })
    server = _make_server(tmp_path)
    response = await server._handle_schedules_list(_make_request())
    body = json.loads(response.body)
    assert body["ok"] is True
    assert len(body["schedules"]) == 1
    assert body["schedules"][0]["id"] == "daily-summary"
    assert body["schedules"][0]["cron"] == "0 9 * * *"


@pytest.mark.asyncio
async def test_schedules_list_filters_by_enabled_on_nodes(tmp_path):
    _seed_schedules_yaml(tmp_path, {
        "for-node-a": {"cron": "0 9 * * *", "mode": "isolate", "prompt": "x", "enabled_on_nodes": "node-a"},
        "for-node-b": {"cron": "0 9 * * *", "mode": "isolate", "prompt": "y", "enabled_on_nodes": "node-b"},
        "for-all":    {"cron": "0 9 * * *", "mode": "isolate", "prompt": "z"},
    })
    server = _make_server(tmp_path)
    response = await server._handle_schedules_list(_make_request())
    body = json.loads(response.body)
    ids = sorted(s["id"] for s in body["schedules"])
    assert ids == ["for-all", "for-node-a"]


@pytest.mark.asyncio
async def test_schedules_runs_returns_recent_first(tmp_path):
    _seed_run_log(tmp_path, "task-x", [
        {"time": "2026-05-13T10:00:00", "task": "task-x", "output": "ok"},
        {"time": "2026-05-13T11:00:00", "task": "task-x", "output": "ok2", "session_id": "sid-2"},
    ])
    server = _make_server(tmp_path)
    response = await server._handle_schedules_runs(_make_request(query={"task": "task-x"}))
    body = json.loads(response.body)
    assert body["ok"] is True
    assert len(body["runs"]) == 2
    assert body["runs"][0]["time"] == "2026-05-13T11:00:00"
    assert body["runs"][0]["session_id"] == "sid-2"


@pytest.mark.asyncio
async def test_schedules_run_detail_by_index(tmp_path):
    _seed_run_log(tmp_path, "task-x", [
        {"time": "2026-05-13T10:00:00", "task": "task-x", "output": "older"},
        {"time": "2026-05-13T11:00:00", "task": "task-x", "output": "newer", "session_id": "sid-2"},
    ])
    server = _make_server(tmp_path)
    request = _make_request(match_info={"task_id": "task-x", "run_index": "1"})
    response = await server._handle_schedules_run_detail(request)
    body = json.loads(response.body)
    assert body["ok"] is True
    assert body["run"]["output"] == "newer"


@pytest.mark.asyncio
async def test_schedules_run_detail_404_when_missing(tmp_path):
    server = _make_server(tmp_path)
    request = _make_request(match_info={"task_id": "ghost", "run_index": "1"})
    response = await server._handle_schedules_run_detail(request)
    assert response.status == 404


@pytest.mark.asyncio
async def test_schedules_runs_dispatches_when_machine_remote(tmp_path):
    server = _make_server(tmp_path)
    remote_response = MagicMock()
    server.cluster_rpc.dispatch_machine_request = AsyncMock(return_value=remote_response)
    request = _make_request(query={"task": "task-x", "machine": "node-b"})
    response = await server._handle_schedules_runs(request)
    assert response is remote_response
    server.cluster_rpc.dispatch_machine_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_transcript_endpoint_routes_by_backend(tmp_path, monkeypatch):
    """?backend=codex-cli must call get_history('codex-cli'), not the default."""
    server = _make_server(tmp_path)
    server.topology.local_machine_id = MagicMock(return_value="node-a")

    captured = {}

    class FakeHistory:
        async def read_messages(self, sid, project_id):
            captured["sid"] = sid
            captured["project_id"] = project_id
            return []

    def fake_get_history(kind):
        captured["kind"] = kind
        return FakeHistory()

    monkeypatch.setattr("boxagent.history.get_history", fake_get_history)
    request = _make_request(query={
        "backend": "codex-cli", "project": "/tmp/wp",
        "session_id": "sid-x", "machine": "node-a",
    })
    request.path = "/api/claude/transcript"
    response = await server._handle_claude_transcript(request)
    body = json.loads(response.body)
    assert body["ok"] is True
    assert captured["kind"] == "codex-cli"
    assert captured["project_id"] == "/tmp/wp"
    assert captured["sid"] == "sid-x"


@pytest.mark.asyncio
async def test_session_id_appears_in_run_log(tmp_path):
    """End-to-end: scheduler isolate run records backend.session_id into jsonl."""
    from boxagent.scheduler.engine import Scheduler, ScheduleTask

    class FakeBackend:
        def __init__(self, *_args, **_kwargs):
            self.session_id = "captured-sid"
        def start(self): pass
        async def stop(self): pass
        async def send(self, message, callback, **kwargs):
            await callback.on_stream("output text")

    import boxagent.scheduler.engine as engine_mod
    monkey_target = engine_mod
    # Inject FakeBackend into the import path used by _spawn_isolate.
    import boxagent.agent.sdk_claude_process as sdk_claude
    original = sdk_claude.AgentSDKClaude
    sdk_claude.AgentSDKClaude = FakeBackend
    try:
        scheduler = Scheduler(
            schedules_file=tmp_path / "schedules.yaml",
            node_id="node-a",
            local_dir=str(tmp_path),
            default_workspace=str(tmp_path),
        )
        task = ScheduleTask(
            id="t1", cron="* * * * *", prompt="run me",
            mode="isolate", ai_backend="claude-cli", model="sonnet",
            timeout_seconds=10.0,
        )
        await scheduler._fire(task)
    finally:
        sdk_claude.AgentSDKClaude = original

    log = (tmp_path / "schedule-runs" / "t1.jsonl").read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(log[-1])
    assert record["session_id"] == "captured-sid"
