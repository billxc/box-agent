"""Tests for boxagent.agent.mcp_endpoints + per-backend wiring.

Covers pick_mcp_endpoints (the shared helper) and the Codex-specific
``-c mcp_servers.X.url=...`` formatting in ``CodexProcess._mcp_args``.
Claude's ``--mcp-config`` JSON path is exercised by the broader
test_claude_process suite.
"""

from __future__ import annotations

from boxagent.agent.codex_process import CodexProcess
from boxagent.agent.mcp_endpoints import pick_mcp_endpoints
from boxagent.agent_env import AgentEnv


def _env(tmp_path, **overrides):
    """Build an AgentEnv with mcp-port.txt staged in tmp_path."""
    (tmp_path / "mcp-port.txt").write_text("9390\n")
    base = dict(
        bot_name="bot-1",
        local_dir=str(tmp_path),
    )
    base.update(overrides)
    return AgentEnv(**base)


# ── pick_mcp_endpoints ──


def test_no_endpoints_when_passthrough(tmp_path):
    env = _env(tmp_path, passthrough=True)
    assert pick_mcp_endpoints(env, "chat-1") == []


def test_no_endpoints_when_chat_id_empty(tmp_path):
    env = _env(tmp_path)
    assert pick_mcp_endpoints(env, "") == []


def test_no_endpoints_when_port_file_missing(tmp_path):
    # No mcp-port.txt staged — pick_mcp_endpoints should bail.
    env = AgentEnv(bot_name="bot-1", local_dir=str(tmp_path))
    assert pick_mcp_endpoints(env, "chat-1") == []


def test_base_endpoint_always_present(tmp_path):
    env = _env(tmp_path)
    endpoints = pick_mcp_endpoints(env, "chat-1")
    assert [e["name"] for e in endpoints] == ["boxagent"]
    assert endpoints[0]["url"] == "http://127.0.0.1:9390/mcp/base"
    assert endpoints[0]["headers"] == {
        "X-BoxAgent-Bot-Name": "bot-1",
        "X-BoxAgent-Chat-Id": "chat-1",
    }


def test_admin_endpoint_added_for_workgroup_admin(tmp_path):
    env = _env(tmp_path, workgroup_role="admin")
    names = [e["name"] for e in pick_mcp_endpoints(env, "chat-1")]
    assert "boxagent-admin" in names


def test_telegram_endpoint_added_when_token_set(tmp_path):
    env = _env(tmp_path, telegram_token="t-token")
    names = [e["name"] for e in pick_mcp_endpoints(env, "chat-1")]
    assert "boxagent-telegram" in names


def test_peer_endpoint_added_when_has_peer_channel(tmp_path):
    env = _env(tmp_path, has_peer_channel=True)
    names = [e["name"] for e in pick_mcp_endpoints(env, "chat-1")]
    assert "boxagent-peer" in names


def test_admin_with_peer_gets_both(tmp_path):
    env = _env(tmp_path, workgroup_role="admin", has_peer_channel=True)
    names = [e["name"] for e in pick_mcp_endpoints(env, "chat-1")]
    assert {"boxagent", "boxagent-admin", "boxagent-peer"}.issubset(names)


# ── CodexProcess._mcp_args ──


def test_codex_mcp_args_empty_without_env():
    proc = CodexProcess(workspace="/tmp")
    assert proc._mcp_args("chat-1") == []


def test_codex_mcp_args_empty_when_no_endpoints(tmp_path):
    proc = CodexProcess(workspace="/tmp")
    env = _env(tmp_path, passthrough=True)
    assert proc._mcp_args("chat-1", env=env) == []


def test_codex_mcp_args_emits_url_and_headers(tmp_path):
    proc = CodexProcess(workspace="/tmp")
    env = _env(tmp_path)  # base only
    args = proc._mcp_args("chat-1", env=env)
    # 2 -c entries per endpoint (url + http_headers)
    assert args.count("-c") == 2
    joined = " ".join(args)
    assert 'mcp_servers.boxagent.url="http://127.0.0.1:9390/mcp/base"' in joined
    assert "mcp_servers.boxagent.http_headers={" in joined
    assert '"X-BoxAgent-Bot-Name" = "bot-1"' in joined
    assert '"X-BoxAgent-Chat-Id" = "chat-1"' in joined


def test_codex_mcp_args_admin_emits_two_servers(tmp_path):
    proc = CodexProcess(workspace="/tmp")
    env = _env(tmp_path, workgroup_role="admin")
    args = proc._mcp_args("chat-1", env=env)
    joined = " ".join(args)
    assert "mcp_servers.boxagent.url=" in joined
    assert "mcp_servers.boxagent-admin.url=" in joined
    assert args.count("-c") == 4  # 2 endpoints × (url + headers)


def test_codex_mcp_args_full_admin(tmp_path):
    proc = CodexProcess(workspace="/tmp")
    env = _env(
        tmp_path, workgroup_role="admin",
        telegram_token="t", has_peer_channel=True,
    )
    args = proc._mcp_args("chat-1", env=env)
    joined = " ".join(args)
    for name in ("boxagent", "boxagent-admin", "boxagent-telegram", "boxagent-peer"):
        assert f"mcp_servers.{name}.url=" in joined
    assert args.count("-c") == 8  # 4 endpoints × 2
