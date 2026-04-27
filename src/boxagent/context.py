"""Session context injection for first-message prompt enrichment."""

import datetime
import logging
import time as _time
from pathlib import Path

logger = logging.getLogger(__name__)


def build_session_context(
    *,
    bot_name: str = "",
    display_name: str = "",
    node_id: str = "",
    ai_backend: str = "",
    model: str = "",
    workspace: str = "",
    config_dir: str = "",
    workgroup_agents: list[str] | None = None,
    running_tasks: list[dict] | None = None,
) -> str:
    """Build a one-time context block for the first message of a session.

    Combines:
    1. BoxAgent runtime info (bot name, node, backend, model, etc.)
    2. {config_dir}/BOXAGENT.md (if exists)
    3. {workspace}/BOXAGENT.md (if exists)
    4. Workgroup agent info (if this bot is an admin with specialists)
    """
    lines = [
        "[BoxAgent Context]",
        f"bot: {bot_name}",
        f"node: {node_id or '(any)'}",
        f"backend: {ai_backend}",
        f"model: {model or 'default'}",
        f"workspace: {workspace}",
        f"time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}",
    ]
    if display_name:
        lines.insert(2, f"display_name: {display_name}")

    # Read BOXAGENT.md from config dir and workspace
    for label, base_dir in [("config", config_dir), ("workspace", workspace)]:
        content = _read_boxagent_md(base_dir)
        if content:
            lines.append(f"\n# From {label}/BOXAGENT.md")
            lines.append(content)

    # Workgroup agent delegation info
    if workgroup_agents:
        lines.append("")
        lines.append("[Workgroup]")
        lines.append("You are the admin of a workgroup. Available specialist agents:")
        for agent_name in workgroup_agents:
            lines.append(f"- {agent_name}")

        # Running tasks status
        if running_tasks:
            lines.append("")
            lines.append("Currently running specialist tasks:")
            for t in running_tasks:
                elapsed = ""
                started = t.get("started_at", 0)
                if started:
                    secs = int(_time.time() - started)
                    mins, s = divmod(secs, 60)
                    elapsed = f" (running {mins}m {s}s)"
                active = " [active]" if t.get("active") else " [queued]"
                lines.append(f"  - {t.get('task_id', '?')}: {t.get('target', '?')}{elapsed}{active}")
        else:
            lines.append("")
            lines.append("No specialist tasks currently running.")

        lines.append("")
        lines.append(
            "Use the send_to_agent MCP tool to delegate tasks to specialists. "
            "The tool sends your message to the specialist's Discord channel "
            "(visible to observers) and returns the specialist's full response."
        )
        lines.append("[/Workgroup]")

    lines.append("[/BoxAgent Context]")
    return "\n".join(lines)


def build_schedule_context(
    *,
    task_id: str,
    mode: str,
    ai_backend: str,
    model: str,
    workspace: str,
    node_id: str = "",
    bot: str = "",
) -> str:
    """Build a scheduler-specific context block prepended to task prompts."""
    lines = [
        "[BoxAgent Schedule]",
        f"task: {task_id}",
        f"mode: {mode}",
        f"node: {node_id or '(any)'}",
    ]
    if mode != "append":
        lines.append(f"backend: {ai_backend}")
        lines.append(f"model: {model or '(inherit)'}")
    lines.append(f"workspace: {workspace}")
    if bot:
        lines.append(f"bot: {bot}")
    lines.append(f"time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("[/BoxAgent Schedule]")
    lines.append("")
    lines.append(
        "IMPORTANT: After completing the task, you MUST wrap your final result summary "
        "in <ScheduleResult> tags as the LAST thing in your response. "
        "Write plain text inside — one to three concise sentences describing what was done or found. "
        "Example:"
    )
    lines.append(
        "<ScheduleResult>\n"
        "Checked disk usage: /dev/sda1 at 45%, all healthy.\n"
        "</ScheduleResult>"
    )
    return "\n".join(lines)


def _read_boxagent_md(base_dir: str) -> str:
    """Read BOXAGENT.md from a directory. Returns empty string if not found."""
    if not base_dir:
        return ""
    md_path = Path(base_dir) / "BOXAGENT.md"
    if not md_path.is_file():
        return ""
    try:
        return md_path.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning("Failed to read %s: %s", md_path, e)
        return ""
