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
CHAT_ID = os.environ.get("BOXAGENT_CHAT_ID", "")


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
def list_specialists() -> str:
    """List all specialist agents in your workgroup with their details.

    Returns each specialist's name, model, workspace, status, and whether
    it is a built-in or dynamically created specialist.
    """
    try:
        client, base_url = _get_gateway_client()
    except RuntimeError as e:
        return f"Error: {e}"

    try:
        resp = client.get(
            f"{base_url}/api/workgroup/specialists",
            params={"workgroup": BOT_NAME},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            return f"Error: {data.get('error', 'unknown error')}"

        specialists = data.get("specialists", [])
        if not specialists:
            return "No specialists found in this workgroup."

        lines = []
        for sp in specialists:
            parts = [f"**{sp['name']}**"]
            if sp.get("display_name") and sp["display_name"] != sp["name"]:
                parts.append(f"({sp['display_name']})")
            parts.append(f"— model: {sp.get('model', 'default')}")
            if sp.get("workspace"):
                parts.append(f"| workspace: {sp['workspace']}")
            if sp.get("builtin"):
                parts.append("| built-in")
            else:
                parts.append("| dynamic")
            if sp.get("running_tasks"):
                parts.append(f"| running: {', '.join(sp['running_tasks'])}")
            lines.append(" ".join(parts))

        return f"Specialists ({len(specialists)}):\n" + "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def send_to_agent(agent_name: str, message: str) -> str:
    """Dispatch a task to a specialist agent in your workgroup.

    The task is dispatched asynchronously — this tool returns immediately.
    The specialist processes the task in the background; results are visible
    in the specialist's Discord channel.

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
                "reply_chat_id": CHAT_ID,
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            task_id = data.get("task_id", "")
            return f"Task dispatched to {agent_name} (task_id: {task_id}). Check the specialist's Discord channel for progress."
        return f"Error: {data.get('error', 'unknown error')}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def create_specialist(name: str, model: str = "") -> str:
    """Dynamically create a new specialist agent in your workgroup.

    Creates a Discord channel for the specialist and starts a new AI backend.
    The specialist gets its own isolated workspace directory automatically.
    It becomes immediately available for send_to_agent calls.

    Args:
        name: Unique name for the specialist (used as channel name too)
        model: AI model to use (default: inherit from workgroup)
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
            },
            timeout=30,
        )
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


@mcp.tool()
def reset_specialist(agent_name: str) -> str:
    """Reset a specialist's session so the next task starts with a clean context.

    Use this when a specialist's conversation history has grown too large,
    or when switching to an unrelated task.

    Args:
        agent_name: Name of the specialist to reset
    """
    try:
        client, base_url = _get_gateway_client()
    except RuntimeError as e:
        return f"Error: {e}"

    try:
        resp = client.post(
            f"{base_url}/api/workgroup/reset_specialist",
            json={"name": agent_name},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            return f"Specialist '{agent_name}' session reset. Next task will start fresh."
        return f"Error: {data.get('error', 'unknown error')}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def delete_specialist(agent_name: str) -> str:
    """Delete a dynamically created specialist agent from your workgroup.

    Stops the specialist's process and pool, removes it from routing and
    persistence.  Built-in specialists (defined in config.yaml) cannot be
    deleted — only dynamically created ones can be removed.

    Args:
        agent_name: Name of the specialist to delete
    """
    try:
        client, base_url = _get_gateway_client()
    except RuntimeError as e:
        return f"Error: {e}"

    try:
        resp = client.post(
            f"{base_url}/api/workgroup/delete_specialist",
            json={"name": agent_name},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            return f"Specialist '{agent_name}' deleted."
        return f"Error: {data.get('error', 'unknown error')}"
    except Exception as e:
        return f"Error: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
