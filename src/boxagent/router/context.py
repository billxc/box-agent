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
    ai_backend: str = "",
    model: str = "",
    workspace: str = "",
    config_dir: str = "",
    workgroup_agents: list[str] | None = None,
    running_tasks: list[dict] | None = None,
) -> str:
    """Build a one-time context block for the first message of a session.

    When *env* is provided the context is derived from it; the individual
    keyword arguments are ignored.  When *env* is ``None`` the old-style
    keyword arguments are used (backward compatibility).

    Combines:
    1. BoxAgent runtime info (bot name, node, backend, model, etc.)
    2. {config_dir}/BOXAGENT.md (if exists)
    3. {workspace}/BOXAGENT.md (if exists, and different from config)
    4. Workgroup agent info (if this bot is an admin with specialists)
    """
    # Resolve parameters — prefer env when available
    if env is not None:
        bot_name = env.bot_name
        display_name = env.display_name
        node_id = env.node_id
        ai_backend = env.ai_backend
        model = env.model
        workspace = env.workspace
        config_dir = env.config_dir
        workgroup_agents = list(env.workgroup_agents) if env.workgroup_agents else None
        running_tasks = list(env.running_tasks) if env.running_tasks else None
        has_peer_channel = env.has_peer_channel
    else:
        has_peer_channel = False

    lines = [
        "[BoxAgent Context]",
        f"bot: {bot_name}",
    ]
    if display_name:
        lines.append(f"display_name: {display_name}")
    lines.append(f"node: {node_id or '(any)'}")
    lines.append(f"workspace: {workspace}")
    lines.append(f"time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # Read BOXAGENT.md — deduplicate if config and workspace point to same file
    config_content = _read_boxagent_md(config_dir)
    workspace_content = _read_boxagent_md(workspace)

    if config_content:
        lines.append(f"\n# BOXAGENT.md")
        lines.append(config_content)
    if workspace_content and workspace_content != config_content:
        lines.append(f"\n# Workspace BOXAGENT.md")
        lines.append(workspace_content)

    # Workgroup agent delegation info
    if workgroup_agents:
        from boxagent.workgroup.manager import format_running_tasks

        lines.append("")
        lines.append("[Workgroup]")
        lines.append("You are the admin of a workgroup. Available specialist agents:")
        for agent_name in workgroup_agents:
            lines.append(f"- {agent_name}")

        # Running tasks status
        lines.append("")
        lines.append(format_running_tasks(running_tasks))

        lines.append("")
        lines.append(
            "Use the send_to_agent MCP tool to delegate tasks to specialists. "
            "The specialist processes your message asynchronously and returns "
            "the result via callback."
        )
        lines.append("[/Workgroup]")

    # Peer messaging info
    if has_peer_channel:
        peer_info = _read_peers_yaml(config_dir)
        lines.append("")
        lines.append("[Peer Messaging]")
        lines.append("You can send messages to other bots using the send_to_peer MCP tool.")
        if peer_info:
            lines.append("Peers:")
            lines.append(peer_info)
        lines.append("[/Peer Messaging]")

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


def _read_peers_yaml(config_dir: str) -> str:
    """Read {config_dir}/peers.yaml and format as peer list.

    Expected format::

        mbp-bot:
          description: Macbook bot, handles macOS development
        win-bot:
          description: Windows bot, handles Edge builds

    Returns formatted string or empty string if not found.
    """
    if not config_dir:
        return ""
    peers_path = Path(config_dir) / "peers.yaml"
    if not peers_path.is_file():
        return ""
    try:
        import yaml

        data = yaml.safe_load(peers_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return ""
        lines = []
        for name, info in data.items():
            desc = ""
            if isinstance(info, dict):
                desc = info.get("description", "")
            elif isinstance(info, str):
                desc = info
            lines.append(f"- {name}: {desc}" if desc else f"- {name}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("Failed to read %s: %s", peers_path, e)
        return ""
