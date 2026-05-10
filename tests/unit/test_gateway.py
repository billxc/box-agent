"""Unit tests for Gateway — startup/shutdown orchestration."""

import asyncio
import os

from unittest.mock import AsyncMock, MagicMock, patch


def _internal_api_from(gateway):
    """Build an InternalApiServer bound to a Gateway's deps (for tests
    that bypass gateway.start()).
    """
    from boxagent.gateway import InternalApiServer
    return InternalApiServer(
        config=gateway.config,
        local_dir=gateway.local_dir,
        peer=gateway._peer,
        workgroup_routes=gateway._workgroup_routes,
        scheduler_routes=gateway._scheduler_routes,
    )


def _agent_manager_from(gateway):
    """Build an AgentManager for tests that bypass gateway.start()."""
    from boxagent.agent.agent_manager import AgentManager
    return AgentManager(
        config=gateway.config,
        config_dir=gateway.config_dir,
        storage=gateway._storage,
        start_time=gateway._start_time,
    )


class TestGateway:
    def test_supports_persistent_session(self):
        from boxagent.agent.agent_manager import _supports_persistent_session

        assert _supports_persistent_session("claude-cli") is True
        assert _supports_persistent_session("codex-cli") is True
        assert _supports_persistent_session("unknown") is False

    async def test_start_calls_start_bot(self, tmp_path):
        from boxagent.gateway import Gateway

        mock_config = MagicMock()
        mock_config.bots = {
            "test-bot": MagicMock(
                telegram_token="123:ABC",
                allowed_users=[111],
                workspace="/tmp",
                display_tool_calls="summary",
                enabled_on_nodes="",
            )
        }
        mock_config.node_id = "test-node"

        gateway = Gateway(config=mock_config, config_dir=tmp_path)

        started: list[str] = []

        async def track(self, name, config):
            started.append(name)

        async def noop_raw(self):
            pass

        with patch("boxagent.agent.agent_manager.AgentManager.start_bot",
                   side_effect=track, autospec=True), \
             patch("boxagent.agent.agent_manager.AgentManager.start_raw_bot",
                   side_effect=noop_raw, autospec=True), \
             patch.object(gateway, "_start_scheduler"), \
             patch("boxagent.gateway.InternalApiServer.start", new_callable=AsyncMock), \
             patch("boxagent.gateway.McpHttpServer.start", new_callable=AsyncMock), \
             patch("boxagent.transports.web.server.WebHttpServer.start", new_callable=AsyncMock):
            await gateway.start()

        assert started == ["test-bot"]

    def test_box_agent_dir_changes_default_dirs(self, tmp_path):
        from boxagent.gateway import Gateway

        mock_config = MagicMock()
        custom_box_agent_dir = tmp_path / "ba-dir"

        with patch.dict(os.environ, {"BOX_AGENT_DIR": str(custom_box_agent_dir)}):
            gateway = Gateway(config=mock_config)

        assert gateway.config_dir == custom_box_agent_dir
        assert gateway.local_dir == custom_box_agent_dir / "local"

    async def test_stop_does_not_crash_without_start(self, tmp_path):
        """Gateway.stop() before start() should be a no-op (all manager refs
        are None). Per-resource teardown lives on AgentManager.stop() and is
        covered by test_agent_manager.py."""
        from boxagent.gateway import Gateway

        mock_config = MagicMock()
        mock_config.bots = {}

        gateway = Gateway(config=mock_config, config_dir=tmp_path)
        await gateway.stop()

    async def test_start_creates_scheduler(self, tmp_path):
        from boxagent.gateway import Gateway

        mock_config = MagicMock()
        mock_config.bots = {}
        mock_config.node_id = "test-node"

        gateway = Gateway(config=mock_config, config_dir=tmp_path)
        with patch("boxagent.gateway.InternalApiServer.start", new_callable=AsyncMock), \
             patch("boxagent.gateway.McpHttpServer.start", new_callable=AsyncMock), \
                     patch("boxagent.transports.web.server.WebHttpServer.start", new_callable=AsyncMock):
            await gateway.start()

        assert gateway._scheduler is not None
        assert gateway._scheduler_task is not None
        assert not gateway._scheduler_task.done()

        # Cleanup
        gateway._scheduler.stop()
        gateway._scheduler_task.cancel()
        try:
            await gateway._scheduler_task
        except asyncio.CancelledError:
            pass

    async def test_stop_cancels_scheduler(self, tmp_path):
        from boxagent.gateway import Gateway

        mock_config = MagicMock()
        mock_config.bots = {}
        mock_config.node_id = "test-node"

        gateway = Gateway(config=mock_config, config_dir=tmp_path)
        with patch("boxagent.gateway.InternalApiServer.start", new_callable=AsyncMock), \
             patch("boxagent.gateway.McpHttpServer.start", new_callable=AsyncMock), \
                     patch("boxagent.transports.web.server.WebHttpServer.start", new_callable=AsyncMock):
            await gateway.start()

        scheduler_task = gateway._scheduler_task
        await gateway.stop()

        # Give the cancelled task a chance to finish
        try:
            await asyncio.wait_for(scheduler_task, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

        assert scheduler_task.cancelled() or scheduler_task.done()

    async def test_start_bot_sends_online_notification(self, tmp_path):
        from boxagent.gateway import Gateway

        mock_config = MagicMock()
        mock_config.bots = {}
        mock_config.node_id = "test-node"

        gateway = Gateway(config=mock_config, config_dir=tmp_path)
        gateway._storage = MagicMock()
        gateway._storage.load_session.return_value = None
        gateway._start_time = 1.0

        bot_config = MagicMock()
        bot_config.telegram_token = "123:ABC"
        bot_config.allowed_users = [111]
        bot_config.telegram_allowed_users = [111]
        bot_config.workspace = str(tmp_path)
        bot_config.display_tool_calls = "summary"
        bot_config.model = ""
        bot_config.agent = ""
        bot_config.extra_skill_dirs = []
        bot_config.display_name = ""
        bot_config.ai_backend = "claude-cli"
        bot_config.enabled_on_nodes = ""

        with patch("boxagent.agent.backend_factory.ClaudeProcess") as MockCLI, \
             patch("boxagent.transports.telegram.TelegramChannel") as MockChan, \
             patch("boxagent.agent.agent_manager.Router"), \
             patch("boxagent.agent.agent_manager.Watchdog") as MockWD:
            mock_cli = MagicMock()
            MockCLI.return_value = mock_cli
            mock_channel = AsyncMock()
            MockChan.return_value = mock_channel
            mock_wd = MagicMock()
            mock_wd.run_forever = AsyncMock()
            MockWD.return_value = mock_wd

            await _agent_manager_from(gateway).start_bot("my-bot", bot_config)
            # Startup notify is now fire-and-forget; let the task run.
            await asyncio.sleep(0)

            mock_channel.send_text.assert_called_once()
            call_args = mock_channel.send_text.call_args
            assert call_args[0][0] == "111"
            text = call_args[0][1]
            assert "🟢 *my-bot* is online" in text
            assert "backend: `claude-cli`" in text

    async def test_restart_bot_updates_scheduler_ref(self, tmp_path):
        from boxagent.gateway import Gateway
        from boxagent.scheduler import BotRef, Scheduler

        mock_config = MagicMock()
        mock_config.bots = {}
        mock_config.node_id = "test-node"

        gateway = Gateway(config=mock_config, config_dir=tmp_path)

        # Set up a mock scheduler with a bot ref
        old_backend = MagicMock()
        old_backend.session_id = "old-session"
        old_backend.stop = AsyncMock()

        mock_channel = MagicMock()
        gateway._scheduler = Scheduler(
            schedules_file=tmp_path / "schedules.yaml",
            node_id="test-node",
            bot_refs={"my-bot": BotRef(
                backend=old_backend, channel=mock_channel, chat_id="123",
            )},
        )

        bot_config = MagicMock()
        bot_config.workspace = str(tmp_path)
        bot_config.model = ""
        bot_config.agent = ""
        bot_config.telegram_token = "token"
        bot_config.extra_skill_dirs = []

        with patch("boxagent.agent.backend_factory.ClaudeProcess") as MockCLI:
            new_backend = MagicMock()
            MockCLI.return_value = new_backend
            manager = _agent_manager_from(gateway)
            manager.backends["my-bot"] = old_backend  # seed manager state
            manager.set_scheduler(gateway._scheduler)
            await manager.restart_bot("my-bot", bot_config)

        assert gateway._scheduler.bot_refs["my-bot"].backend is new_backend

    async def test_start_bot_loads_saved_codex_session(self, tmp_path):
        from boxagent.gateway import Gateway

        mock_config = MagicMock()
        mock_config.bots = {}
        mock_config.node_id = "test-node"

        gateway = Gateway(config=mock_config, config_dir=tmp_path)
        gateway._storage = MagicMock()
        gateway._storage.load_session.return_value = "saved-codex-session"
        gateway._start_time = 1.0

        bot_config = MagicMock()
        bot_config.ai_backend = "codex-cli"
        bot_config.telegram_token = "123:ABC"
        bot_config.allowed_users = [111]
        bot_config.workspace = str(tmp_path)
        bot_config.display_tool_calls = "summary"
        bot_config.model = ""
        bot_config.agent = ""
        bot_config.yolo = False
        bot_config.extra_skill_dirs = []

        with patch("boxagent.agent.codex_process.CodexProcess") as MockCodex, \
             patch("boxagent.transports.telegram.TelegramChannel") as MockChan, \
             patch("boxagent.agent.agent_manager.Router"), \
             patch("boxagent.agent.agent_manager.Watchdog") as MockWD:
            mock_cli = MagicMock()
            MockCodex.return_value = mock_cli
            MockChan.return_value = AsyncMock()
            mock_wd = MagicMock()
            mock_wd.run_forever = AsyncMock()
            MockWD.return_value = mock_wd

            await _agent_manager_from(gateway).start_bot("my-bot", bot_config)

        assert MockCodex.call_args_list[0].kwargs["session_id"] == "saved-codex-session"

    def test_sync_skills_uses_agents_dir_for_codex_cli(self, tmp_path):
        from boxagent.agent.workspace import sync_skills

        workspace = tmp_path / "workspace"
        source_root = tmp_path / "skills-src"
        skill_dir = source_root / "demo-skill"
        skill_dir.mkdir(parents=True)

        linked = sync_skills(
            str(workspace),
            [str(source_root)],
            ai_backend="codex-cli",
        )

        assert linked == ["demo-skill"]
        assert (workspace / ".agents" / "skills" / "demo-skill").is_symlink()

    async def test_start_http_uses_tcp(self, tmp_path):
        from boxagent.gateway import Gateway
        from aiohttp import web

        mock_config = MagicMock()
        mock_config.bots = {}
        mock_config.node_id = "test-node"
        mock_config.api_port = 0
        mock_config.mcp_port = 0

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        gateway = Gateway(config=mock_config, config_dir=tmp_path, local_dir=local_dir)
        gateway._peer = MagicMock()
        gateway._workgroup_routes = MagicMock()
        gateway._scheduler_routes = MagicMock()
        api = _internal_api_from(gateway)

        mock_socket = MagicMock()
        mock_socket.getsockname.return_value = ("127.0.0.1", 50762)
        mock_server = MagicMock(sockets=[mock_socket])
        mock_tcp = AsyncMock()
        mock_tcp._server = mock_server

        with patch.object(web, "TCPSite", return_value=mock_tcp) as MockTCP:
            await api.start()
            MockTCP.assert_called_once()

        assert (local_dir / "api-port.txt").read_text(encoding="utf-8") == "50762\n"

        await api.stop()

    async def test_start_http_uses_configured_port(self, tmp_path):
        from boxagent.gateway import Gateway
        from aiohttp import web

        mock_config = MagicMock()
        mock_config.bots = {}
        mock_config.node_id = "test-node"
        mock_config.api_port = 19876
        mock_config.mcp_port = 0

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        gateway = Gateway(config=mock_config, config_dir=tmp_path, local_dir=local_dir)
        gateway._peer = MagicMock()
        gateway._workgroup_routes = MagicMock()
        gateway._scheduler_routes = MagicMock()
        api = _internal_api_from(gateway)

        with patch.object(web, "TCPSite") as MockTCP:
            mock_tcp = AsyncMock()
            MockTCP.return_value = mock_tcp
            await api.start()
            MockTCP.assert_called_once()
            assert MockTCP.call_args[0][2] == 19876

        await api.stop()

    async def test_stop_http_removes_port_file(self, tmp_path):
        from boxagent.gateway import Gateway

        mock_config = MagicMock()
        mock_config.bots = {}
        mock_config.node_id = "test-node"
        mock_config.api_port = 0

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        gateway = Gateway(config=mock_config, config_dir=tmp_path, local_dir=local_dir)
        gateway._peer = MagicMock()
        gateway._workgroup_routes = MagicMock()
        gateway._scheduler_routes = MagicMock()
        api = _internal_api_from(gateway)

        port_file = local_dir / "api-port.txt"
        port_file.write_text("50762\n", encoding="utf-8")
        assert port_file.exists()

        await api.stop()
        assert not port_file.exists()

    async def test_clear_http_artifacts_removes_stale_sock(self, tmp_path):
        """InternalApiServer._clear_artifacts removes stale api.sock +
        api-port.txt from previous runs."""
        from boxagent.gateway import InternalApiServer

        local_dir = tmp_path / "local"
        local_dir.mkdir()

        sock = local_dir / "api.sock"
        sock.touch()
        port_file = local_dir / "api-port.txt"
        port_file.write_text("50762\n", encoding="utf-8")

        api = InternalApiServer(
            config=MagicMock(),
            local_dir=local_dir,
            peer=MagicMock(),
            workgroup_routes=None,
            scheduler_routes=MagicMock(),
        )
        api._clear_artifacts()
        assert not sock.exists()
        assert not port_file.exists()

    async def test_start_skips_bot_on_node_mismatch(self, tmp_path):
        from boxagent.gateway import Gateway

        bot_a = MagicMock()
        bot_a.enabled_on_nodes = "cloud-pc"
        bot_b = MagicMock()
        bot_b.enabled_on_nodes = ""

        mock_config = MagicMock()
        mock_config.bots = {"bot-a": bot_a, "bot-b": bot_b}
        mock_config.node_id = "home-server"

        gateway = Gateway(config=mock_config, config_dir=tmp_path)

        started = []

        async def track_start_bot(self, name, config):
            started.append(name)

        async def noop_raw(self):
            pass

        with patch("boxagent.agent.agent_manager.AgentManager.start_bot",
                   side_effect=track_start_bot, autospec=True), \
             patch("boxagent.agent.agent_manager.AgentManager.start_raw_bot",
                   side_effect=noop_raw, autospec=True), \
             patch.object(gateway, "_start_scheduler"), \
             patch("boxagent.gateway.InternalApiServer.start", new_callable=AsyncMock), \
             patch("boxagent.gateway.McpHttpServer.start", new_callable=AsyncMock), \
             patch("boxagent.transports.web.server.WebHttpServer.start", new_callable=AsyncMock):
            await gateway.start()

        assert "bot-a" not in started
        assert "bot-b" in started

    async def test_start_runs_all_bots_when_no_node_filter(self, tmp_path):
        from boxagent.gateway import Gateway

        bot_a = MagicMock()
        bot_a.enabled_on_nodes = ""
        bot_b = MagicMock()
        bot_b.enabled_on_nodes = ""

        mock_config = MagicMock()
        mock_config.bots = {"bot-a": bot_a, "bot-b": bot_b}
        mock_config.node_id = ""

        gateway = Gateway(config=mock_config, config_dir=tmp_path)

        started = []

        async def track_start_bot(self, name, config):
            started.append(name)

        async def noop_raw(self):
            pass

        with patch("boxagent.agent.agent_manager.AgentManager.start_bot",
                   side_effect=track_start_bot, autospec=True), \
             patch("boxagent.agent.agent_manager.AgentManager.start_raw_bot",
                   side_effect=noop_raw, autospec=True), \
             patch.object(gateway, "_start_scheduler"), \
             patch("boxagent.gateway.InternalApiServer.start", new_callable=AsyncMock), \
             patch("boxagent.gateway.McpHttpServer.start", new_callable=AsyncMock), \
             patch("boxagent.transports.web.server.WebHttpServer.start", new_callable=AsyncMock):
            await gateway.start()

        assert "bot-a" in started
        assert "bot-b" in started
