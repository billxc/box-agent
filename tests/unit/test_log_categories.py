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
    assert Category.CLUSTER_HOST_ELECTED == "cluster.host.elected"
    assert Category.CLUSTER_HOST_DEMOTED == "cluster.host.demoted"
    assert Category.CLUSTER_HOST_PROBE_FAIL == "cluster.host.probe_fail"
    assert Category.CLUSTER_HOST_RPC_FAIL == "cluster.host.rpc_fail"
    assert Category.CLUSTER_TUNNEL_UP == "cluster.tunnel.up"
    assert Category.CLUSTER_TUNNEL_DOWN == "cluster.tunnel.down"
    assert Category.CLUSTER_TUNNEL_ERROR == "cluster.tunnel.error"
    assert Category.CLUSTER_GUEST_JOINED == "cluster.guest.joined"
    assert Category.CLUSTER_GUEST_LEFT == "cluster.guest.left"
    assert Category.CLUSTER_GUEST_CONNECTED == "cluster.guest.connected"
    assert Category.CLUSTER_GUEST_DISCONNECTED == "cluster.guest.disconnected"
    assert Category.CLUSTER_GUEST_RPC_FAIL == "cluster.guest.rpc_fail"
    assert Category.CLUSTER_PROTOCOL_ERROR == "cluster.protocol.error"
    assert Category.CLUSTER_TOPOLOGY_PUSH_FAIL == "cluster.topology.push_fail"
    assert Category.SYSTEM_STARTUP == "system.startup"
    assert Category.SYSTEM_SHUTDOWN == "system.shutdown"
