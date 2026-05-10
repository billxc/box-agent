"""Tests for TopologyService — composition replacement for TopologyMixin.

Locks the public contract: config/web_channels in __init__, host_election
+ workgroup_manager injected via setters (Phase-2 DI).
"""

from unittest.mock import MagicMock

from boxagent.cluster.topology_service import TopologyService


def _make() -> TopologyService:
    config = MagicMock()
    config.machine_id = "m-A"
    config.node_id = ""
    config.cluster_tunnel = False
    return TopologyService(config=config, web_channels={})


class TestTopologyServiceConstruction:
    def test_init_stores_infra(self):
        config = MagicMock()
        config.machine_id = "m-A"
        web_channels: dict = {}
        ts = TopologyService(config=config, web_channels=web_channels)
        assert ts.config is config
        assert ts.web_channels is web_channels

    def test_phase2_deps_default_none(self):
        ts = _make()
        assert ts.host_election is None
        assert ts.workgroup_manager is None


class TestTopologyServicePhase2:
    def test_set_host_election(self):
        ts = _make()
        he = MagicMock()
        ts.set_host_election(he)
        assert ts.host_election is he

    def test_set_workgroup_manager(self):
        ts = _make()
        wm = MagicMock()
        ts.set_workgroup_manager(wm)
        assert ts.workgroup_manager is wm


class TestTopologyServiceLocalIdentity:
    def test_local_machine_id_uses_machine_id_first(self):
        config = MagicMock()
        config.machine_id = "machine-A"
        config.node_id = "node-X"
        ts = TopologyService(config=config, web_channels={})
        assert ts.local_machine_id() == "machine-A"

    def test_local_machine_id_falls_back_to_node_id(self):
        config = MagicMock()
        config.machine_id = ""
        config.node_id = "node-X"
        ts = TopologyService(config=config, web_channels={})
        assert ts.local_machine_id() == "node-X"

    def test_local_role_single_when_no_election_no_tunnel(self):
        config = MagicMock()
        config.machine_id = ""
        config.node_id = ""
        config.cluster_tunnel = False
        ts = TopologyService(config=config, web_channels={})
        assert ts.local_role() == "single"
