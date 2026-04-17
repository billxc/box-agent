#!/usr/bin/env python3
"""BoxAgent MCP server — sends media to Telegram via Bot API,
and exposes schedule/session management tools.

Launched as a subprocess by Claude CLI (via --mcp-config) or Codex CLI
(via -c mcp_servers.*).

Receives configuration via:
  1. CLI args: ``python mcp_server.py <bot_token> <chat_id>``
  2. Environment variables: BOXAGENT_BOT_TOKEN, BOXAGENT_CHAT_ID,
     BOXAGENT_CONFIG_DIR, BOXAGENT_LOCAL_DIR, BOXAGENT_NODE_ID
"""

import os
import sys

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("boxagent-telegram")

# CLI args take priority over env vars.
# Usage: python mcp_server.py <bot_token> <chat_id>
# The token always starts with a digit sequence + colon, so we can
# distinguish real args from pytest/other launcher args.
if len(sys.argv) >= 3 and ":" in sys.argv[1]:
    BOT_TOKEN = sys.argv[1]
    CHAT_ID = sys.argv[2]
else:
    BOT_TOKEN = os.environ.get("BOXAGENT_BOT_TOKEN", "")
    CHAT_ID = os.environ.get("BOXAGENT_CHAT_ID", "")
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

CONFIG_DIR = os.environ.get("BOXAGENT_CONFIG_DIR", "")
LOCAL_DIR = os.environ.get("BOXAGENT_LOCAL_DIR", "")
NODE_ID = os.environ.get("BOXAGENT_NODE_ID", "")


def _send_media(
    method: str, field: str, file_path: str, caption: str = ""
) -> str:
    """Upload a file to Telegram via Bot API multipart POST."""
    with open(file_path, "rb") as f:
        files = {field: f}
        data: dict[str, str] = {"chat_id": CHAT_ID}
        if caption:
            data["caption"] = caption
        r = httpx.post(
            f"{BASE_URL}/{method}", data=data, files=files, timeout=60
        )
        r.raise_for_status()
    return f"Sent {field} to chat {CHAT_ID}"


@mcp.tool()
def send_photo(file_path: str, caption: str = "") -> str:
    """Send a photo/image to the user via Telegram.

    Args:
        file_path: Absolute path to the image file (jpg, png, etc.)
        caption: Optional caption text
    """
    return _send_media("sendPhoto", "photo", file_path, caption)


@mcp.tool()
def send_document(file_path: str, caption: str = "") -> str:
    """Send a file/document to the user via Telegram.

    Args:
        file_path: Absolute path to the file
        caption: Optional caption text
    """
    return _send_media("sendDocument", "document", file_path, caption)


@mcp.tool()
def send_video(file_path: str, caption: str = "") -> str:
    """Send a video to the user via Telegram.

    Args:
        file_path: Absolute path to the video file (mp4, etc.)
        caption: Optional caption text
    """
    return _send_media("sendVideo", "video", file_path, caption)


@mcp.tool()
def send_audio(file_path: str, caption: str = "") -> str:
    """Send an audio file to the user via Telegram.

    Args:
        file_path: Absolute path to the audio file (mp3, ogg, etc.)
        caption: Optional caption text
    """
    return _send_media("sendAudio", "audio", file_path, caption)


@mcp.tool()
def send_animation(file_path: str, caption: str = "") -> str:
    """Send a GIF animation to the user via Telegram.

    Args:
        file_path: Absolute path to the GIF file
        caption: Optional caption text
    """
    return _send_media("sendAnimation", "animation", file_path, caption)


# ---- Schedule / Sessions tools ----


@mcp.tool()
def schedule_list() -> str:
    """List all configured scheduled tasks with their cron, mode, and prompt."""
    if not CONFIG_DIR:
        return "BOXAGENT_CONFIG_DIR not set."
    from boxagent.schedule_cli import format_schedule_list

    return format_schedule_list(CONFIG_DIR, NODE_ID)


@mcp.tool()
def schedule_add(
    task_id: str,
    cron: str,
    prompt: str,
    mode: str = "isolate",
    bot: str = "",
    ai_backend: str = "",
    model: str = "",
) -> str:
    """Add a new scheduled task.

    Args:
        task_id: Unique task ID
        cron: Cron expression (5-field, e.g. "0 9 * * 1-5")
        prompt: Prompt to send when the schedule fires
        mode: Execution mode - "isolate" (standalone) or "append" (send to bot)
        bot: Bot name (required when mode=append)
        ai_backend: Backend for isolate mode (claude-cli, codex-cli, codex-acp)
        model: Model for isolate mode (e.g. sonnet, opus)
    """
    if not CONFIG_DIR:
        return "BOXAGENT_CONFIG_DIR not set."
    from boxagent.schedule_cli import add_schedule as _add

    return _add(
        config_dir=CONFIG_DIR,
        task_id=task_id,
        cron=cron,
        prompt=prompt,
        mode=mode,
        bot=bot,
        ai_backend=ai_backend,
        model=model,
    )


@mcp.tool()
def schedule_logs(task_id: str = "") -> str:
    """Show recent schedule execution logs.

    Args:
        task_id: Optional task ID to filter logs for a specific schedule
    """
    if not LOCAL_DIR:
        return "BOXAGENT_LOCAL_DIR not set."
    from boxagent.schedule_cli import format_schedule_logs

    return format_schedule_logs(LOCAL_DIR, task_id=task_id)


@mcp.tool()
def schedule_show(task_id: str) -> str:
    """Show detailed configuration for a specific scheduled task.

    Args:
        task_id: The schedule task ID to show
    """
    if not CONFIG_DIR:
        return "BOXAGENT_CONFIG_DIR not set."
    from boxagent.schedule_cli import format_schedule_show

    return format_schedule_show(CONFIG_DIR, NODE_ID, task_id)


@mcp.tool()
def schedule_run(task_id: str) -> str:
    """Trigger a scheduled task to run immediately (async).

    Args:
        task_id: The schedule task ID to run
    """
    if not LOCAL_DIR:
        return "BOXAGENT_LOCAL_DIR not set."
    from boxagent.schedule_cli import trigger_schedule_run

    return trigger_schedule_run(LOCAL_DIR, task_id)


@mcp.tool()
def sessions_list(project_filter: str = "") -> str:
    """List Claude CLI sessions from ~/.claude/projects/.

    Args:
        project_filter: Optional substring to filter by project path
    """
    from boxagent.sessions_cli import format_sessions_list

    return format_sessions_list(project_filter=project_filter)


if __name__ == "__main__":
    mcp.run(transport="stdio")
