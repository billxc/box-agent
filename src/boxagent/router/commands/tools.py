"""Tool commands — shell exec + schedule management."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from boxagent.router.commands.registry import CommandCategory, command

if TYPE_CHECKING:
    from boxagent.router.core import Router
    from boxagent.transports.base import Channel, IncomingMessage

logger = logging.getLogger(__name__)


EXEC_DEFAULT_TIMEOUT = 30


def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill a subprocess and its children via process group (Unix) or taskkill (Windows)."""
    if proc.returncode is not None:
        return
    pid = proc.pid
    if sys.platform == "win32":
        import subprocess
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=5,
            )
        except Exception:
            proc.kill()
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


@command("/exec", help="Run a shell command (e.g. /exec ls -la)", category=CommandCategory.TOOLS)
async def cmd_exec(router: "Router", msg: "IncomingMessage", channel: "Channel") -> None:
    """Execute a shell command and return output."""
    raw = msg.text.strip()
    parts = raw.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await channel.send_text(msg.chat_id, "Usage: `/exec [-t SECONDS] COMMAND`")
        return

    rest = parts[1].strip()
    timeout = EXEC_DEFAULT_TIMEOUT

    if rest.startswith("-t "):
        tokens = rest.split(maxsplit=2)
        if len(tokens) >= 3:
            try:
                timeout = int(tokens[1])
                if timeout <= 0 or timeout > 600:
                    await channel.send_text(msg.chat_id, "Timeout must be 1-600 seconds.")
                    return
                rest = tokens[2]
            except ValueError:
                pass

    command_str = rest
    workspace = router.workspace
    await channel.show_typing(msg.chat_id)

    cwd = workspace if workspace and Path(workspace).is_dir() else None
    shell_args: list[str] | None
    shell_env: dict[str, str] | None
    if sys.platform == "win32":
        pwsh = shutil.which("pwsh") or shutil.which("pwsh.exe")
        if not pwsh:
            pwsh = shutil.which("powershell") or shutil.which("powershell.exe")
        if pwsh:
            shell_args = [pwsh, "-NoProfile", "-NoLogo", "-Command", command_str]
            shell_env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}
        else:
            shell_args = None
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
                command_str,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                start_new_session=True,
            )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            _kill_process_tree(proc)
            await proc.wait()
            await channel.send_text(msg.chat_id, f"Command timed out after {timeout}s (killed).")
            return

        output = stdout.decode("utf-8", errors="replace").rstrip()
        output = re.sub(r"\x1b\[[0-9;]*m", "", output)
        exit_code = proc.returncode

    except Exception as e:
        await channel.send_text(msg.chat_id, f"Exec failed: {e}")
        return

    header = f"Exit: {exit_code}"
    if not output:
        await channel.send_text(msg.chat_id, header)
        return

    code_msg = f"{header}\n```\n{output}\n```"
    if len(code_msg) <= 3900:
        await channel.send_text(msg.chat_id, code_msg)
    else:
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, prefix="exec_",
        ) as f:
            f.write(output)
            tmp_path = f.name

        bot = getattr(channel, "_bot", None)
        if bot is not None:
            from aiogram.types import FSInputFile
            await bot.send_document(
                chat_id=int(msg.chat_id),
                document=FSInputFile(tmp_path),
                caption=header,
            )
        else:
            truncated = output[:3500] + "\n... (truncated)"
            await channel.send_text(msg.chat_id, f"{header}\n```\n{truncated}\n```")


@command("/schedule", help="Manage schedules (list/logs/show/run)", category=CommandCategory.TOOLS)
async def cmd_schedule(router: "Router", msg: "IncomingMessage", channel: "Channel") -> None:
    """Dispatch /schedule subcommands: list, logs, show, run, add."""
    from boxagent.scheduler.cli import (
        add_schedule,
        format_schedule_list,
        format_schedule_logs,
        format_schedule_run_detail,
        format_schedule_show,
        trigger_schedule_run,
    )

    config_dir = router.config_dir
    local_dir = router.local_dir
    node_id = router.node_id

    parts = msg.text.strip().split(maxsplit=2)
    sub = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""

    if sub == "list":
        text = format_schedule_list(config_dir, node_id)
    elif sub == "logs":
        if not local_dir:
            text = "Local dir not configured."
        else:
            text = format_schedule_logs(local_dir, task_id=arg, n=15)
    elif sub == "show" and arg:
        text = format_schedule_show(config_dir, node_id, arg)
    elif sub == "run" and arg:
        if not local_dir:
            text = "Local dir not configured."
        else:
            text = trigger_schedule_run(local_dir, arg)
    elif sub == "run-log" and arg:
        if not local_dir:
            text = "Local dir not configured."
        else:
            run_parts = arg.split(maxsplit=1)
            rid = run_parts[0]
            run_n = 1
            if len(run_parts) > 1:
                try:
                    run_n = int(run_parts[1])
                except ValueError:
                    pass
            text = format_schedule_run_detail(local_dir, rid, run_n)
    elif sub == "add":
        text = _parse_and_add_schedule(arg, config_dir, add_schedule)
    else:
        text = (
            "**Usage**\n"
            "/schedule list — List all schedules\n"
            "/schedule logs [task\\_id] — Show execution logs\n"
            "/schedule run-log <task\\_id> [N] — Show full detail for Nth run (1=latest)\n"
            "/schedule show <task\\_id> — Show schedule details\n"
            "/schedule run <task\\_id> — Run a schedule once\n"
            "/schedule add <params> — Add a schedule\n"
            "  params: id=<id> cron=<expr> prompt=<text> "
            "[mode=isolate] [ai\\_backend=claude-cli] [model=<model>]"
        )

    await channel.send_text(msg.chat_id, text)


def _parse_and_add_schedule(arg: str, config_dir, add_fn) -> str:
    """Parse key=value params from /schedule add and call add_schedule."""
    if not arg:
        return (
            "Usage: /schedule add id=<id> cron=<expr> prompt=<text> "
            "[mode=isolate] [ai\\_backend=claude-cli] [model=sonnet] [bot=<name>]"
        )

    import shlex
    try:
        tokens = shlex.split(arg)
    except ValueError:
        tokens = arg.split()

    params: dict[str, str] = {}
    for token in tokens:
        if "=" in token:
            k, _, v = token.partition("=")
            params[k] = v

    task_id = params.get("id", "")
    cron = params.get("cron", "")
    prompt = params.get("prompt", "")
    if not task_id or not cron or not prompt:
        return "Error: id, cron, and prompt are required."

    return add_fn(
        config_dir=config_dir,
        task_id=task_id,
        cron=cron,
        prompt=prompt,
        mode=params.get("mode", "isolate"),
        bot=params.get("bot", ""),
        ai_backend=params.get("ai_backend", ""),
        model=params.get("model", ""),
    )
