"""Cross-admin peer messaging tool.

Visible only to workgroup admins with peer_channel capability.
"""

from __future__ import annotations

import asyncio
import logging

from boxagent.tools import ToolContext, boxagent_tool

logger = logging.getLogger(__name__)


@boxagent_tool(
    name="send_to_peer",
    group="peer",
    description=(
        "Send a message to another workgroup admin. Routes via cluster "
        "RPC: in-process if the target lives on this machine, otherwise "
        "over guest WS. Default is fire-and-forget (returns 'queued' "
        "immediately). Pass wait=true to wait for the send chain to "
        "succeed (still NOT for the target to reply)."
    ),
    schema={"target": str, "message": str, "wait": bool},
    requires=["peer_channel"],
)
async def send_to_peer(args: dict, ctx: ToolContext) -> str:
    if ctx.gateway is None:
        return "Error: gateway not available"
    if not ctx.bot_name:
        return "Error: bot_name not set"

    target = args["target"]
    message = args["message"]
    wait = bool(args.get("wait", False))

    if not wait:
        asyncio.create_task(
            ctx.gateway.send_peer(target, ctx.bot_name, message),
            name=f"send_to_peer:{target}",
        )
        return (
            f"Queued message to {target} "
            f"(fire-and-forget; pass wait=true for confirmation)."
        )

    result = await ctx.gateway.send_peer(target, ctx.bot_name, message)
    if result.get("ok"):
        via = result.get("via", "?")
        return f"Message sent to {target} (via {via})."
    return f"Error: {result.get('error', 'unknown')}"
