"""Tests for AgentManager — the composition replacement for BotsMixin.

Locks the public contract: infrastructure deps in __init__, cross-manager
deps via setters (Phase 2 of the two-phase DI scheme).
"""

from unittest.mock import MagicMock

from boxagent.agent.manager import AgentManager


def _make_manager() -> AgentManager:
    return AgentManager(
        config=MagicMock(),
        config_dir=MagicMock(),
        storage=MagicMock(),
        start_time=0.0,
        backends={},
        pools={},
        routers={},
        channels={},
        web_channels={},
        watchdogs={},
        watchdog_tasks=[],
    )


class TestAgentManagerConstruction:
    def test_init_stores_infrastructure(self):
        backends: dict = {}
        bm = AgentManager(
            config=MagicMock(),
            config_dir=MagicMock(),
            storage=MagicMock(),
            start_time=42.0,
            backends=backends,
            pools={},
            routers={},
            channels={},
            web_channels={},
            watchdogs={},
            watchdog_tasks=[],
        )
        # Shared dicts are kept by reference so other managers see the writes.
        assert bm.backends is backends
        assert bm.start_time == 42.0

    def test_scheduler_is_none_until_phase2_setter(self):
        bm = _make_manager()
        assert bm.scheduler is None


class TestAgentManagerPhase2:
    def test_set_scheduler_injects_dep(self):
        bm = _make_manager()
        sched = MagicMock()
        bm.set_scheduler(sched)
        assert bm.scheduler is sched
