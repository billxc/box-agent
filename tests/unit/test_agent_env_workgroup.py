"""Phase 1 (yait #98): AgentEnv 把 workgroup 裸字段归并进 WorkgroupContext。

只断言数据类形状 + property 委托，不 peek 任何私有状态。
"""

import dataclasses

from boxagent.agent_env import AgentEnv, WorkgroupContext


def test_plain_env_has_no_workgroup():
    env = AgentEnv(bot_name="b")
    assert env.workgroup is None
    assert env.is_workgroup_admin is False
    assert env.is_specialist is False
    assert env.has_peer_channel is False


def test_admin_env_properties_delegate():
    env = AgentEnv(
        bot_name="b",
        workgroup=WorkgroupContext(role="admin", has_peer_channel=True),
    )
    assert env.is_workgroup_admin is True
    assert env.is_specialist is False
    assert env.has_peer_channel is True


def test_specialist_env_properties_delegate():
    env = AgentEnv(bot_name="b", workgroup=WorkgroupContext(role="specialist"))
    assert env.is_specialist is True
    assert env.is_workgroup_admin is False
    assert env.has_peer_channel is False


def test_workgroup_context_carries_agents_tasks_peers():
    wg = WorkgroupContext(
        role="admin",
        agents=("dev-1",),
        running_tasks=({"task_id": "t1"},),
        peers=({"name": "p1"},),
    )
    env = AgentEnv(bot_name="b", workgroup=wg)
    assert env.workgroup.agents == ("dev-1",)
    assert env.workgroup.running_tasks == ({"task_id": "t1"},)
    assert env.workgroup.peers == ({"name": "p1"},)


def test_dead_and_moved_fields_removed_from_agent_env():
    names = {f.name for f in dataclasses.fields(AgentEnv)}
    assert "via_workgroup" not in names  # dead field deleted
    for moved in ("workgroup_role", "workgroup_agents", "running_tasks", "peers"):
        assert moved not in names, f"{moved} should live on WorkgroupContext now"


def test_via_workgroup_removed_from_incoming_message():
    from boxagent.transports.base import IncomingMessage

    names = {f.name for f in dataclasses.fields(IncomingMessage)}
    assert "via_workgroup" not in names
