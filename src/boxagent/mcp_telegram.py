#!/usr/bin/env python3
"""BoxAgent MCP server — Telegram media tools.

Launched as a subprocess by Claude CLI (via --mcp-config) or Codex CLI.
Only injected when the bot has a Telegram token.

Receives configuration via:
  1. CLI args: ``python mcp_telegram.py <bot_token> <chat_id>``
  2. Environment variables: BOXAGENT_BOT_TOKEN, BOXAGENT_CHAT_ID
"""

import os
import sys

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("boxagent-telegram")

# CLI args take priority over env vars.
if len(sys.argv) >= 3 and ":" in sys.argv[1]:
    BOT_TOKEN = sys.argv[1]
    CHAT_ID = sys.argv[2]
else:
    BOT_TOKEN = os.environ.get("BOXAGENT_BOT_TOKEN", "")
    CHAT_ID = os.environ.get("BOXAGENT_CHAT_ID", "")
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"


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


if __name__ == "__main__":
    mcp.run(transport="stdio")
