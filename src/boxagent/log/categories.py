"""Category constants for the BoxAgent event log.

Categories are dotted-lowercase strings forming a hierarchical namespace.
Web UI supports prefix filtering ("scheduler" matches all "scheduler.*").

Business code should prefer these constants over raw strings to avoid typos,
but raw strings are also accepted (e.g. agent-supplied via the log_event tool).
"""
from __future__ import annotations


class Category:
    SCHEDULER_RUN = "scheduler.run"
    SCHEDULER_FAIL = "scheduler.fail"
    SCHEDULER_SKIP = "scheduler.skip"

    HEARTBEAT_TICK = "workgroup.heartbeat.tick"
    HEARTBEAT_DRIVE = "workgroup.heartbeat.drive"
    HEARTBEAT_PAUSE = "workgroup.heartbeat.pause"

    BACKEND_START = "backend.start"
    BACKEND_STOP = "backend.stop"
    BACKEND_CRASH = "backend.crash"
    BACKEND_RESTART = "backend.restart"

    AGENT_NOTIFY = "agent.notify"
    AGENT_TURN = "agent.turn"
    AGENT_TOOL_CALL = "agent.tool_call"
    AGENT_TOOL_RESULT = "agent.tool_result"

    CLUSTER_PEER_UP = "cluster.peer.up"
    CLUSTER_PEER_DOWN = "cluster.peer.down"

    SYSTEM_STARTUP = "system.startup"
    SYSTEM_SHUTDOWN = "system.shutdown"

    WEB_ERROR = "web.error"
