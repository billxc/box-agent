"""System-prompt fragment for workgroup admins (+ peer messaging).

``router/context.py`` delegates here whenever a turn carries workgroup
context, so the core prompt builder never hardcodes workgroup/peer
wording. Deleting the workgroup module means deleting this file and the
one guarded call in ``context.py``.
"""

from __future__ import annotations


def build_workgroup_block(
    *,
    workgroup_agents: list[str] | None = None,
    running_tasks: list[dict] | None = None,
    peers: list | None = None,
    has_peer_channel: bool = False,
) -> str:
    """Render the ``[Workgroup]`` + ``[Peer Messaging]`` prompt sections.

    Returns ``""`` when there is nothing to add (no specialist agents and
    no peer channel). The leading blank line matches the spacing the core
    context builder previously produced inline.
    """
    from boxagent.workgroup.formatting import format_running_tasks

    lines: list[str] = []

    # Workgroup agent delegation info
    if workgroup_agents:
        lines.append("")
        lines.append("[Workgroup]")
        lines.append("You are the admin of a workgroup. Available specialist agents:")
        for agent_name in workgroup_agents:
            lines.append(f"- {agent_name}")

        # Running tasks status
        lines.append("")
        lines.append(format_running_tasks(running_tasks))

        lines.append("")
        lines.append(
            "Use the send_to_agent MCP tool to delegate tasks to specialists. "
            "The specialist processes your message asynchronously and returns "
            "the result via callback."
        )
        lines.append("[/Workgroup]")

    # Peer messaging info — list comes from cluster registry via Router.get_peers
    # (see Gateway._build_peer_descriptors). NOTE on guests the registry is
    # not visible, so the list will be local-only until guest→host peer-list RPC
    # lands (yait #67).
    if has_peer_channel:
        lines.append("")
        lines.append("[Peer Messaging]")
        lines.append("You can send messages to other workgroup admins using the send_to_peer MCP tool.")
        if peers:
            lines.append("Peers:")
            for peer in peers:
                lines.append(_format_peer(peer))
        lines.append("[/Peer Messaging]")

    return "\n".join(lines)


def _format_peer(peer) -> str:
    """One bullet line per peer descriptor.

    peer shape: {name, machine, online, kind, description?}
    """
    if not isinstance(peer, dict):
        return f"- {peer}"
    name = peer.get("name", "")
    machine = peer.get("machine", "")
    online = peer.get("online", True)
    description = peer.get("description", "")
    where = "local" if machine in ("", "local") else f"@{machine}"
    status = "" if online else " (offline)"
    suffix = f" — {description}" if description else ""
    return f"- {name} ({where}){status}{suffix}"
