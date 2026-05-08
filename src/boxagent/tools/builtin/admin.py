"""Workgroup admin tools — manage specialists, dispatch work, monitor.

All visible only to workgroup admins (``requires=['workgroup_admin']``).
The handlers wrap ``WorkgroupManager`` methods on Gateway.
"""

from __future__ import annotations

import logging
from typing import Any

from boxagent.tools import ToolContext, boxagent_tool

logger = logging.getLogger(__name__)


def _wg(ctx: ToolContext):
    """Resolve the WorkgroupManager or return a tuple (None, error_str)."""
    if ctx.gateway is None:
        return None, "Error: gateway not available"
    mgr = getattr(ctx.gateway, "_workgroup_mgr", None)
    if mgr is None:
        return None, "Error: workgroup manager not available"
    return mgr, ""


@boxagent_tool(
    name="list_specialists",
    group="admin",
    description=(
        "List all specialist agents in your workgroup with name, model, "
        "workspace, template, and current running tasks."
    ),
    schema={},
    requires=["workgroup_admin"],
)
async def list_specialists(args: dict, ctx: ToolContext) -> str:
    mgr, err = _wg(ctx)
    if mgr is None:
        return err
    result = mgr.list_specialists(ctx.bot_name)
    if not result.get("ok"):
        return f"Error: {result.get('error', 'unknown error')}"
    specialists = result.get("specialists", [])
    if not specialists:
        return "No specialists found in this workgroup."
    lines = []
    for s in specialists:
        parts = [f"**{s['name']}**"]
        if s.get("display_name") and s["display_name"] != s["name"]:
            parts.append(f"({s['display_name']})")
        parts.append(f"— model: {s.get('model', 'default')}")
        if s.get("template"):
            parts.append(f"| template: {s['template']}")
        if s.get("workspace"):
            parts.append(f"| workspace: {s['workspace']}")
        if s.get("running_tasks"):
            parts.append(f"| running: {', '.join(s['running_tasks'])}")
        lines.append(" ".join(parts))
    return f"Specialists ({len(specialists)}):\n" + "\n".join(lines)


@boxagent_tool(
    name="list_templates",
    group="admin",
    description=(
        "List specialist templates available in your workgroup. Templates "
        "are pre-configured roles you can pass to create_specialist via "
        "the 'template' argument; each ships a CLAUDE.md and may bundle "
        "skills."
    ),
    schema={},
    requires=["workgroup_admin"],
)
async def list_templates(args: dict, ctx: ToolContext) -> str:
    mgr, err = _wg(ctx)
    if mgr is None:
        return err
    if not ctx.bot_name:
        return "Error: bot_name not set — cannot determine workgroup"
    result = mgr.list_templates(ctx.bot_name)
    if not result.get("ok"):
        return f"Error: {result.get('error', 'unknown error')}"
    templates = result.get("templates", [])
    if not templates:
        return "No templates available."
    lines = ["Available templates:"]
    for t in templates:
        lines.append(f"- {t['name']}: {t['description']}")
    return "\n".join(lines)


@boxagent_tool(
    name="get_specialist_status",
    group="admin",
    description=(
        "Get a specialist's current status, recent tasks, and chat history."
    ),
    schema={"agent_name": str},
    requires=["workgroup_admin"],
)
async def get_specialist_status(args: dict, ctx: ToolContext) -> str:
    mgr, err = _wg(ctx)
    if mgr is None:
        return err
    agent_name = args["agent_name"]
    result = mgr.get_specialist_status(agent_name)
    if not result.get("ok"):
        return f"Error: {result.get('error', 'unknown error')}"
    lines = [f"**{agent_name}** — {'active' if result.get('active') else 'idle'}"]
    tasks = result.get("tasks", [])
    if tasks:
        lines.append(f"\nTasks ({len(tasks)}):")
        for t in tasks[-5:]:
            status = t.get("status", "?")
            tid = t.get("task_id", "?")
            preview = t.get("result_preview", "")
            error = t.get("error", "")
            line = f"  - {tid}: {status}"
            if preview:
                line += f" — {preview[:100]}"
            if error:
                line += f" — ERROR: {error[:100]}"
            lines.append(line)
    chat = result.get("recent_chat", [])
    if chat:
        lines.append(f"\nRecent chat ({len(chat)} lines):")
        for c in chat:
            lines.append(f"  {c}")
    return "\n".join(lines)


