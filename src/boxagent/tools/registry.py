"""Unified BoxAgent tool registry — single definition, multi-backend dispatch.

Today each AI backend has its own way to expose BoxAgent capabilities
(send_photo, schedule_*, send_to_peer, …):

- claude-cli  → ``--mcp-config`` pointing at our HTTP MCP server
- codex-cli   → no tools currently
- agent-sdk-claude  → no tools currently
- agent-sdk-copilot → no tools currently

The registry lets every tool be defined once as a plain async Python
function. Each backend then has its own *adapter* that wraps the same
registry into the form that backend understands:

- HTTP MCP server (claude-cli, codex-cli) — :mod:`boxagent.tools.adapters.mcp_http`
- in-process SdkMcpServer (agent-sdk-claude) — :mod:`boxagent.tools.adapters.claude_sdk`
- native Tool objects (agent-sdk-copilot) — :mod:`boxagent.tools.adapters.copilot_sdk`

Tool definition:

    @boxagent_tool(
        name="send_photo",
        group="telegram",
        description="Send a photo to the user via Telegram",
        schema={"file_path": str, "caption": str},
        requires=["telegram"],
    )
    async def send_photo(args: dict, ctx: ToolContext) -> str:
        ...

``requires`` is a list of capability flags an env must have for the
tool to be exposed. Adapters filter the registry per-bot using
``AgentEnv``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


# ── Per-call context ──────────────────────────────────────────────────


@dataclass
class ToolContext:
    """Per-invocation context handed to the tool body.

    Adapters populate this from whatever they have:

    - HTTP MCP adapter: ``bot_name`` / ``chat_id`` come from
      ``X-BoxAgent-*`` headers; gateway/dirs from module-level globals
      set at server creation.
    - SDK adapters: closure-captured at session creation (so ``bot_name``
      and ``chat_id`` are baked in per Router-spawned conversation).
    """

    bot_name: str
    chat_id: str
    gateway: Any = None  # boxagent.gateway.Gateway
    config_dir: str = ""
    local_dir: str = ""
    node_id: str = ""


ToolHandler = Callable[[dict, ToolContext], Awaitable[Any]]


# ── Tool definition ───────────────────────────────────────────────────


# Allowed values for ``ToolDef.group`` — drives which mcp/* endpoint a tool
# lives on (HTTP MCP path) and which capability flag injects it for SDK
# backends. Match the existing endpoint naming so claude-cli's URL routes
# stay stable.
ToolGroup = str  # "base" | "telegram" | "admin" | "peer"

# Allowed values for ``ToolDef.requires`` capability flags.
# Adapters filter tools whose requirements aren't met by the env.
KnownRequirement = str  # "telegram" | "workgroup_admin" | "peer_channel"


@dataclass
class ToolDef:
    name: str
    group: ToolGroup
    description: str
    schema: dict[str, type]
    handler: ToolHandler
    requires: list[KnownRequirement] = field(default_factory=list)


# ── Registry ──────────────────────────────────────────────────────────


# Module-level — populated at import time by every module that defines
# tools. Adapters read from here.
_TOOLS: list[ToolDef] = []


def _summarize_args(args: dict) -> dict:
    """Best-effort short, non-sensitive snapshot of tool args for error logs.

    Strings over 200 chars are truncated. Bytes / non-JSON types are
    stringified. Keys whose name suggests a secret are redacted.
    """
    if not isinstance(args, dict):
        return {"_repr": repr(args)[:300]}
    REDACT = {"token", "password", "secret", "api_key", "authorization"}
    out: dict = {}
    for k, v in args.items():
        kl = str(k).lower()
        if any(s in kl for s in REDACT):
            out[k] = "<redacted>"
            continue
        if isinstance(v, str):
            out[k] = v if len(v) <= 200 else v[:200] + f"…(+{len(v)-200})"
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        else:
            out[k] = repr(v)[:200]
    return out


def boxagent_tool(
    *,
    name: str,
    group: ToolGroup,
    description: str,
    schema: dict[str, type] | None = None,
    requires: list[KnownRequirement] | None = None,
) -> Callable[[ToolHandler], ToolHandler]:
    """Decorator that registers a tool in the global registry."""

    def decorator(fn: ToolHandler) -> ToolHandler:
        if any(t.name == name for t in _TOOLS):
            raise ValueError(f"Duplicate boxagent_tool name: {name!r}")

        async def wrapped(args: dict, ctx: "ToolContext"):
            from boxagent.log import Category, log
            args_summary = _summarize_args(args)
            try:
                result = await fn(args, ctx)
            except Exception as e:
                import traceback as _tb
                log.error(
                    Category.AGENT_TOOL_ERROR,
                    f"tool {name} raised: {type(e).__name__}: {e}",
                    tool=name,
                    bot=getattr(ctx, "bot_name", None),
                    chat_id=getattr(ctx, "chat_id", None),
                    args=args_summary,
                    exception=type(e).__name__,
                    traceback=_tb.format_exc(limit=20),
                )
                raise
            if isinstance(result, str) and result.lstrip().lower().startswith("error:"):
                log.error(
                    Category.AGENT_TOOL_ERROR,
                    f"tool {name} returned: {result[:300]}",
                    tool=name,
                    bot=getattr(ctx, "bot_name", None),
                    chat_id=getattr(ctx, "chat_id", None),
                    args=args_summary,
                )
            return result

        _TOOLS.append(ToolDef(
            name=name,
            group=group,
            description=description,
            schema=schema or {},
            handler=wrapped,
            requires=list(requires or []),
        ))
        return wrapped

    return decorator


def all_tools() -> list[ToolDef]:
    """Return every registered tool. Adapters use this + filter by env."""
    return list(_TOOLS)


def tools_for(*, group: str | None = None, env_caps: set[str] | None = None) -> list[ToolDef]:
    """Filter the registry.

    Args:
        group: If set, restrict to tools in this group.
        env_caps: Capability flags this env satisfies (e.g. {"telegram",
            "workgroup_admin"}). Tools whose ``requires`` aren't all in
            ``env_caps`` are excluded. ``None`` skips capability filtering.
    """
    out = []
    for t in _TOOLS:
        if group is not None and t.group != group:
            continue
        if env_caps is not None and not set(t.requires).issubset(env_caps):
            continue
        out.append(t)
    return out


def env_capabilities(env: Any) -> set[str]:
    """Translate an :class:`AgentEnv` (or anything with the right attrs) to
    the capability flag set used by ``ToolDef.requires``."""
    capabilities: set[str] = set()
    if env is None:
        return capabilities
    if getattr(env, "has_telegram", False):
        capabilities.add("telegram")
    if getattr(env, "is_workgroup_admin", False):
        capabilities.add("workgroup_admin")
    if getattr(env, "has_peer_channel", False):
        capabilities.add("peer_channel")
    return capabilities


def _reset_for_tests() -> None:
    """Empty the registry. Tests use this to isolate state."""
    _TOOLS.clear()
