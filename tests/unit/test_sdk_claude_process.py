"""Smoke tests for AgentSDKClaude backend."""

import pytest

from boxagent.agent.protocol import AgentBackend


class TestAgentSDKClaude:
    def test_satisfies_protocol(self):
        from boxagent.agent.sdk_claude_process import AgentSDKClaude
        backend = AgentSDKClaude(workspace="/tmp", bot_name="t")
        assert isinstance(backend, AgentBackend)

    def test_initial_state(self):
        from boxagent.agent.sdk_claude_process import AgentSDKClaude
        backend = AgentSDKClaude(workspace="/tmp/work", model="sonnet", bot_name="t")
        assert backend.state == "idle"
        assert backend.session_id is None
        assert backend.supports_session_persistence is True
        assert backend.last_turn_failed is False
        assert backend.last_turn_error == ""

    def test_start_is_noop(self):
        from boxagent.agent.sdk_claude_process import AgentSDKClaude
        backend = AgentSDKClaude(workspace="/tmp", bot_name="t")
        # Should not raise; SDK has no subprocess to start.
        backend.start()
        assert backend.state == "idle"

    @pytest.mark.asyncio
    async def test_cancel_when_idle(self):
        from boxagent.agent.sdk_claude_process import AgentSDKClaude
        backend = AgentSDKClaude(workspace="/tmp", bot_name="t")
        await backend.cancel()
        assert backend.state == "idle"

    @pytest.mark.asyncio
    async def test_reset_session_clears_id(self):
        from boxagent.agent.sdk_claude_process import AgentSDKClaude
        backend = AgentSDKClaude(workspace="/tmp", bot_name="t", session_id="abc-123")
        await backend.reset_session()
        assert backend.session_id is None

    @pytest.mark.asyncio
    async def test_stop_marks_dead(self):
        from boxagent.agent.sdk_claude_process import AgentSDKClaude
        backend = AgentSDKClaude(workspace="/tmp", bot_name="t")
        backend.start()
        await backend.stop()
        assert backend.state == "dead"

    def test_build_options_passes_through(self):
        from boxagent.agent.sdk_claude_process import AgentSDKClaude
        backend = AgentSDKClaude(
            workspace="/tmp/work", model="sonnet",
            bot_name="t", session_id="s-1", yolo=True,
        )
        opts = backend._build_options(model="opus", append_system_prompt="extra")
        assert opts.cwd == "/tmp/work"
        assert opts.model == "opus"
        assert opts.resume == "s-1"
        assert opts.permission_mode == "bypassPermissions"
        assert opts.system_prompt == {
            "type": "preset",
            "preset": "claude_code",
            "append": "extra",
        }


class TestFactoryWiring:
    def test_create_backend_dispatches_to_sdk_claude(self):
        from boxagent.agent.manager import _create_backend
        from boxagent.agent.sdk_claude_process import AgentSDKClaude
        from boxagent.config import BotConfig

        cfg = BotConfig(
            name="t", ai_backend="agent-sdk-claude",
            workspace="/tmp", model="sonnet",
        )
        backend = _create_backend(cfg, session_id="s-9")
        assert isinstance(backend, AgentSDKClaude)
        assert backend.session_id == "s-9"
        assert backend.workspace == "/tmp"
        assert backend.model == "sonnet"

    def test_supports_persistent_session_includes_sdk_claude(self):
        from boxagent.agent.manager import _supports_persistent_session
        assert _supports_persistent_session("agent-sdk-claude") is True
