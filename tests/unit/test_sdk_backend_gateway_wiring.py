"""SDK backends must propagate the gateway reference into ToolContext.

Without this, in-process tools that need a live Gateway see
``ctx.gateway is None`` and return "Error: gateway not available".
"""

from __future__ import annotations

from unittest.mock import patch

from boxagent.agent.backend_factory import create_backend
from boxagent.config import BotConfig


class _FakeGateway:
    """Sentinel object — only its identity matters for these tests."""


class TestSdkClaudeGatewayField:
    def test_constructor_accepts_gateway(self):
        from boxagent.agent.sdk_claude_process import AgentSDKClaude
        gw = _FakeGateway()
        backend = AgentSDKClaude(workspace="/tmp", bot_name="t", gateway=gw)
        assert backend.gateway is gw

    def test_build_options_passes_gateway_to_tool_context(self):
        """``_build_options`` builds the ToolContext fed to the SDK adapter.

        We patch ``build_mcp_servers`` to capture the ctx and assert
        ``ctx.gateway`` is the same object passed at construction.
        """
        from boxagent.agent.sdk_claude_process import AgentSDKClaude
        from boxagent.agent_env import AgentEnv

        gw = _FakeGateway()
        backend = AgentSDKClaude(workspace="/tmp", bot_name="t", gateway=gw)
        env = AgentEnv(bot_name="t", workspace="/tmp", ai_backend="agent-sdk-claude")

        captured: dict = {}

        def fake_build(*, ctx, env):  # noqa: ARG001
            captured["ctx"] = ctx
            return {}

        with patch(
            "boxagent.tools.adapters.claude_sdk.build_mcp_servers",
            side_effect=fake_build,
        ):
            backend._build_options(model="opus", append_system_prompt="", chat_id="c1", env=env)

        assert captured["ctx"].gateway is gw
        assert captured["ctx"].bot_name == "t"
        assert captured["ctx"].chat_id == "c1"


class TestSdkCopilotGatewayField:
    def test_constructor_accepts_gateway(self):
        from boxagent.agent.sdk_copilot_process import AgentSDKCopilot
        gw = _FakeGateway()
        backend = AgentSDKCopilot(workspace="/tmp", bot_name="t", gateway=gw)
        assert backend.gateway is gw


class TestCreateBackendForwardsGateway:
    def test_sdk_claude_receives_gateway(self):
        from boxagent.agent.sdk_claude_process import AgentSDKClaude
        gw = _FakeGateway()
        config = BotConfig(name="t", ai_backend="agent-sdk-claude", workspace="/tmp")
        backend = create_backend(config, session_id=None, gateway=gw)
        assert isinstance(backend, AgentSDKClaude)
        assert backend.gateway is gw

    def test_sdk_copilot_receives_gateway(self):
        from boxagent.agent.sdk_copilot_process import AgentSDKCopilot
        gw = _FakeGateway()
        config = BotConfig(name="t", ai_backend="agent-sdk-copilot", workspace="/tmp")
        backend = create_backend(config, session_id=None, gateway=gw)
        assert isinstance(backend, AgentSDKCopilot)
        assert backend.gateway is gw

    def test_legacy_claude_cli_alias_also_receives_gateway(self):
        from boxagent.agent.sdk_claude_process import AgentSDKClaude
        gw = _FakeGateway()
        config = BotConfig(name="t", ai_backend="claude-cli", workspace="/tmp")
        backend = create_backend(config, session_id=None, gateway=gw)
        assert isinstance(backend, AgentSDKClaude)
        assert backend.gateway is gw

    def test_gateway_default_is_none(self):
        from boxagent.agent.sdk_claude_process import AgentSDKClaude
        config = BotConfig(name="t", ai_backend="agent-sdk-claude", workspace="/tmp")
        backend = create_backend(config, session_id=None)
        assert isinstance(backend, AgentSDKClaude)
        assert backend.gateway is None


class TestAgentManagerStoresGateway:
    def test_constructor_accepts_gateway(self, tmp_path):
        from boxagent.agent.agent_manager import AgentManager
        from boxagent.config import AppConfig
        from boxagent.sessions import Storage

        gw = _FakeGateway()
        manager = AgentManager(
            config=AppConfig(),
            config_dir=tmp_path,
            storage=Storage(local_dir=tmp_path),
            start_time=0.0,
            gateway=gw,
        )
        assert manager.gateway is gw
