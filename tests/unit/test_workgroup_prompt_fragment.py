"""Phase 2 (yait #98): [Workgroup]/[Peer] prompt rendering lives in the
workgroup module, not in router/context.py.
"""

from boxagent.workgroup.prompt_fragment import build_workgroup_block


def test_empty_when_no_agents_and_no_peer_channel():
    assert build_workgroup_block() == ""
    assert build_workgroup_block(running_tasks=[{"task_id": "t1"}]) == ""


def test_workgroup_section_lists_agents_and_tool():
    block = build_workgroup_block(workgroup_agents=["dev-1", "qa-2"], running_tasks=[])
    assert "[Workgroup]" in block
    assert "- dev-1" in block
    assert "- qa-2" in block
    assert "send_to_agent MCP tool" in block
    assert "[/Workgroup]" in block


def test_peer_section_present_with_channel_no_peers():
    block = build_workgroup_block(has_peer_channel=True, peers=[])
    assert "[Peer Messaging]" in block
    assert "send_to_peer" in block
    assert "Peers:" not in block


def test_peer_list_renders_local_and_remote_and_offline():
    peers = [
        {"name": "war-room-2", "machine": "local", "online": True, "description": "local backup"},
        {"name": "mac", "machine": "macmini", "online": True, "description": "Mac Admin"},
        {"name": "old", "machine": "old-mbp", "online": False},
    ]
    block = build_workgroup_block(has_peer_channel=True, peers=peers)
    assert "- war-room-2 (local) — local backup" in block
    assert "- mac (@macmini) — Mac Admin" in block
    assert "(offline)" in block


def test_context_no_longer_defines_format_peer():
    """Regression: the peer formatter moved out of router.context."""
    import boxagent.router.context as context

    assert not hasattr(context, "_format_peer")
