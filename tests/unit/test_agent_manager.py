"""Tests for AgentManager — the composition replacement for BotsMixin.

Locks the public contract: infrastructure deps in __init__, cross-manager
deps via setters (Phase 2 of the two-phase DI scheme).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from boxagent.agent.agent_manager import AgentManager


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


class TestAgentManagerStop:
    @pytest.mark.asyncio
    async def test_stop_tears_down_owned_resources(self):
        """stop() walks every dict it owns and calls .stop() on each, plus
        cancels watchdog tasks. Errors are logged not raised."""
        ch = AsyncMock()
        web_ch = AsyncMock()
        backend = AsyncMock()
        backend.session_id = "sid-1"
        pool = AsyncMock()
        storage = MagicMock()

        bm = AgentManager(
            config=MagicMock(),
            config_dir=MagicMock(),
            storage=storage,
            start_time=0.0,
            backends={"bot": backend},
            pools={"bot": pool},
            routers={},
            channels={"bot": ch},
            web_channels={"bot": web_ch},
            watchdogs={},
            watchdog_tasks=[],
        )
        await bm.stop()

        ch.stop.assert_awaited_once()
        web_ch.stop.assert_awaited_once()
        backend.stop.assert_awaited_once()
        pool.stop.assert_awaited_once()
        # session saved before backend stop
        storage.save_session.assert_called_once_with("bot", "sid-1")

    @pytest.mark.asyncio
    async def test_stop_swallows_per_resource_errors(self):
        """One bad backend.stop() must not block the rest of teardown."""
        good_ch = AsyncMock()
        bad_backend = AsyncMock()
        bad_backend.session_id = ""
        bad_backend.stop.side_effect = RuntimeError("boom")
        good_pool = AsyncMock()

        bm = AgentManager(
            config=MagicMock(),
            config_dir=MagicMock(),
            storage=MagicMock(),
            start_time=0.0,
            backends={"bot": bad_backend},
            pools={"bot": good_pool},
            routers={},
            channels={"bot": good_ch},
            web_channels={},
            watchdogs={},
            watchdog_tasks=[],
        )
        await bm.stop()

        good_ch.stop.assert_awaited_once()
        good_pool.stop.assert_awaited_once()


class TestAgentManagerSchedulerRefs:
    def test_build_scheduler_refs_skips_raw_bot(self):
        """The synthetic ``raw`` bot is web-only and never a scheduler target."""
        cfg = MagicMock()
        cfg.bots = {
            "real": MagicMock(allowed_users=[111], ai_backend="claude-cli", telegram_token="t"),
        }
        backend = MagicMock()
        bm = AgentManager(
            config=cfg,
            config_dir=MagicMock(),
            storage=MagicMock(),
            start_time=0.0,
            backends={"real": backend, "raw": MagicMock()},
            pools={},
            routers={"real": MagicMock(), "raw": MagicMock()},
            channels={"real": MagicMock()},
            web_channels={},
            watchdogs={},
            watchdog_tasks=[],
        )
        refs = bm.build_scheduler_refs()
        assert set(refs.keys()) == {"real"}
        assert refs["real"].backend is backend
        assert refs["real"].chat_id == "111"
