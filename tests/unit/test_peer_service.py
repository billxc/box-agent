"""Tests for PeerService — composition replacement for PeerMixin."""

from unittest.mock import MagicMock

from boxagent.cluster.peer_service import PeerService


def _make() -> PeerService:
    topo = MagicMock()
    topo.guest_registry = None
    topo.guest_client = None
    return PeerService(
        topology=topo,
        main_chat_id_provider=lambda bot: f"main-{bot}",
    )


class TestPeerServiceConstruction:
    def test_phase1_stores_topology_and_main_chat_provider(self):
        topo = MagicMock()
        ps = PeerService(topology=topo, main_chat_id_provider=lambda b: b)
        assert ps.topology is topo
        assert ps.workgroup_manager is None

    def test_phase2_set_workgroup_manager(self):
        ps = _make()
        wm = MagicMock()
        ps.set_workgroup_manager(wm)
        assert ps.workgroup_manager is wm
