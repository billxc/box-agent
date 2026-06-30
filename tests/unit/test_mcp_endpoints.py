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


def test_telegram_endpoint_added_when_token_set(tmp_path):
    env = _env(tmp_path, telegram_token="t-token")
    names = [e["name"] for e in pick_mcp_endpoints(env, "chat-1")]
    assert "boxagent-telegram" in names


# ── CodexProcess._mcp_args ──


def test_codex_mcp_args_empty_without_env():
    backend = CodexProcess(workspace="/tmp")
    assert backend._mcp_args("chat-1") == []


def test_codex_mcp_args_empty_when_no_endpoints(tmp_path):
    backend = CodexProcess(workspace="/tmp")
    env = _env(tmp_path, passthrough=True)
    assert backend._mcp_args("chat-1", env=env) == []


def test_codex_mcp_args_emits_url_and_headers(tmp_path):
    backend = CodexProcess(workspace="/tmp")
    env = _env(tmp_path)  # base only
    args = backend._mcp_args("chat-1", env=env)
    # 2 -c entries per endpoint (url + http_headers)
    assert args.count("-c") == 2
    joined = " ".join(args)
    assert 'mcp_servers.boxagent.url="http://127.0.0.1:9390/mcp/base"' in joined
    assert "mcp_servers.boxagent.http_headers={" in joined
    assert '"X-BoxAgent-Bot-Name" = "bot-1"' in joined
    assert '"X-BoxAgent-Chat-Id" = "chat-1"' in joined


def test_codex_mcp_args_telegram_emits_two_servers(tmp_path):
    backend = CodexProcess(workspace="/tmp")
    env = _env(tmp_path, telegram_token="t")
    args = backend._mcp_args("chat-1", env=env)
    joined = " ".join(args)
    assert "mcp_servers.boxagent.url=" in joined
    assert "mcp_servers.boxagent-telegram.url=" in joined
    assert args.count("-c") == 4  # 2 endpoints × (url + headers)
