"""log_event: lets agents emit structured events into the BoxAgent log.

Categories supplied by agents are auto-prefixed with ``agent.`` if they
don't already start with it (per design Q3) — this keeps the namespace
boundary clean: system-emitted categories (scheduler.*, workgroup.*,
backend.*) cannot be forged by agent traffic.
"""
from __future__ import annotations

import logging

from boxagent.log import log
from boxagent.tools import ToolContext, boxagent_tool

logger = logging.getLogger(__name__)

_VALID_LEVELS = {"debug", "info", "warning", "error", "notify"}


@boxagent_tool(
    name="log_event",
    group="log",
    description=(
        "Record an event into the BoxAgent log. The category is auto-"
        "prefixed with 'agent.' (e.g. 'task_done' becomes 'agent.task_done'). "
        "Use this to surface notable agent activity to the operator's "
        "events feed and Telegram notifications."
    ),
    schema={
        "category": str,
        "message": str,
        "level": str,
        "meta": dict,
    },
)
async def log_event(args: dict, ctx: ToolContext) -> str:
    category = str(args.get("category") or "").strip()
    message = str(args.get("message") or "").strip()
    if not category:
        return "Error: category is required"
    if not message:
        return "Error: message is required"

    requested_level = str(args.get("level") or "info").strip().lower()
    level = requested_level if requested_level in _VALID_LEVELS else "info"

    if not category.startswith("agent."):
        category = f"agent.{category}"

    meta = args.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}

    log_meta = dict(meta)
    if ctx.bot_name:
        log_meta["bot"] = ctx.bot_name
    if ctx.chat_id:
        log_meta.setdefault("chat_id", ctx.chat_id)

    handler = getattr(log, level, log.info)
    handler(category, message, **log_meta)
    note = "" if requested_level == level else f" (invalid level '{requested_level}' → info)"
    return f"Logged event {category} (level={level}){note}"
