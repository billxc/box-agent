"""Unit tests for Gateway — startup/shutdown orchestration."""

import asyncio
import os

from unittest.mock import AsyncMock, MagicMock, patch


def _http_server_from(gw):
    """Build an HttpApiServer bound to a Gateway's deps (for tests that
    bypass gw.start()).
    """
    from boxagent.gateway.http_api_server import HttpApiServer
    return HttpApiServer(
        config=gw.config,
        config_dir=gw.config_dir,
        local_dir=gw.local_dir,
        peer=gw._peer,
        workgroup_routes=gw._workgroup_routes,
        scheduler_routes=gw._scheduler_routes,
        mcp_gateway_context=gw,
    )


def _agent_mgr_from(gw):
    """Build an AgentManager for tests that bypass gw.start()."""
    from boxagent.agent.agent_manager import AgentManager
    return AgentManager(
        config=gw.config,
        config_dir=gw.config_dir,
        storage=gw._storage,
        start_time=gw._start_time,
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

        gw = Gateway(config=mock_config, config_dir=tmp_path)

        started: list[str] = []

        async def track(self, name, cfg):
            started.append(name)

        async def noop_raw(self):
            pass

        with patch("boxagent.agent.agent_manager.AgentManager.start_bot",
                   side_effect=track, autospec=True), \
             patch("boxagent.agent.agent_manager.AgentManager.start_raw_bot",
                   side_effect=noop_raw, autospec=True), \
             patch.object(gw, "_start_scheduler"), \
             patch("boxagent.gateway.http_api_server.HttpApiServer.start", new_callable=AsyncMock), \
             patch("boxagent.transports.web.server.WebHttpServer.start", new_callable=AsyncMock):
            await gw.start()

        assert started == ["test-bot"]

    def test_box_agent_dir_changes_default_dirs(self, tmp_path):
        from boxagent.gateway import Gateway

        mock_config = MagicMock()
        custom_box_agent_dir = tmp_path / "ba-dir"

        with patch.dict(os.environ, {"BOX_AGENT_DIR": str(custom_box_agent_dir)}):
            gw = Gateway(config=mock_config)

        assert gw.config_dir == custom_box_agent_dir
        assert gw.local_dir == custom_box_agent_dir / "local"

    async def test_stop_does_not_crash_without_start(self, tmp_path):
        """Gateway.stop() before start() should be a no-op (all manager refs
        are None). Per-resource teardown lives on AgentManager.stop() and is
        covered by test_agent_manager.py."""
        from boxagent.gateway import Gateway

        mock_config = MagicMock()
        mock_config.bots = {}

        gw = Gateway(config=mock_config, config_dir=tmp_path)
        await gw.stop()

    async def test_start_creates_scheduler(self, tmp_path):
        from boxagent.gateway import Gateway

        mock_config = MagicMock()
        mock_config.bots = {}
        mock_config.node_id = "test-node"

        gw = Gateway(config=mock_config, config_dir=tmp_path)
        with patch("boxagent.gateway.http_api_server.HttpApiServer.start", new_callable=AsyncMock), \
                     patch("boxagent.transports.web.server.WebHttpServer.start", new_callable=AsyncMock):
            await gw.start()

        assert gw._scheduler is not None
        assert gw._scheduler_task is not None
        assert not gw._scheduler_task.done()

        # Cleanup
        gw._scheduler.stop()
        gw._scheduler_task.cancel()
        try:
            await gw._scheduler_task
        except asyncio.CancelledError:
            pass

    async def test_stop_cancels_scheduler(self, tmp_path):
        from boxagent.gateway import Gateway

        mock_config = MagicMock()
        mock_config.bots = {}
        mock_config.node_id = "test-node"

        gw = Gateway(config=mock_config, config_dir=tmp_path)
        with patch("boxagent.gateway.http_api_server.HttpApiServer.start", new_callable=AsyncMock), \
                     patch("boxagent.transports.web.server.WebHttpServer.start", new_callable=AsyncMock):
            await gw.start()

        scheduler_task = gw._scheduler_task
        await gw.stop()

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

        gw = Gateway(config=mock_config, config_dir=tmp_path)
        gw._storage = MagicMock()
        gw._storage.load_session.return_value = None
        gw._start_time = 1.0

        bot_cfg = MagicMock()
        bot_cfg.telegram_token = "123:ABC"
        bot_cfg.allowed_users = [111]
        bot_cfg.telegram_allowed_users = [111]
        bot_cfg.workspace = str(tmp_path)
        bot_cfg.display_tool_calls = "summary"
        bot_cfg.model = ""
        bot_cfg.agent = ""
        bot_cfg.extra_skill_dirs = []
        bot_cfg.display_name = ""
        bot_cfg.ai_backend = "claude-cli"
        bot_cfg.enabled_on_nodes = ""

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

            await _agent_mgr_from(gw).start_bot("my-bot", bot_cfg)
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

        gw = Gateway(config=mock_config, config_dir=tmp_path)

        # Set up a mock scheduler with a bot ref
        old_backend = MagicMock()
        old_backend.session_id = "old-session"
        old_backend.stop = AsyncMock()

        mock_channel = MagicMock()
        gw._scheduler = Scheduler(
            schedules_file=tmp_path / "schedules.yaml",
            node_id="test-node",
            bot_refs={"my-bot": BotRef(
                backend=old_backend, channel=mock_channel, chat_id="123",
            )},
        )

        bot_cfg = MagicMock()
        bot_cfg.workspace = str(tmp_path)
        bot_cfg.model = ""
        bot_cfg.agent = ""
        bot_cfg.telegram_token = "token"
        bot_cfg.extra_skill_dirs = []

        with patch("boxagent.agent.backend_factory.ClaudeProcess") as MockCLI:
            new_backend = MagicMock()
            MockCLI.return_value = new_backend
            mgr = _agent_mgr_from(gw)
            mgr.backends["my-bot"] = old_backend  # seed manager state
            mgr.set_scheduler(gw._scheduler)
            await mgr.restart_bot("my-bot", bot_cfg)

        assert gw._scheduler.bot_refs["my-bot"].backend is new_backend

    async def test_start_bot_loads_saved_codex_session(self, tmp_path):
        from boxagent.gateway import Gateway

        mock_config = MagicMock()
        mock_config.bots = {}
        mock_config.node_id = "test-node"

        gw = Gateway(config=mock_config, config_dir=tmp_path)
        gw._storage = MagicMock()
        gw._storage.load_session.return_value = "saved-codex-session"
        gw._start_time = 1.0

        bot_cfg = MagicMock()
        bot_cfg.ai_backend = "codex-cli"
        bot_cfg.telegram_token = "123:ABC"
        bot_cfg.allowed_users = [111]
        bot_cfg.workspace = str(tmp_path)
        bot_cfg.display_tool_calls = "summary"
        bot_cfg.model = ""
        bot_cfg.agent = ""
        bot_cfg.yolo = False
        bot_cfg.extra_skill_dirs = []

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

            await _agent_mgr_from(gw).start_bot("my-bot", bot_cfg)

        assert MockCodex.call_args_list[0].kwargs["session_id"] == "saved-codex-session"

    def test_sync_skills_uses_agents_dir_for_codex_cli(self, tmp_path):
        from boxagent.agent import sync_skills

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
        gw = Gateway(config=mock_config, config_dir=tmp_path, local_dir=local_dir)
        gw._peer = MagicMock()
        gw._workgroup_routes = MagicMock()
        gw._scheduler_routes = MagicMock()
        http_server = _http_server_from(gw)

        mock_socket = MagicMock()
        mock_socket.getsockname.return_value = ("127.0.0.1", 50762)
        mock_server = MagicMock(sockets=[mock_socket])
        mock_tcp = AsyncMock()
        mock_tcp._server = mock_server

        with patch.object(web, "TCPSite", return_value=mock_tcp) as MockTCP, \
             patch.object(http_server, "start_mcp", new_callable=AsyncMock):
            await http_server.start()
            MockTCP.assert_called_once()

        assert (local_dir / "api-port.txt").read_text(encoding="utf-8") == "50762\n"

        await http_server.stop()

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
        gw = Gateway(config=mock_config, config_dir=tmp_path, local_dir=local_dir)
        gw._peer = MagicMock()
        gw._workgroup_routes = MagicMock()
        gw._scheduler_routes = MagicMock()
        http_server = _http_server_from(gw)

        with patch.object(web, "TCPSite") as MockTCP, \
             patch.object(http_server, "start_mcp", new_callable=AsyncMock):
            mock_tcp = AsyncMock()
            MockTCP.return_value = mock_tcp
            await http_server.start()
            MockTCP.assert_called_once()
            assert MockTCP.call_args[0][2] == 19876

        await http_server.stop()

    async def test_stop_http_removes_port_file(self, tmp_path):
        from boxagent.gateway import Gateway

        mock_config = MagicMock()
        mock_config.bots = {}
        mock_config.node_id = "test-node"
        mock_config.api_port = 0

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        gw = Gateway(config=mock_config, config_dir=tmp_path, local_dir=local_dir)
        gw._peer = MagicMock()
        gw._workgroup_routes = MagicMock()
        gw._scheduler_routes = MagicMock()
        http_server = _http_server_from(gw)

        port_file = local_dir / "api-port.txt"
        port_file.write_text("50762\n", encoding="utf-8")
        assert port_file.exists()

        await http_server.stop()
        assert not port_file.exists()

    async def test_clear_http_artifacts_removes_stale_sock(self, tmp_path):
        """HttpApiServer._clear_artifacts removes stale api.sock + port files
        from previous runs."""
        from boxagent.gateway.http_api_server import HttpApiServer

        local_dir = tmp_path / "local"
        local_dir.mkdir()

        sock = local_dir / "api.sock"
        sock.touch()
        port_file = local_dir / "api-port.txt"
        port_file.write_text("50762\n", encoding="utf-8")

        srv = HttpApiServer(
            config=MagicMock(),
            config_dir=tmp_path,
            local_dir=local_dir,
            peer=MagicMock(),
            workgroup_routes=None,
            scheduler_routes=MagicMock(),
            mcp_gateway_context=MagicMock(),
        )
        srv._clear_artifacts()
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

        gw = Gateway(config=mock_config, config_dir=tmp_path)

        started = []

        async def track_start_bot(self, name, cfg):
            started.append(name)

        async def noop_raw(self):
            pass

        with patch("boxagent.agent.agent_manager.AgentManager.start_bot",
                   side_effect=track_start_bot, autospec=True), \
             patch("boxagent.agent.agent_manager.AgentManager.start_raw_bot",
                   side_effect=noop_raw, autospec=True), \
             patch.object(gw, "_start_scheduler"), \
             patch("boxagent.gateway.http_api_server.HttpApiServer.start", new_callable=AsyncMock), \
             patch("boxagent.transports.web.server.WebHttpServer.start", new_callable=AsyncMock):
            await gw.start()

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

        gw = Gateway(config=mock_config, config_dir=tmp_path)

        started = []

        async def track_start_bot(self, name, cfg):
            started.append(name)

        async def noop_raw(self):
            pass

        with patch("boxagent.agent.agent_manager.AgentManager.start_bot",
                   side_effect=track_start_bot, autospec=True), \
             patch("boxagent.agent.agent_manager.AgentManager.start_raw_bot",
                   side_effect=noop_raw, autospec=True), \
             patch.object(gw, "_start_scheduler"), \
             patch("boxagent.gateway.http_api_server.HttpApiServer.start", new_callable=AsyncMock), \
             patch("boxagent.transports.web.server.WebHttpServer.start", new_callable=AsyncMock):
            await gw.start()

        assert "bot-a" in started
        assert "bot-b" in started
