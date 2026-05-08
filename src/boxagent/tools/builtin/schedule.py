"""Schedule tools — list / add / show / del / enable / disable / run / logs.

These wrap the existing scheduler/cli.py business functions. They're
visible to every bot (group="base", no requires) since schedule
management isn't tied to a specific channel or role.
"""

from __future__ import annotations

import logging

from boxagent.tools import ToolContext, boxagent_tool

logger = logging.getLogger(__name__)


@boxagent_tool(
    name="schedule_list",
    group="base",
    description="List all configured scheduled tasks with their cron, mode, and prompt.",
    schema={},
)
async def schedule_list(args: dict, ctx: ToolContext) -> str:
    if not ctx.config_dir:
        return "Config dir not set."
    from boxagent.scheduler.cli import format_schedule_list
    return format_schedule_list(ctx.config_dir, ctx.node_id)


@boxagent_tool(
    name="schedule_add",
    group="base",
    description=(
        "Add a new scheduled task. mode is 'isolate' (standalone "
        "subprocess) or 'append' (append to a bot's running session). "
        "isolate mode requires ai_backend (claude-cli/codex-cli/agent-sdk-claude/agent-sdk-copilot) "
        "and optional model. append mode requires bot."
    ),
    schema={
        "task_id": str, "cron": str, "prompt": str,
        "mode": str, "bot": str, "ai_backend": str, "model": str,
    },
)
async def schedule_add(args: dict, ctx: ToolContext) -> str:
    if not ctx.config_dir:
        return "Config dir not set."
    from boxagent.scheduler.cli import add_schedule
    return add_schedule(
        config_dir=ctx.config_dir,
        task_id=args["task_id"],
        cron=args["cron"],
        prompt=args["prompt"],
        mode=args.get("mode", "isolate"),
        bot=args.get("bot", ""),
        ai_backend=args.get("ai_backend", ""),
        model=args.get("model", ""),
    )


@boxagent_tool(
    name="schedule_logs",
    group="base",
    description="Show recent schedule execution logs. Pass task_id to filter to one task.",
    schema={"task_id": str},
)
async def schedule_logs(args: dict, ctx: ToolContext) -> str:
    if not ctx.local_dir:
        return "Local dir not set."
    from boxagent.scheduler.cli import format_schedule_logs
    return format_schedule_logs(ctx.local_dir, task_id=args.get("task_id", ""))


@boxagent_tool(
    name="schedule_show",
    group="base",
    description="Show detailed configuration for a specific scheduled task.",
    schema={"task_id": str},
)
async def schedule_show(args: dict, ctx: ToolContext) -> str:
    if not ctx.config_dir:
        return "Config dir not set."
    from boxagent.scheduler.cli import format_schedule_show
    return format_schedule_show(ctx.config_dir, ctx.node_id, args["task_id"])


@boxagent_tool(
    name="schedule_run",
    group="base",
    description="Trigger a scheduled task to run immediately (async — returns when accepted).",
    schema={"task_id": str},
)
async def schedule_run(args: dict, ctx: ToolContext) -> str:
    if not ctx.local_dir:
        return "Local dir not set."
    from boxagent.scheduler.cli import trigger_schedule_run
    return trigger_schedule_run(ctx.local_dir, args["task_id"])


@boxagent_tool(
    name="schedule_run_detail",
    group="base",
    description=(
        "Show full details for a specific schedule run log entry. "
        "run_index 1 is most recent, 2 is second most recent, etc."
    ),
    schema={"task_id": str, "run_index": int},
)
async def schedule_run_detail(args: dict, ctx: ToolContext) -> str:
    if not ctx.local_dir:
        return "Local dir not set."
    from boxagent.scheduler.cli import format_schedule_run_detail
    return format_schedule_run_detail(
        ctx.local_dir, args["task_id"], args.get("run_index", 1),
    )
