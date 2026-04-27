#!/usr/bin/env python3
"""BoxAgent MCP server — schedule and session tools.

Injected for all agents (admin, specialist, regular bots).

Receives configuration via environment variables:
  BOXAGENT_CONFIG_DIR, BOXAGENT_LOCAL_DIR, BOXAGENT_NODE_ID
"""

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("boxagent")

CONFIG_DIR = os.environ.get("BOXAGENT_CONFIG_DIR", "")
LOCAL_DIR = os.environ.get("BOXAGENT_LOCAL_DIR", "")
NODE_ID = os.environ.get("BOXAGENT_NODE_ID", "")


# ---- Schedule tools ----


@mcp.tool()
def schedule_list() -> str:
    """List all configured scheduled tasks with their cron, mode, and prompt."""
    if not CONFIG_DIR:
        return "BOXAGENT_CONFIG_DIR not set."
    from boxagent.scheduler.cli import format_schedule_list

    return format_schedule_list(CONFIG_DIR, NODE_ID)


@mcp.tool()
def schedule_add(
    task_id: str,
    cron: str,
    prompt: str,
    mode: str = "isolate",
    bot: str = "",
    ai_backend: str = "",
    model: str = "",
) -> str:
    """Add a new scheduled task.

    Args:
        task_id: Unique task ID
        cron: Cron expression (5-field, e.g. "0 9 * * 1-5")
        prompt: Prompt to send when the schedule fires
        mode: Execution mode - "isolate" (standalone) or "append" (send to bot)
        bot: Bot name (required when mode=append)
        ai_backend: Backend for isolate mode (claude-cli, codex-cli, codex-acp)
        model: Model for isolate mode (e.g. sonnet, opus). Empty = default model
    """
    if not CONFIG_DIR:
        return "BOXAGENT_CONFIG_DIR not set."
    from boxagent.scheduler.cli import add_schedule as _add

    return _add(
        config_dir=CONFIG_DIR,
        task_id=task_id,
        cron=cron,
        prompt=prompt,
        mode=mode,
        bot=bot,
        ai_backend=ai_backend,
        model=model,
    )


@mcp.tool()
def schedule_logs(task_id: str = "") -> str:
    """Show recent schedule execution logs.

    Args:
        task_id: Optional task ID to filter logs for a specific schedule
    """
    if not LOCAL_DIR:
        return "BOXAGENT_LOCAL_DIR not set."
    from boxagent.scheduler.cli import format_schedule_logs

    return format_schedule_logs(LOCAL_DIR, task_id=task_id)


@mcp.tool()
def schedule_show(task_id: str) -> str:
    """Show detailed configuration for a specific scheduled task.

    Args:
        task_id: The schedule task ID to show
    """
    if not CONFIG_DIR:
        return "BOXAGENT_CONFIG_DIR not set."
    from boxagent.scheduler.cli import format_schedule_show

    return format_schedule_show(CONFIG_DIR, NODE_ID, task_id)


@mcp.tool()
def schedule_run(task_id: str) -> str:
    """Trigger a scheduled task to run immediately (async).

    Args:
        task_id: The schedule task ID to run
    """
    if not LOCAL_DIR:
        return "BOXAGENT_LOCAL_DIR not set."
    from boxagent.scheduler.cli import trigger_schedule_run

    return trigger_schedule_run(LOCAL_DIR, task_id)


@mcp.tool()
def schedule_run_detail(task_id: str, run_index: int = 1) -> str:
    """Show full details for a specific schedule run log entry.

    Args:
        task_id: The schedule task ID
        run_index: Which run to show (1 = most recent, 2 = second most recent, etc.)
    """
    if not LOCAL_DIR:
        return "BOXAGENT_LOCAL_DIR not set."
    from boxagent.scheduler.cli import format_schedule_run_detail

    return format_schedule_run_detail(LOCAL_DIR, task_id, run_index)


# ---- Session tools ----


@mcp.tool()
def sessions_list(query: str = "", workspace: str = "") -> str:
    """Search and list sessions (Claude CLI + BoxAgent history + Codex).

    By default, only sessions matching *workspace* are shown.
    Use ``--all`` in the query to search across all projects.

    Query syntax (all tokens are optional, order-independent):
        --all           Show sessions from all projects (skip workspace filter)
        <keywords>      Text search on summary/prompt/project/path (multi-word AND)
        cwd:<substr>    Fuzzy match on session projectPath (bypasses workspace filter)
        grep:<substr>   Full-text search inside session JSONL content (applied last)
        <N>d            Only sessions modified in the last N days (e.g. 7d)
        backend:<name>  Filter by backend (e.g. claude-cli, codex-cli)
        bot:<name>      Filter by bot name
        p<N>            Page number (e.g. p2)
        <hex-prefix>    Lookup session by ID prefix (4+ hex chars)

    Examples:
        "7d"                        — current project, last 7 days
        "--all discord"             — all projects, keyword "discord"
        "cwd:chromium grep:WebView" — path contains "chromium", content contains "WebView"
        "grep:TODO 7d"              — current project, last 7 days, content contains "TODO"

    Args:
        query: Search query string (see syntax above)
        workspace: Project directory path to scope results (default: all projects)
    """
    from boxagent.sessions.cli import format_sessions_list
    from boxagent.sessions import Storage

    storage = Storage(LOCAL_DIR) if LOCAL_DIR else None
    return format_sessions_list(query=query, storage=storage, workspace=workspace)


if __name__ == "__main__":
    mcp.run(transport="stdio")
