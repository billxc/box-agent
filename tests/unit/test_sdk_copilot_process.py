"""Smoke tests for AgentSDKCopilot backend."""

import pytest

from boxagent.agent.protocol import AgentBackend


class TestAgentSDKCopilot:
    def test_satisfies_protocol(self):
        from boxagent.agent.sdk_copilot_process import AgentSDKCopilot
        backend = AgentSDKCopilot(workspace="/tmp", bot_name="t")
        assert isinstance(backend, AgentBackend)

    def test_initial_state(self):
        from boxagent.agent.sdk_copilot_process import AgentSDKCopilot
        backend = AgentSDKCopilot(workspace="/tmp/work", model="gpt-5", bot_name="t")
        assert backend.state == "idle"
        assert backend.session_id is None
        assert backend.supports_session_persistence is True
        assert backend.last_turn_failed is False
        assert backend.last_turn_error == ""

    def test_start_is_sync_marker(self):
        from boxagent.agent.sdk_copilot_process import AgentSDKCopilot
        backend = AgentSDKCopilot(workspace="/tmp", bot_name="t")
        backend.start()
        assert backend._started is True
        # Real client/session not yet created — that happens on first send.
        assert backend._client is None
        assert backend._session is None

    @pytest.mark.asyncio
    async def test_cancel_when_idle_no_session(self):
        from boxagent.agent.sdk_copilot_process import AgentSDKCopilot
        backend = AgentSDKCopilot(workspace="/tmp", bot_name="t")
        # Should not raise even without an active session.
        await backend.cancel()
        assert backend.state == "idle"

    @pytest.mark.asyncio
    async def test_reset_session_clears_id(self):
        from boxagent.agent.sdk_copilot_process import AgentSDKCopilot
        backend = AgentSDKCopilot(workspace="/tmp", bot_name="t", session_id="abc-1")
        await backend.reset_session()
        assert backend.session_id is None

    @pytest.mark.asyncio
    async def test_stop_marks_dead_without_started_client(self):
        from boxagent.agent.sdk_copilot_process import AgentSDKCopilot
        backend = AgentSDKCopilot(workspace="/tmp", bot_name="t")
        backend.start()
        await backend.stop()
        assert backend.state == "dead"


class TestFactoryWiring:
    def test_create_backend_dispatches_to_sdk_copilot(self):
        from boxagent.agent.manager import _create_backend
        from boxagent.agent.sdk_copilot_process import AgentSDKCopilot
        from boxagent.config import BotConfig

        cfg = BotConfig(
            name="t", ai_backend="agent-sdk-copilot",
            workspace="/tmp", model="gpt-5",
        )
        backend = _create_backend(cfg, session_id="s-9")
        assert isinstance(backend, AgentSDKCopilot)
        assert backend.session_id == "s-9"
        assert backend.workspace == "/tmp"
        assert backend.model == "gpt-5"

    def test_supports_persistent_session_includes_sdk_copilot(self):
        from boxagent.agent.manager import _supports_persistent_session
        assert _supports_persistent_session("agent-sdk-copilot") is True

    def test_router_recognises_kind(self):
        from boxagent.router.core import Router
        assert "agent-sdk-copilot" in Router._VALID_BACKENDS

    def test_config_validator_accepts_kind(self, tmp_path):
        from textwrap import dedent
        from boxagent.config import load_config
        config = dedent("""\
            global: {}
            bots:
              cop:
                ai_backend: agent-sdk-copilot
                workspace: /tmp/cop
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)
        cfg = load_config(tmp_path)
        assert cfg.bots["cop"].ai_backend == "agent-sdk-copilot"
