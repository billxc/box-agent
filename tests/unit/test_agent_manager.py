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
    )


class TestAgentManagerConstruction:
    def test_init_stores_infrastructure(self):
        bm = AgentManager(
            config=MagicMock(),
            config_dir=MagicMock(),
            storage=MagicMock(),
            start_time=42.0,
        )
        assert bm.start_time == 42.0
        # Manager allocates its own state dicts.
        assert bm.backends == {}
        assert bm.routers == {}
        assert bm.web_channels == {}

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
        channel = AsyncMock()
        web_channel = AsyncMock()
        backend = AsyncMock()
        backend.session_id = "sid-1"
        pool = AsyncMock()
        storage = MagicMock()

        bm = AgentManager(
            config=MagicMock(),
            config_dir=MagicMock(),
            storage=storage,
            start_time=0.0,
        )
        bm.backends["bot"] = backend
        bm.pools["bot"] = pool
        bm.channels["bot"] = channel
        bm.web_channels["bot"] = web_channel

        await bm.stop()

        channel.stop.assert_awaited_once()
        web_channel.stop.assert_awaited_once()
        backend.stop.assert_awaited_once()
        pool.stop.assert_awaited_once()
        storage.save_session.assert_called_once_with("bot", "sid-1")

    @pytest.mark.asyncio
    async def test_stop_swallows_per_resource_errors(self):
        """One bad backend.stop() must not block the rest of teardown."""
        good_channel = AsyncMock()
        bad_backend = AsyncMock()
        bad_backend.session_id = ""
        bad_backend.stop.side_effect = RuntimeError("boom")
        good_pool = AsyncMock()

        bm = AgentManager(
            config=MagicMock(),
            config_dir=MagicMock(),
            storage=MagicMock(),
            start_time=0.0,
        )
        bm.backends["bot"] = bad_backend
        bm.pools["bot"] = good_pool
        bm.channels["bot"] = good_channel

        await bm.stop()

        good_channel.stop.assert_awaited_once()
        good_pool.stop.assert_awaited_once()


class TestAgentManagerSchedulerRefs:
    def test_build_scheduler_refs_skips_raw_bot(self):
        """The synthetic ``raw`` bot is web-only and never a scheduler target."""
        config = MagicMock()
        config.bots = {
            "real": MagicMock(allowed_users=[111], ai_backend="claude-cli", telegram_token="t"),
        }
        backend = MagicMock()
        bm = AgentManager(
            config=config,
            config_dir=MagicMock(),
            storage=MagicMock(),
            start_time=0.0,
        )
        bm.backends["real"] = backend
        bm.backends["raw"] = MagicMock()
        bm.routers["real"] = MagicMock()
        bm.routers["raw"] = MagicMock()
        bm.channels["real"] = MagicMock()

        refs = bm.build_scheduler_refs()
        assert set(refs.keys()) == {"real"}
        assert refs["real"].backend is backend
        assert refs["real"].chat_id == "111"
