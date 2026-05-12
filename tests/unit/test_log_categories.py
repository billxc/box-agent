"""Test Category constant values: stable strings, dotted-lowercase convention."""
from __future__ import annotations

import re

from boxagent.log import Category


def test_category_constants_are_dotted_lowercase():
    pattern = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    for name in dir(Category):
        if name.startswith("_"):
            continue
        value = getattr(Category, name)
        if not isinstance(value, str):
            continue
        assert pattern.match(value), f"{name}={value!r} violates dotted-lowercase"


def test_known_category_values_are_stable():
    assert Category.SCHEDULER_RUN == "scheduler.run"
    assert Category.SCHEDULER_FAIL == "scheduler.fail"
    assert Category.SCHEDULER_SKIP == "scheduler.skip"
    assert Category.HEARTBEAT_TICK == "workgroup.heartbeat.tick"
    assert Category.HEARTBEAT_DRIVE == "workgroup.heartbeat.drive"
    assert Category.HEARTBEAT_PAUSE == "workgroup.heartbeat.pause"
    assert Category.BACKEND_START == "backend.start"
    assert Category.BACKEND_STOP == "backend.stop"
    assert Category.BACKEND_CRASH == "backend.crash"
    assert Category.BACKEND_RESTART == "backend.restart"
    assert Category.AGENT_NOTIFY == "agent.notify"
    assert Category.AGENT_TURN == "agent.turn"
    assert Category.AGENT_TOOL_CALL == "agent.tool_call"
    assert Category.AGENT_TOOL_RESULT == "agent.tool_result"
    assert Category.CLUSTER_PEER_UP == "cluster.peer.up"
    assert Category.CLUSTER_PEER_DOWN == "cluster.peer.down"
    assert Category.SYSTEM_STARTUP == "system.startup"
    assert Category.SYSTEM_SHUTDOWN == "system.shutdown"
