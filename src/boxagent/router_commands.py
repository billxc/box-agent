"""Auxiliary slash-command handlers for Router.

These are stateless or near-stateless commands that don't touch
core session/dispatch logic. Each function takes explicit dependencies
rather than the full Router instance, keeping the coupling minimal.
"""

import asyncio
import logging
import os
import re
import shutil
import signal
import sys
import time
from pathlib import Path

from boxagent.channels.base import IncomingMessage

logger = logging.getLogger(__name__)


def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill a subprocess and its children via process group (Unix) or taskkill (Windows)."""
    if proc.returncode is not None:
        return
    pid = proc.pid
    if sys.platform == "win32":
        import subprocess as sp
        try:
            sp.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                   capture_output=True, timeout=5)
        except Exception:
            proc.kill()
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()

EXEC_DEFAULT_TIMEOUT = 30
TOOL_DISPLAY_MODES = ["silent", "summary", "detailed"]


async def cmd_status(
    msg: IncomingMessage,
    *,
    channel: object,
    bot_name: str,
    cli_process: object,
    start_time: float,
    display_name: str = "",
    ai_backend: str = "",
    workspace: str = "",
    node_id: str = "",
) -> None:
    uptime = int(time.time() - start_time)
    hours, remainder = divmod(uptime, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"

    state = getattr(cli_process, "state", "unknown")
    session = getattr(cli_process, "session_id", None) or "none"
    model = getattr(cli_process, "model", "") or "default"
    yolo = getattr(cli_process, "yolo", False)
    tool_display = getattr(channel, "tool_calls_display", "")

    lines = [
        f"**Status**",
        f"Bot: {bot_name}",
    ]
    if display_name and display_name != bot_name:
        lines.append(f"Display: {display_name}")
    if node_id:
        lines.append(f"Node: {node_id}")
    lines.append(f"Backend: {ai_backend or 'unknown'}")
    lines.append(f"Model: {model}")
    lines.append(f"State: {state}")
    lines.append(f"Session: {session}")
    lines.append(f"Workspace: {workspace or '(not set)'}")
    if yolo:
        lines.append(f"Yolo: on")
    if tool_display:
        lines.append(f"Tool display: {tool_display}")
    lines.append(f"Uptime: {uptime_str}")

    await channel.send_text(msg.chat_id, "\n".join(lines))


async def cmd_start(
    msg: IncomingMessage,
    *,
    channel: object,
    bot_name: str,
) -> None:
    name = bot_name or "BoxAgent"
    await channel.send_text(
        msg.chat_id,
        f"Welcome to {name}!\n"
        "Send me a message and I'll forward it to the configured agent.\n"
        "Type /help to see available commands.",
    )


async def cmd_version(
    msg: IncomingMessage,
    *,
    channel: object,
) -> None:
    from boxagent._version import version_string
    await channel.send_text(msg.chat_id, f"`{version_string()}`")


async def cmd_help(
    msg: IncomingMessage,
    *,
    channel: object,
) -> None:
    await channel.send_text(
        msg.chat_id,
        "**Commands**\n"
        "/new — Start a fresh conversation\n"
        "/resume — List or restore a previous session\n"
        "/compact — Summarize and start a new session with context\n"
        "/model — Show or switch model (e.g. /model sonnet)\n"
        "/cd — Show or switch workspace (e.g. /cd ~/projects)\n"
        "/backend — Show or switch AI backend (claude-cli/codex-cli/codex-acp)\n"
        "/status — Show bot state and uptime\n"
        "/cancel — Cancel the current running task\n"
        "/verbose — Cycle tool call display (silent/summary/detailed)\n"
        "/exec — Run a shell command (e.g. /exec ls -la)\n"
        "/sync\\_skills — Re-sync linked skill directories\n"
        "/trust\\_workspace — Trust current workspace in Claude\n"
        "/review\\_loop — Multi-agent adversarial review loop\n"
        "/version — Show version and commit hash\n"
        "/help — Show this message\n\n"
        "Prefix with @model to use a model for one message:\n"
        "  @opus explain this code\n\n"
        "Any other message is sent to the configured agent as a prompt.",
    )


async def cmd_verbose(
    msg: IncomingMessage,
    *,
    channel: object,
) -> None:
    current = getattr(channel, "tool_calls_display", "summary")
    try:
        idx = TOOL_DISPLAY_MODES.index(current)
    except ValueError:
        idx = 0
    new_mode = TOOL_DISPLAY_MODES[(idx + 1) % len(TOOL_DISPLAY_MODES)]
    channel.tool_calls_display = new_mode
    await channel.send_text(
        msg.chat_id, f"Tool call display: {new_mode}",
    )


async def cmd_sync_skills(
    msg: IncomingMessage,
    *,
    channel: object,
    workspace: str,
    extra_skill_dirs: list[str],
    ai_backend: str,
) -> None:
    from boxagent.gateway import sync_skills
    linked = sync_skills(workspace, extra_skill_dirs, ai_backend)
    if linked:
        text = f"Synced {len(linked)} skill(s):\n" + "\n".join(
            f"• {s}" for s in sorted(linked)
        )
    else:
        text = "No skills to sync (extra\\_skill\\_dirs is empty or dirs not found)."
    await channel.send_text(msg.chat_id, text)


async def cmd_exec(
    msg: IncomingMessage,
    *,
    channel: object,
    workspace: str,
) -> None:
    """Execute a shell command and return output."""
    raw = msg.text.strip()
    # Parse: /exec [-t TIMEOUT] COMMAND
    parts = raw.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await channel.send_text(
            msg.chat_id,
            "Usage: `/exec [-t SECONDS] COMMAND`",
        )
        return

    rest = parts[1].strip()
    timeout = EXEC_DEFAULT_TIMEOUT

    # Parse optional -t flag
    if rest.startswith("-t "):
        tokens = rest.split(maxsplit=2)
        if len(tokens) >= 3:
            try:
                timeout = int(tokens[1])
                if timeout <= 0 or timeout > 600:
                    await channel.send_text(
                        msg.chat_id, "Timeout must be 1-600 seconds.",
                    )
                    return
                rest = tokens[2]
            except ValueError:
                pass  # Not a number, treat entire rest as command

    command = rest
    await channel.show_typing(msg.chat_id)

    # On Windows, run via PowerShell directly (create_subprocess_shell
    # always uses cmd.exe regardless of COMSPEC).
    cwd = workspace if workspace and Path(workspace).is_dir() else None
    if sys.platform == "win32":
        pwsh = shutil.which("pwsh") or shutil.which("pwsh.exe")
        if not pwsh:
            pwsh = shutil.which("powershell") or shutil.which("powershell.exe")
        if pwsh:
            shell_args = [pwsh, "-NoProfile", "-NoLogo", "-Command", command]
            # Disable ANSI color output (NO_COLOR is a cross-shell standard,
            # TERM=dumb is a fallback for older PowerShell)
            shell_env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}
        else:
            shell_args = None  # fallback to cmd
            shell_env = None
    else:
        shell_args = None
        shell_env = None

    try:
        if shell_args:
            proc = await asyncio.create_subprocess_exec(
                *shell_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                env=shell_env,
                start_new_session=True,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                start_new_session=True,
            )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            _kill_process_tree(proc)
            await proc.wait()
            await channel.send_text(
                msg.chat_id,
                f"Command timed out after {timeout}s (killed).",
            )
            return

        output = stdout.decode("utf-8", errors="replace").rstrip()
        # Strip ANSI escape codes (PowerShell colors, etc.)
        output = re.sub(r"\x1b\[[0-9;]*m", "", output)
        exit_code = proc.returncode

    except Exception as e:
        await channel.send_text(
            msg.chat_id, f"Exec failed: {e}",
        )
        return

    # Build response
    header = f"Exit: {exit_code}"
    if not output:
        await channel.send_text(msg.chat_id, header)
        return

    # If output fits in a code block within Telegram limit (~4000 chars)
    code_msg = f"{header}\n```\n{output}\n```"
    if len(code_msg) <= 3900:
        await channel.send_text(msg.chat_id, code_msg)
    else:
        # Too long — send as file
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, prefix="exec_",
        ) as f:
            f.write(output)
            tmp_path = f.name

        if hasattr(channel, "_bot"):
            from aiogram.types import FSInputFile
            await channel._bot.send_document(
                chat_id=int(msg.chat_id),
                document=FSInputFile(tmp_path),
                caption=header,
            )
        else:
            # Fallback: send truncated output
            truncated = output[:3500] + "\n... (truncated)"
            await channel.send_text(
                msg.chat_id, f"{header}\n```\n{truncated}\n```",
            )


async def cmd_trust_workspace(
    msg: IncomingMessage,
    *,
    channel: object,
    workspace: str,
) -> None:
    """Add the current workspace to Claude's trusted projects in ~/.claude.json."""
    import json

    if not workspace or not Path(workspace).is_dir():
        await channel.send_text(
            msg.chat_id, "No valid workspace configured for this bot.",
        )
        return

    workspace_path = Path(workspace).resolve().as_posix()
    claude_json_path = Path.home() / ".claude.json"

    if not claude_json_path.exists():
        await channel.send_text(
            msg.chat_id, f"~/.claude.json not found.",
        )
        return

    try:
        data = json.loads(claude_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        await channel.send_text(
            msg.chat_id, f"Failed to read ~/.claude.json: {e}",
        )
        return

    projects = data.setdefault("projects", {})
    project = projects.setdefault(workspace_path, {})

    if project.get("hasTrustDialogAccepted"):
        await channel.send_text(
            msg.chat_id,
            f"Already trusted: `{workspace_path}`",
        )
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
        await channel.send_text(
            msg.chat_id, f"Failed to write ~/.claude.json: {e}",
        )
        return

    await channel.send_text(
        msg.chat_id,
        f"Trusted workspace: `{workspace_path}`",
    )
