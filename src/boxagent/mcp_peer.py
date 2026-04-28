#!/usr/bin/env python3
"""BoxAgent MCP server — peer messaging between admin bots.

Only injected for bots with discord_peer_channel configured.

Receives configuration via environment variables:
  BOXAGENT_LOCAL_DIR, BOXAGENT_BOT_NAME
"""

import os

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("boxagent-peer")

LOCAL_DIR = os.environ.get("BOXAGENT_LOCAL_DIR", "")
BOT_NAME = os.environ.get("BOXAGENT_BOT_NAME", "")


def _get_gateway_client() -> tuple[httpx.Client, str]:
    """Create an HTTP client pointing at the Gateway API."""
    if not LOCAL_DIR:
        raise RuntimeError("BOXAGENT_LOCAL_DIR not set — cannot reach Gateway API")

    sock_path = os.path.join(LOCAL_DIR, "api.sock")
    if os.path.exists(sock_path):
        transport = httpx.HTTPTransport(uds=sock_path)
        return httpx.Client(transport=transport), "http://localhost"

    port_file = os.path.join(LOCAL_DIR, "api-port.txt")
    if os.path.exists(port_file):
        port = open(port_file).read().strip()
        return httpx.Client(), f"http://127.0.0.1:{port}"

    raise RuntimeError("Gateway API not reachable (no socket or port file)")


@mcp.tool()
def send_to_peer(target: str, message: str) -> str:
    """Send a message to another admin bot via the shared peer channel.

    The message is posted to the shared Discord peer channel. The target
    bot will receive and process it. Use this to collaborate with bots
    running on other machines.

    Args:
        target: Name of the target bot (e.g. "win-bot", "mbp-bot")
        message: The message to send
    """
    try:
        client, base_url = _get_gateway_client()
    except RuntimeError as e:
        return f"Error: {e}"

    try:
        resp = client.post(
            f"{base_url}/api/peer/send",
            json={
                "target": target,
                "message": message,
                "from": BOT_NAME,
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            return f"Message sent to {target}."
        return f"Error: {data.get('error', 'unknown error')}"
    except Exception as e:
        return f"Error: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
