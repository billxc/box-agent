"""Phase 4 (yait #98): workgroup 的 config 解析/校验逻辑在 workgroup 包内。"""

import pytest

from boxagent.config import ConfigError, WorkgroupConfig
from boxagent.workgroup.config import parse_workgroup, validate_workgroups


def test_parse_workgroup_resolves_absolute_workspace(tmp_path):
    wg = parse_workgroup(
        "war-room",
        {"workspace": str(tmp_path), "model": "opus", "ai_backend": "claude-cli"},
    )
    assert isinstance(wg, WorkgroupConfig)
    assert wg.name == "war-room"
    assert wg.workspace == str(tmp_path)
    assert wg.model == "opus"
    # WebChannel is always force-enabled for workgroup admins
    assert wg.web_enabled is True


def test_parse_workgroup_defaults_display_name_to_name():
    wg = parse_workgroup("ops", {"workspace": "/tmp/ws"})
    assert wg.display_name == "ops"


def test_validate_raises_when_enabled_workgroup_missing_workspace():
    wg = WorkgroupConfig(name="bad", workspace="")
    with pytest.raises(ConfigError, match="missing workspace"):
        validate_workgroups({"bad": wg}, node_id="")


def test_validate_skips_workgroup_not_enabled_on_node():
    # enabled only on node-A; validating on node-B must skip (no raise despite
    # the empty workspace).
    wg = WorkgroupConfig(name="bad", workspace="", enabled_on_nodes=["node-A"])
    validate_workgroups({"bad": wg}, node_id="node-B")  # no exception
