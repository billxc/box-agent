#!/usr/bin/env python3
"""BoxAgent MCP server — workgroup admin tools (send_to_agent, create_specialist).

Only injected for workgroup admin agents.

Receives configuration via environment variables:
  BOXAGENT_LOCAL_DIR, BOXAGENT_BOT_NAME
"""

import os

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("boxagent-admin")

LOCAL_DIR = os.environ.get("BOXAGENT_LOCAL_DIR", "")
BOT_NAME = os.environ.get("BOXAGENT_BOT_NAME", "")


def _get_gateway_client() -> tuple[httpx.Client, str]:
    """Create an HTTP client pointing at the Gateway API.

    Returns (client, base_url).
    Raises RuntimeError if no connection method is available.
    """
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
def send_to_agent(agent_name: str, message: str) -> str:
    """Delegate a task to a specialist agent in your workgroup and wait for the response.

    The message will be sent to the specialist's Discord channel (visible to
    observers) and the specialist will process it. This tool blocks until the
    specialist completes and returns the full response text.

    Args:
        agent_name: Name of the specialist agent to delegate to
        message: The task description or question to send
    """
    try:
        client, base_url = _get_gateway_client()
    except RuntimeError as e:
        return f"Error: {e}"

    try:
        resp = client.post(
            f"{base_url}/api/workgroup/send",
            json={
                "target": agent_name,
                "message": message,
                "from": BOT_NAME,
            },
            timeout=300,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok"):
            return data.get("response", "")
        return f"Error: {data.get('error', 'unknown error')}"
    except httpx.TimeoutException:
        return f"Error: specialist '{agent_name}' timed out (5 min limit)"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def create_specialist(name: str, model: str = "", workspace: str = "") -> str:
    """Dynamically create a new specialist agent in your workgroup.

    Creates a Discord channel for the specialist and starts a new AI backend.
    The specialist becomes immediately available for send_to_agent calls.

    Args:
        name: Unique name for the specialist (used as channel name too)
        model: AI model to use (default: inherit from workgroup)
        workspace: Working directory (default: inherit from workgroup)
    """
    try:
        client, base_url = _get_gateway_client()
    except RuntimeError as e:
        return f"Error: {e}"

    wg_name = BOT_NAME
    if not wg_name:
        return "Error: BOT_NAME not set — cannot determine workgroup"

    try:
        resp = client.post(
            f"{base_url}/api/workgroup/create_specialist",
            json={
                "workgroup": wg_name,
                "name": name,
                "model": model,
                "workspace": workspace,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok"):
            ch_id = data.get("channel_id", 0)
            msg = f"Created specialist '{name}'"
            if ch_id:
                msg += f" with Discord channel (ID: {ch_id})"
            return msg
        return f"Error: {data.get('error', 'unknown error')}"
    except Exception as e:
        return f"Error: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
