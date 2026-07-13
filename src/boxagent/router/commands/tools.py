"""Tool commands — shell exec + schedule management."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from boxagent.router.commands.registry import CommandCategory, command
from boxagent.shell_exec import EXEC_DEFAULT_TIMEOUT, run_shell_command

if TYPE_CHECKING:
    from boxagent.router.core import Router
    from boxagent.transports.base import Channel, IncomingMessage

logger = logging.getLogger(__name__)


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
    await channel.show_typing(msg.chat_id)

    try:
        result = await run_shell_command(
            command_str, workspace=router.workspace, timeout=timeout,
        )
    except Exception as e:
        await channel.send_text(msg.chat_id, f"Exec failed: {e}")
        return

    if result.timed_out:
        await channel.send_text(msg.chat_id, f"Command timed out after {timeout}s (killed).")
        return

    output = result.output
    exit_code = result.exit_code

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
