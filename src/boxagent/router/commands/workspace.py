"""Workspace commands — re-sync skills, browse sessions, mark workspace trusted."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from boxagent.router.commands.registry import CommandCategory, command

if TYPE_CHECKING:
    from boxagent.router.core import Router
    from boxagent.transports.base import Channel, IncomingMessage


@command("/sync_skills", help="Re-sync linked skill directories", category=CommandCategory.WORKSPACE)
async def cmd_sync_skills(router: "Router", msg: "IncomingMessage", channel: "Channel") -> None:
    from boxagent.agent.workspace import sync_skills
    linked = sync_skills(router.workspace, router.extra_skill_dirs, router.ai_backend)
    if linked:
        text = f"Synced {len(linked)} skill(s):\n" + "\n".join(f"• {s}" for s in sorted(linked))
    else:
        text = "No skills to sync (extra\\_skill\\_dirs is empty or dirs not found)."
    await channel.send_text(msg.chat_id, text)


@command(
    "/sessions",
    help="Browse sessions (e.g. /sessions chromium 7d backend:codex-cli)",
    category=CommandCategory.WORKSPACE,
)
async def cmd_sessions(router: "Router", msg: "IncomingMessage", channel: "Channel") -> None:
    """List unified sessions (Claude CLI + BoxAgent history + Codex)."""
    from boxagent.sessions.browser import format_sessions_list

    arg = msg.text.strip().partition(" ")[2].strip()
    text = format_sessions_list(query=arg, storage=router.storage, workspace=router.workspace)
    await channel.send_text(msg.chat_id, text)


@command("/trust_workspace", help="Trust current workspace in Claude", category=CommandCategory.WORKSPACE)
async def cmd_trust_workspace(router: "Router", msg: "IncomingMessage", channel: "Channel") -> None:
    """Add the current workspace to Claude's trusted projects in ~/.claude.json."""
    workspace = router.workspace
    if not workspace or not Path(workspace).is_dir():
        await channel.send_text(msg.chat_id, "No valid workspace configured for this bot.")
        return

    workspace_path = Path(workspace).resolve().as_posix()
    claude_json_path = Path.home() / ".claude.json"

    if not claude_json_path.exists():
        await channel.send_text(msg.chat_id, "~/.claude.json not found.")
        return

    try:
        data = json.loads(claude_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        await channel.send_text(msg.chat_id, f"Failed to read ~/.claude.json: {e}")
        return

    projects = data.setdefault("projects", {})
    project = projects.setdefault(workspace_path, {})

    if project.get("hasTrustDialogAccepted"):
        await channel.send_text(msg.chat_id, f"Already trusted: `{workspace_path}`")
        return

    project["hasTrustDialogAccepted"] = True
    project.setdefault("allowedTools", [])
    project.setdefault("mcpContextUris", [])
    project.setdefault("mcpServers", {})
    project.setdefault("enabledMcpjsonServers", [])
    project.setdefault("disabledMcpjsonServers", [])
    project.setdefault("ignorePatterns", [])

    try:
        claude_json_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        await channel.send_text(msg.chat_id, f"Failed to write ~/.claude.json: {e}")
        return

    await channel.send_text(msg.chat_id, f"Trusted workspace: `{workspace_path}`")
