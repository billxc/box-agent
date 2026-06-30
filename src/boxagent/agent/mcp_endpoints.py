"""Resolve which boxagent MCP HTTP endpoints to attach to a backend turn.

Both ``sdk_claude_process`` and ``codex_process`` need the same answer for
"given this env + chat_id, which boxagent-* MCP servers should the LLM see?"
— previously duplicated inline. This module is the single source of truth.

Each entry has ``name`` (the MCP server's identifier as the LLM sees it,
e.g. ``boxagent-admin`` → tool prefix ``mcp__boxagent-admin__...``),
``url`` (full streamable-http endpoint), and ``headers`` (per-request
context the MCP middleware reads back as ContextVars).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from boxagent.agent_env import AgentEnv


class McpEndpoint(TypedDict):
    name: str
    url: str
    headers: dict[str, str]


def pick_mcp_endpoints(env: "AgentEnv", chat_id: str) -> list[McpEndpoint]:
    """Return the MCP endpoints to attach for this turn.

    Empty list iff MCP wiring is disabled (passthrough bot, no chat_id, or
    the gateway-managed ``mcp-port.txt`` is missing — meaning the MCP HTTP
    server didn't come up on this process).

    The ``base`` endpoint is unconditional; ``admin`` / ``telegram`` /
    ``peer`` are gated on the corresponding env capability.
    """
    if env.passthrough or not chat_id or not env.local_dir:
        return []
    port_file = Path(env.local_dir) / "mcp-port.txt"
    if not port_file.exists():
        return []
    try:
        port = port_file.read_text().strip()
    except OSError:
        return []
    if not port:
        return []

    base_url = f"http://127.0.0.1:{port}"
    headers = {
        "X-BoxAgent-Bot-Name": env.bot_name,
        "X-BoxAgent-Chat-Id": chat_id,
    }

    out: list[McpEndpoint] = [
        {"name": "boxagent", "url": f"{base_url}/mcp/base", "headers": headers},
    ]
    if env.has_telegram:
        out.append({"name": "boxagent-telegram", "url": f"{base_url}/mcp/telegram", "headers": headers})
    return out
