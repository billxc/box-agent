"""Adapter: BoxAgent registry → github-copilot-sdk native tools.

Returns a list of ``copilot.Tool`` suitable for the ``tools=`` kwarg of
``CopilotClient.create_session``. No MCP wrapper — the SDK calls these
handlers directly.

Tool group is flattened: Copilot has no concept of named "tool groups",
so every visible tool ends up in the same flat list. The adapter still
filters by env capabilities, matching the HTTP MCP server's
endpoint-per-group gating.
"""

from __future__ import annotations

import logging
from typing import Any

from copilot.tools import Tool, ToolInvocation, ToolResult

from boxagent.tools import ToolContext, ToolDef, env_capabilities, tools_for

logger = logging.getLogger(__name__)


def build_tools(*, ctx: ToolContext, env: Any) -> list[Tool]:
    """Build the ``tools`` list for ``CopilotClient.create_session``."""
    caps = env_capabilities(env)
    visible = tools_for(env_caps=caps)
    return [_convert(t, ctx) for t in visible]


def _convert(tool_def: ToolDef, ctx: ToolContext) -> Tool:
    """Wrap a ToolDef into a copilot.Tool.

    Copilot signs the schema as a JSON Schema dict. We translate the
    registry's simple ``{name: type}`` mapping into JSON Schema with all
    fields optional (the registry doesn't currently distinguish required
    fields; tools handle missing args by reading defaults).
    """

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            name: {"type": _python_type_to_json_schema(t)}
            for name, t in (tool_def.schema or {}).items()
        },
    }

    async def handler(invocation: ToolInvocation) -> ToolResult:
        args = invocation.arguments if isinstance(invocation.arguments, dict) else {}
        try:
            result = await tool_def.handler(args, ctx)
        except Exception as e:
            logger.exception("Tool %s raised", tool_def.name)
            return ToolResult(
                text_result_for_llm=f"Tool error: {e}",
                result_type="error",  # type: ignore[arg-type]
                error=str(e),
            )
        text = result if isinstance(result, str) else str(result)
        return ToolResult(text_result_for_llm=text, result_type="success")

    return Tool(
        name=tool_def.name,
        description=tool_def.description,
        handler=handler,
        parameters=parameters,
    )


def _python_type_to_json_schema(t: type) -> str:
    if t is str:
        return "string"
    if t is int:
        return "integer"
    if t is float:
        return "number"
    if t is bool:
        return "boolean"
    if t is list:
        return "array"
    if t is dict:
        return "object"
    return "string"  # fallback
