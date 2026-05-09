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
        from boxagent.agent.backend_factory import create_backend
        from boxagent.agent.sdk_copilot_process import AgentSDKCopilot
        from boxagent.config import BotConfig

        cfg = BotConfig(
            name="t", ai_backend="agent-sdk-copilot",
            workspace="/tmp", model="gpt-5",
        )
        backend = create_backend(cfg, session_id="s-9")
        assert isinstance(backend, AgentSDKCopilot)
        assert backend.session_id == "s-9"
        assert backend.workspace == "/tmp"
        assert backend.model == "gpt-5"

    def test_supports_persistent_session_includes_sdk_copilot(self):
        from boxagent.agent.agent_manager import _supports_persistent_session
        assert _supports_persistent_session("agent-sdk-copilot") is True

    def test_router_recognises_kind(self):
        from boxagent.agent.protocol import BACKEND_KINDS
        assert "agent-sdk-copilot" in BACKEND_KINDS

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


class TestSharedClient:
    """The CopilotClient subprocess is heavy (~7s spawn, dozens of MB).
    All AgentSDKCopilot instances share one via refcount-tracked acquire/release."""

    def setup_method(self):
        # Reset module globals between tests to keep refcount sane.
        import boxagent.agent.sdk_copilot_process as mod
        mod._SHARED_CLIENT = None
        mod._SHARED_REFCOUNT = 0
        mod._SHARED_LOCK = None

    @pytest.mark.asyncio
    async def test_acquire_then_release_drops_refcount(self):
        from unittest.mock import AsyncMock, patch
        import boxagent.agent.sdk_copilot_process as mod

        fake_client = AsyncMock()
        with patch.object(mod, "CopilotClient", return_value=fake_client):
            client = await mod._acquire_shared_client()
            assert client is fake_client
            assert mod._SHARED_REFCOUNT == 1
            fake_client.start.assert_awaited_once()

            await mod._release_shared_client()
            assert mod._SHARED_REFCOUNT == 0
            assert mod._SHARED_CLIENT is None
            fake_client.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_multiple_acquire_share_one_client(self):
        from unittest.mock import AsyncMock, patch
        import boxagent.agent.sdk_copilot_process as mod

        fake_client = AsyncMock()
        with patch.object(mod, "CopilotClient", return_value=fake_client) as MockClient:
            c1 = await mod._acquire_shared_client()
            c2 = await mod._acquire_shared_client()
            c3 = await mod._acquire_shared_client()
            assert c1 is c2 is c3 is fake_client
            # Only constructed ONCE, even after 3 acquires.
            MockClient.assert_called_once()
            assert mod._SHARED_REFCOUNT == 3
            fake_client.start.assert_awaited_once()

            # Two releases — still alive, refcount=1
            await mod._release_shared_client()
            await mod._release_shared_client()
            assert mod._SHARED_REFCOUNT == 1
            fake_client.stop.assert_not_awaited()

            # Final release — stops.
            await mod._release_shared_client()
            assert mod._SHARED_REFCOUNT == 0
            assert mod._SHARED_CLIENT is None
            fake_client.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_multi_instance_through_backend_lifecycle(self):
        """Three AgentSDKCopilot.start() + send() → one shared client.

        We don't actually call send (would hit the real CLI); just verify
        that ensure_started routes through _acquire_shared_client and
        stop() releases."""
        from unittest.mock import AsyncMock, patch
        import boxagent.agent.sdk_copilot_process as mod

        fake_client = AsyncMock()
        with patch.object(mod, "CopilotClient", return_value=fake_client) as MockClient:
            backends = [
                mod.AgentSDKCopilot(workspace="/tmp", bot_name=f"b{i}")
                for i in range(3)
            ]
            for b in backends:
                b.start()
                await b._ensure_started()

            MockClient.assert_called_once()
            assert mod._SHARED_REFCOUNT == 3

            for b in backends:
                await b.stop()

            assert mod._SHARED_REFCOUNT == 0
            assert mod._SHARED_CLIENT is None
