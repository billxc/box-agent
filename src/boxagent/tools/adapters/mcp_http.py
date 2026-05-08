"""Adapter: BoxAgent registry → FastMCP HTTP server.

Used by ``transports/mcp/server.py`` to register registry tools onto the
existing HTTP MCP servers consumed by claude-cli / codex-cli. Per-call
context (bot_name, chat_id) comes from HTTP headers — the MCP server's
middleware already populates ContextVars; this adapter reads those into
a ``ToolContext`` for each invocation.
"""

from __future__ import annotations

import logging
from typing import Any

from boxagent.tools import ToolContext, ToolDef

logger = logging.getLogger(__name__)


def register_into(
    mcp,  # mcp.server.fastmcp.FastMCP
    tool_defs: list[ToolDef],
    *,
    bot_name_var,   # ContextVar[str]
    chat_id_var,    # ContextVar[str]
    gateway: Any = None,
    config_dir: str = "",
    local_dir: str = "",
    node_id: str = "",
) -> None:
    """Register every tool_def onto the FastMCP server.

    ``bot_name_var`` and ``chat_id_var`` are the ContextVars the HTTP
    middleware sets per request. Gateway / dirs / node_id are fixed at
    server creation (they don't change per request).
    """
    for tool_def in tool_defs:
        _register_one(
            mcp, tool_def,
            bot_name_var=bot_name_var, chat_id_var=chat_id_var,
            gateway=gateway, config_dir=config_dir,
            local_dir=local_dir, node_id=node_id,
        )


def _register_one(
    mcp,
    tool_def: ToolDef,
    *,
    bot_name_var,
    chat_id_var,
    gateway,
    config_dir: str,
    local_dir: str,
    node_id: str,
) -> None:
    """Wrap a ToolDef as a FastMCP tool and register it.

    FastMCP introspects the function signature to build the JSON schema,
    so we synthesise a function with ``**kwargs`` matching the registry
    schema. Per-invocation we build a fresh ToolContext from the
    ContextVars set by middleware.
    """
    schema = tool_def.schema or {}

    # FastMCP needs the wrapper function to advertise typed parameters
    # (so the LLM sees the right input schema). We synthesise it using
    # the schema dict.
    param_lines = []
    for name, py_type in schema.items():
        type_name = getattr(py_type, "__name__", "str")
        param_lines.append(f"{name}: {type_name} = ''" if py_type is str else f"{name}: {type_name} = None")

    # Use exec to construct a function signature matching the schema. Done
    # at registration time, not at call time, so per-invocation cost is
    # nil. The resulting closure calls _invoke().
    src = (
        f"async def _wrapper({', '.join(param_lines)}) -> str:\n"
        f"    return await _invoke(locals())\n"
    )

    async def _invoke(args: dict) -> str:
        ctx = ToolContext(
            bot_name=bot_name_var.get(),
            chat_id=chat_id_var.get(),
            gateway=gateway,
            config_dir=config_dir,
            local_dir=local_dir,
            node_id=node_id,
        )
        try:
            result = await tool_def.handler(args, ctx)
        except Exception as e:
            logger.exception("Tool %s raised", tool_def.name)
            return f"Tool error: {e}"
        return result if isinstance(result, str) else str(result)

    namespace: dict[str, Any] = {"_invoke": _invoke}
    exec(src, namespace)
    wrapper = namespace["_wrapper"]
    wrapper.__name__ = tool_def.name
    wrapper.__doc__ = tool_def.description
    mcp.tool()(wrapper)
