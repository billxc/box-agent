"""Telegram media-send tools.

Reachable from any bot whose context has ``has_telegram=True`` (capability
``"telegram"``). All tools share an ``_send_media`` helper that does a
multipart upload via the bot's Telegram token.
"""

from __future__ import annotations

import logging

import httpx

from boxagent.tools import ToolContext, boxagent_tool

logger = logging.getLogger(__name__)


def _resolve_telegram_token(ctx: ToolContext) -> str:
    """Look up the Telegram bot token for this context's bot."""
    gw = ctx.gateway
    if gw is None:
        return ""
    cfg = gw.config.bots.get(ctx.bot_name)
    if cfg and cfg.telegram_token:
        return cfg.telegram_token
    workgroup = gw.config.workgroups.get(ctx.bot_name)
    if workgroup:
        return gw.config.telegram_bots.get(ctx.bot_name, "")
    return ""


async def _send_media(
    method: str, field: str, file_path: str, caption: str, ctx: ToolContext,
) -> str:
    if not ctx.chat_id:
        return "Error: chat_id not set"
    token = _resolve_telegram_token(ctx)
    if not token:
        return f"Error: no Telegram token for bot '{ctx.bot_name}'"
    base_url = f"https://api.telegram.org/bot{token}"
    async with httpx.AsyncClient(timeout=60) as client:
        with open(file_path, "rb") as f:
            files = {field: f}
            data: dict[str, str] = {"chat_id": ctx.chat_id}
            if caption:
                data["caption"] = caption
            r = await client.post(f"{base_url}/{method}", data=data, files=files)
            r.raise_for_status()
    return f"Sent {field} to chat {ctx.chat_id}"


@boxagent_tool(
    name="send_photo",
    group="telegram",
    description="Send a photo/image to the user via Telegram.",
    schema={"file_path": str, "caption": str},
    requires=["telegram"],
)
async def send_photo(args: dict, ctx: ToolContext) -> str:
    return await _send_media(
        "sendPhoto", "photo",
        args["file_path"], args.get("caption", ""), ctx,
    )
