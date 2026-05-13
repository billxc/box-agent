"""Info commands — read-only status / version / help / display toggles."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from boxagent.router.commands.registry import COMMAND_REGISTRY, CommandCategory, CommandSpec, command

if TYPE_CHECKING:
    from boxagent.router.core import Router
    from boxagent.transports.base import Channel, IncomingMessage


TOOL_DISPLAY_MODES = ["silent", "summary", "detailed"]


def _fmt_tokens(n: int) -> str:
    if n >= 1000:
        return f"{n // 1000}k"
    return str(n)


def _format_usage_line(usage) -> str:
    """Render usage dict as ``Last turn: in 12k · out 3k · cache 45k``.

    Returns "" when usage is missing or empty.
    """
    if not isinstance(usage, dict) or not usage:
        return ""
    parts = []
    if isinstance(usage.get("input_tokens"), int):
        parts.append(f"in {_fmt_tokens(usage['input_tokens'])}")
    if isinstance(usage.get("output_tokens"), int):
        parts.append(f"out {_fmt_tokens(usage['output_tokens'])}")
    cache = usage.get("cache_read_input_tokens", 0)
    if isinstance(cache, int) and cache:
        parts.append(f"cache {_fmt_tokens(cache)}")
    return "Last turn: " + " · ".join(parts) if parts else ""


@command("/status", help="Show bot state and uptime", category=CommandCategory.INFO)
async def cmd_status(router: "Router", msg: "IncomingMessage", channel: "Channel") -> None:
    chat_id = msg.chat_id
    uptime = int(time.time() - router.start_time)
    hours, remainder = divmod(uptime, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"

    session_info = None
    if router.pool and chat_id:
        session = router.pool.get_session_id(chat_id) or "none"
        model = router.pool.get_model(chat_id) or "default"
        active = router.pool.get_active(chat_id)
        state = active.state if active else "idle"
        workspace = router.pool.get_workspace(chat_id) or router.workspace
        session_id = router.pool.get_session_id(chat_id) or ""
        if session_id:
            from boxagent.sessions.info_builder import build_session_info
            try:
                session_info = await build_session_info(
                    session_id=session_id,
                    backend_kind=router.ai_backend or "",
                    model=router.pool.get_model(chat_id) or "",
                    workspace=router.pool.get_workspace(chat_id) or "",
                )
            except Exception:
                session_info = None
    else:
        state = router.backend.state
        session = router.backend.session_id or "none"
        model = router.backend.model or "default"
        workspace = router.workspace
    yolo = router.backend.yolo
    tool_display = getattr(channel, "tool_calls_display", "")

    bot_name = router.display_name or router.bot_name
    lines = ["**Status**", f"Bot: {bot_name}"]
    if router.display_name and router.display_name != router.bot_name:
        lines.append(f"Display: {router.display_name}")
    if router.node_id:
        lines.append(f"Node: {router.node_id}")
    lines.append(f"Backend: {router.ai_backend or 'unknown'}")
    lines.append(f"Model: {model}")
    lines.append(f"State: {state}")
    lines.append(f"Session: {session}")
    lines.append(f"Workspace: {workspace or '(not set)'}")
    if yolo:
        lines.append("Yolo: on")
    if tool_display:
        lines.append(f"Tool display: {tool_display}")
    if session_info is not None:
        usage_line = _format_usage_line(session_info.last_turn_usage)
        if usage_line:
            lines.append(usage_line)
        if session_info.context_window and session_info.context_used:
            pct = round(session_info.context_used / session_info.context_window * 100)
            lines.append(
                f"Context: {_fmt_tokens(session_info.context_used)}/"
                f"{_fmt_tokens(session_info.context_window)} ({pct}%)"
            )
        if session_info.message_count:
            lines.append(f"Messages: {session_info.message_count}")
    lines.append(f"Uptime: {uptime_str}")

    await channel.send_text(chat_id, "\n".join(lines))


@command("/start", help="Welcome message", category=CommandCategory.INFO)
async def cmd_start(router: "Router", msg: "IncomingMessage", channel: "Channel") -> None:
    name = (router.display_name or router.bot_name) or "BoxAgent"
    await channel.send_text(
        msg.chat_id,
        f"Welcome to {name}!\n"
        "Send me a message and I'll forward it to the configured agent.\n"
        "Type /help to see available commands.",
    )


@command("/version", help="Show version and commit hash", category=CommandCategory.INFO)
async def cmd_version(router: "Router", msg: "IncomingMessage", channel: "Channel") -> None:
    from boxagent._version import version_string
    await channel.send_text(msg.chat_id, f"`{version_string()}`")


@command("/verbose", help="Cycle tool call display (silent/summary/detailed)", category=CommandCategory.INFO)
async def cmd_verbose(router: "Router", msg: "IncomingMessage", channel: "Channel") -> None:
    current = getattr(channel, "tool_calls_display", "summary")
    try:
        index = TOOL_DISPLAY_MODES.index(current)
    except ValueError:
        index = 0
    new_mode = TOOL_DISPLAY_MODES[(index + 1) % len(TOOL_DISPLAY_MODES)]
    setattr(channel, "tool_calls_display", new_mode)
    await channel.send_text(msg.chat_id, f"Tool call display: {new_mode}")


@command("/help", help="Show this message", category=CommandCategory.INFO)
async def cmd_help(router: "Router", msg: "IncomingMessage", channel: "Channel") -> None:
    """Auto-generated from COMMAND_REGISTRY: any command registered with a
    non-empty ``help=`` shows up here, grouped by ``category``. Section
    order = enum-declaration order in :class:`CommandCategory`; commands
    with ``category=None`` render last under "Other".

    Underscores escaped for Telegram MarkdownV2.
    """
    by_category: dict[CommandCategory | None, list[CommandSpec]] = {}
    for spec in COMMAND_REGISTRY.values():
        if not spec.help:
            continue
        by_category.setdefault(spec.category, []).append(spec)

    ordered: list[CommandCategory | None] = [cat for cat in CommandCategory if cat in by_category]
    if None in by_category:
        ordered.append(None)

    lines = ["**Commands**"]
    for cat in ordered:
        title = cat.value if cat is not None else "Other"
        lines.append(f"\n_{title}_")
        for spec in by_category[cat]:
            escaped = spec.name.replace("_", "\\_")
            lines.append(f"{escaped} — {spec.help}")
    lines.append("")
    lines.append("Prefix with @model to use a model for one message:")
    lines.append("  @opus explain this code")
    lines.append("")
    lines.append("Any other message is sent to the configured agent as a prompt.")
    await channel.send_text(msg.chat_id, "\n".join(lines))
