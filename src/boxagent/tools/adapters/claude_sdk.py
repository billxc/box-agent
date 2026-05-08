"""Adapter: BoxAgent registry → claude-agent-sdk in-process MCP server.

Returns a dict suitable for ``ClaudeAgentOptions.mcp_servers``. Each
distinct ``ToolDef.group`` becomes one in-process MCP server (e.g.
``boxagent``, ``boxagent-telegram``, ``boxagent-admin``, ``boxagent-peer``)
matching the URL naming used by the HTTP MCP server, so the agent sees
identical tool namespaces regardless of backend.

Tool handlers run in this process, no HTTP / IPC. Per-call context
(bot_name / chat_id / gateway) is closure-captured at adapter-build
time — adapters are rebuilt per turn so context is fresh.
"""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool as sdk_tool

from boxagent.tools import ToolContext, ToolDef, env_capabilities, tools_for

logger = logging.getLogger(__name__)


GROUP_TO_SERVER_NAME = {
    "base":     "boxagent",
    "telegram": "boxagent-telegram",
    "admin":    "boxagent-admin",
    "peer":     "boxagent-peer",
}


def build_mcp_servers(*, ctx: ToolContext, env: Any) -> dict[str, Any]:
    """Build a dict for ``ClaudeAgentOptions.mcp_servers``.

    Filters tools by env capabilities (``has_telegram`` / ``is_workgroup_admin``
    / ``has_peer_channel``) and groups them into one in-process MCP server
    per ``ToolDef.group``.
    """
    caps = env_capabilities(env)
    visible = tools_for(env_caps=caps)
    if not visible:
        return {}

    by_group: dict[str, list[ToolDef]] = {}
    for t in visible:
        by_group.setdefault(t.group, []).append(t)

    servers: dict[str, Any] = {}
    for group, defs in by_group.items():
        server_name = GROUP_TO_SERVER_NAME.get(group, f"boxagent-{group}")
        sdk_tools = [_convert(d, ctx) for d in defs]
        servers[server_name] = create_sdk_mcp_server(name=server_name, tools=sdk_tools)
    return servers


def _convert(tool_def: ToolDef, ctx: ToolContext):
    """Wrap one ToolDef into an SdkMcpTool the SDK can register.

    The handler closure captures ``ctx`` — fresh per turn (adapters are
    rebuilt at every send). Returns the dict shape MCP expects:
    ``{"content": [{"type": "text", "text": ...}], "is_error": ...}``.
    """

    @sdk_tool(tool_def.name, tool_def.description, tool_def.schema or {"_": str})
    async def handler(args: dict) -> dict:
        try:
            result = await tool_def.handler(args, ctx)
        except Exception as e:
            logger.exception("Tool %s raised", tool_def.name)
            return {
                "content": [{"type": "text", "text": f"Tool error: {e}"}],
                "is_error": True,
            }
        text = result if isinstance(result, str) else str(result)
        return {"content": [{"type": "text", "text": text}]}

    return handler
