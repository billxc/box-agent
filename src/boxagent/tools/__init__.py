"""BoxAgent tool registry — single source of truth for tools across backends.

See :mod:`boxagent.tools.registry` for the design overview.
"""

from boxagent.tools.registry import (
    ToolContext,
    ToolDef,
    all_tools,
    boxagent_tool,
    env_capabilities,
    tools_for,
)

__all__ = [
    "ToolContext",
    "ToolDef",
    "all_tools",
    "boxagent_tool",
    "env_capabilities",
    "tools_for",
]
