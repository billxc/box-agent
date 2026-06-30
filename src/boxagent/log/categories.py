"""Category constants for the BoxAgent event log.

Categories are dotted-lowercase strings forming a hierarchical namespace.
Web UI supports prefix filtering ("scheduler" matches all "scheduler.*").

Business code should prefer these constants over raw strings to avoid typos,
but raw strings are also accepted (e.g. agent-supplied via the log_event tool).
"""
from __future__ import annotations


class Category:
    SCHEDULER_RUN = "scheduler.run"
    SCHEDULER_DONE = "scheduler.done"
    SCHEDULER_FAIL = "scheduler.fail"
    SCHEDULER_SKIP = "scheduler.skip"

    BACKEND_START = "backend.start"
    BACKEND_STOP = "backend.stop"
    BACKEND_CRASH = "backend.crash"
    BACKEND_RESTART = "backend.restart"

    AGENT_NOTIFY = "agent.notify"
    AGENT_TURN = "agent.turn"
    AGENT_TOOL_CALL = "agent.tool_call"
    AGENT_TOOL_RESULT = "agent.tool_result"
    AGENT_TOOL_ERROR = "agent.tool_error"

    CLUSTER_PEER_UP = "cluster.peer.up"
    CLUSTER_PEER_DOWN = "cluster.peer.down"

    CLUSTER_HOST_ELECTED = "cluster.host.elected"
    CLUSTER_HOST_DEMOTED = "cluster.host.demoted"
    CLUSTER_HOST_PROBE_FAIL = "cluster.host.probe_fail"
    CLUSTER_HOST_RPC_FAIL = "cluster.host.rpc_fail"

    CLUSTER_TUNNEL_UP = "cluster.tunnel.up"
    CLUSTER_TUNNEL_DOWN = "cluster.tunnel.down"
    CLUSTER_TUNNEL_ERROR = "cluster.tunnel.error"

    CLUSTER_GUEST_JOINED = "cluster.guest.joined"
    CLUSTER_GUEST_LEFT = "cluster.guest.left"
    CLUSTER_GUEST_CONNECTED = "cluster.guest.connected"
    CLUSTER_GUEST_DISCONNECTED = "cluster.guest.disconnected"
    CLUSTER_GUEST_RPC_FAIL = "cluster.guest.rpc_fail"

    CLUSTER_PROTOCOL_ERROR = "cluster.protocol.error"
    CLUSTER_TOPOLOGY_PUSH_FAIL = "cluster.topology.push_fail"

    SYSTEM_STARTUP = "system.startup"
    SYSTEM_SHUTDOWN = "system.shutdown"

    WEB_ERROR = "web.error"