@boxagent_tool(
    name="send_to_specialist",
    group="admin",
    description=(
        "Dispatch a task to a specialist agent in your workgroup. "
        "Asynchronous — returns immediately with a task_id; the "
        "specialist processes the task in the background. Results are "
        "visible in the specialist's web view and via "
        "get_specialist_status."
    ),
    schema={"agent_name": str, "message": str},
    requires=["workgroup_admin"],
)
async def send_to_specialist(args: dict, ctx: ToolContext) -> str:
    mgr, err = _wg(ctx)
    if mgr is None:
        return err
    try:
        result = await mgr.send_to_specialist(
            target=args["agent_name"],
            text=args["message"],
            from_bot=ctx.bot_name,
            reply_chat_id=ctx.chat_id,
        )
        if result.get("ok"):
            task_id = result.get("task_id", "")
            return f"Task dispatched to {args['agent_name']} (task_id: {task_id})."
        return f"Error: {result.get('error', 'unknown error')}"
    except Exception as e:
        return f"Error: {e}"


@boxagent_tool(
    name="create_specialist",
    group="admin",
    description=(
        "Dynamically create a new specialist agent in your workgroup. "
        "Spawns a new AI backend in its own isolated workspace. The "
        "specialist is reachable at chat_id 'wg:<name>'. Pass 'template' "
        "(see list_templates) to inherit a pre-configured role."
    ),
    schema={
        "name": str, "model": str, "template": str,
        "extra_skill_dirs": list, "display_name": str,
    },
    requires=["workgroup_admin"],
)
async def create_specialist(args: dict, ctx: ToolContext) -> str:
    mgr, err = _wg(ctx)
    if mgr is None:
        return err
    if not ctx.bot_name:
        return "Error: bot_name not set — cannot determine workgroup"
    name = args["name"]
    template = args.get("template", "")
    try:
        result = await mgr.create_specialist(
            ctx.bot_name, name,
            model=args.get("model", ""),
            template=template,
            extra_skill_dirs=args.get("extra_skill_dirs"),
            display_name=args.get("display_name", ""),
        )
        if result.get("ok"):
            msg = f"Created specialist '{name}'"
            if template:
                msg += f" from template '{template}'"
            chat_id = result.get("chat_id", "")
            if chat_id:
                msg += f" (chat_id: {chat_id})"
            return msg
        return f"Error: {result.get('error', 'unknown error')}"
    except Exception as e:
        return f"Error: {e}"


@boxagent_tool(
    name="reset_specialist",
    group="admin",
    description="Reset a specialist's session so the next task starts with a clean context.",
    schema={"agent_name": str},
    requires=["workgroup_admin"],
)
async def reset_specialist(args: dict, ctx: ToolContext) -> str:
    mgr, err = _wg(ctx)
    if mgr is None:
        return err
    result = mgr.reset_specialist(args["agent_name"])
    if result.get("ok"):
        return f"Specialist '{args['agent_name']}' session reset. Next task will start fresh."
    return f"Error: {result.get('error', 'unknown error')}"


@boxagent_tool(
    name="delete_specialist",
    group="admin",
    description="Delete a dynamically created specialist agent from your workgroup.",
    schema={"agent_name": str},
    requires=["workgroup_admin"],
)
async def delete_specialist(args: dict, ctx: ToolContext) -> str:
    mgr, err = _wg(ctx)
    if mgr is None:
        return err
    try:
        result = await mgr.delete_specialist(args["agent_name"])
        if result.get("ok"):
            return f"Specialist '{args['agent_name']}' deleted."
        return f"Error: {result.get('error', 'unknown error')}"
    except Exception as e:
        return f"Error: {e}"


@boxagent_tool(
    name="cancel_task",
    group="admin",
    description="Cancel a running specialist task by task_id.",
    schema={"task_id": str},
    requires=["workgroup_admin"],
)
async def cancel_task(args: dict, ctx: ToolContext) -> str:
    mgr, err = _wg(ctx)
    if mgr is None:
        return err
    try:
        result = await mgr.cancel_task(args["task_id"])
        if result.get("ok"):
            return f"Task '{args['task_id']}' cancelled."
        return f"Error: {result.get('error', 'unknown error')}"
    except Exception as e:
        return f"Error: {e}"
