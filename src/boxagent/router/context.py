"""Session context injection for first-message prompt enrichment."""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from boxagent.agent_env import AgentEnv

logger = logging.getLogger(__name__)


def build_session_context(
    *,
    env: AgentEnv | None = None,
    bot_name: str = "",
    display_name: str = "",
    node_id: str = "",
    workspace: str = "",
    config_dir: str = "",
) -> str:
    """Build a one-time context block for the first message of a session.

    When *env* is provided the context is derived from it; the individual
    keyword arguments are ignored.  When *env* is ``None`` the old-style
    keyword arguments are used (backward compatibility).

    Combines:
    1. BoxAgent runtime info (bot name, node, backend, model, etc.)
    2. {config_dir}/BOXAGENT.md (if exists)
    3. {workspace}/BOXAGENT.md (if exists, and different from config)
    """
    # Resolve parameters — prefer env when available
    if env is not None:
        bot_name = env.bot_name
        display_name = env.display_name
        node_id = env.node_id
        workspace = env.workspace
        config_dir = env.config_dir

    lines = [
        "[BoxAgent Context]",
        f"bot: {bot_name}",
    ]
    if display_name:
        lines.append(f"display_name: {display_name}")
    lines.append(f"node: {node_id or '(any)'}")
    lines.append(f"workspace: {workspace}")
    lines.append(f"time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # Read BOXAGENT.md and BOXAGENT-{node_id}.md — deduplicate across dirs
    seen: set[str] = set()

    for label, base_dir in [("BOXAGENT.md", config_dir), ("Workspace BOXAGENT.md", workspace)]:
        content = _read_boxagent_md(base_dir)
        if content and content not in seen:
            lines.append(f"\n# {label}")
            lines.append(content)
            seen.add(content)

    node_content = _read_boxagent_node_md(config_dir, node_id)
    if node_content and node_content not in seen:
        lines.append("\n# BOXAGENT (node)")
        lines.append(node_content)
        seen.add(node_content)

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


def _read_boxagent_node_md(base_dir: str, node_id: str) -> str:
    """Read BOXAGENT-{node_id}.md from a directory. Returns empty string if not found."""
    if not base_dir or not node_id:
        return ""
    md_path = Path(base_dir) / f"BOXAGENT-{node_id}.md"
    if not md_path.is_file():
        return ""
    try:
        return md_path.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning("Failed to read %s: %s", md_path, e)
        return ""
