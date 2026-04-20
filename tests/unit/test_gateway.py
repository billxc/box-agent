"""Unit tests for Gateway — startup/shutdown orchestration."""

import asyncio
import os

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock


class TestGateway:
    def test_supports_persistent_session(self):
        from boxagent.gateway import _supports_persistent_session

        assert _supports_persistent_session("claude-cli") is True
        assert _supports_persistent_session("codex-acp") is True

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

        with patch.object(gw, "_start_bot", new_callable=AsyncMock) as m:
            with patch.object(gw, "_start_scheduler"):
                with patch.object(gw, "_start_http", new_callable=AsyncMock):
                    await gw.start()
            m.assert_called_once_with(
                "test-bot", mock_config.bots["test-bot"]
            )

    def test_box_agent_dir_changes_default_dirs(self, tmp_path):
        from boxagent.gateway import Gateway

        mock_config = MagicMock()
        custom_box_agent_dir = tmp_path / "ba-dir"

        with patch.dict(os.environ, {"BOX_AGENT_DIR": str(custom_box_agent_dir)}):
            gw = Gateway(config=mock_config)

        assert gw.config_dir == custom_box_agent_dir
        assert gw.local_dir == custom_box_agent_dir / "local"

    async def test_stop_stops_all_components(self, tmp_path):
        from boxagent.gateway import Gateway

        mock_config = MagicMock()
        mock_config.bots = {}

        gw = Gateway(config=mock_config, config_dir=tmp_path)
        mock_ch = AsyncMock()
        mock_cli = AsyncMock()
        gw._channels = {"test-bot": mock_ch}
        gw._cli_processes = {"test-bot": mock_cli}

        await gw.stop()

        mock_ch.stop.assert_called_once()
        mock_cli.stop.assert_called_once()

    async def test_start_creates_scheduler(self, tmp_path):
        from boxagent.gateway import Gateway

        mock_config = MagicMock()
        mock_config.bots = {}
        mock_config.node_id = "test-node"

        gw = Gateway(config=mock_config, config_dir=tmp_path)
        with patch.object(gw, "_start_http", new_callable=AsyncMock):
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
        with patch.object(gw, "_start_http", new_callable=AsyncMock):
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
        bot_cfg.workspace = str(tmp_path)
        bot_cfg.display_tool_calls = "summary"
        bot_cfg.model = ""
        bot_cfg.agent = ""
        bot_cfg.extra_skill_dirs = []
        bot_cfg.display_name = ""
        bot_cfg.ai_backend = "claude-cli"
        bot_cfg.enabled_on_nodes = ""

        with patch("boxagent.gateway.ClaudeProcess") as MockCLI, \
             patch("boxagent.gateway.TelegramChannel") as MockChan, \
             patch("boxagent.gateway.Router"), \
             patch("boxagent.gateway.Watchdog") as MockWD:
            mock_cli = MagicMock()
            MockCLI.return_value = mock_cli
            mock_channel = AsyncMock()
            MockChan.return_value = mock_channel
            mock_wd = MagicMock()
            mock_wd.run_forever = AsyncMock()
            MockWD.return_value = mock_wd

            await gw._start_bot("my-bot", bot_cfg)

            gw._storage.load_session.assert_called_once_with(
                "my-bot",
                backend="claude-cli",
                workspace=str(tmp_path),
            )

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
        old_cli = MagicMock()
        old_cli.session_id = "old-session"
        old_cli.stop = AsyncMock()
        gw._cli_processes["my-bot"] = old_cli

        mock_channel = MagicMock()
        gw._scheduler = Scheduler(
            schedules_file=tmp_path / "schedules.yaml",
            node_id="test-node",
            bot_refs={"my-bot": BotRef(
                cli_process=old_cli, channel=mock_channel, chat_id="123",
            )},
        )

        bot_cfg = MagicMock()
        bot_cfg.workspace = str(tmp_path)
        bot_cfg.model = ""
        bot_cfg.agent = ""
        bot_cfg.telegram_token = "token"
        bot_cfg.extra_skill_dirs = []

        with patch("boxagent.gateway.ClaudeProcess") as MockCLI:
            new_cli = MagicMock()
            MockCLI.return_value = new_cli
            await gw._restart_bot("my-bot", bot_cfg)

        assert gw._scheduler.bot_refs["my-bot"].cli_process is new_cli

    async def test_start_bot_loads_saved_codex_acp_session(self, tmp_path):
        from boxagent.gateway import Gateway

        mock_config = MagicMock()
        mock_config.bots = {}
        mock_config.node_id = "test-node"

        gw = Gateway(config=mock_config, config_dir=tmp_path)
        gw._storage = MagicMock()
        gw._storage.load_session.return_value = "saved-acp-session"
        gw._start_time = 1.0

        bot_cfg = MagicMock()
        bot_cfg.ai_backend = "codex-acp"
        bot_cfg.telegram_token = "123:ABC"
        bot_cfg.allowed_users = [111]
        bot_cfg.workspace = str(tmp_path)
        bot_cfg.display_tool_calls = "summary"
        bot_cfg.model = ""
        bot_cfg.agent = ""
        bot_cfg.extra_skill_dirs = []

        with patch("boxagent.agent.acp_process.ACPProcess") as MockACP, \
             patch("boxagent.gateway.TelegramChannel") as MockChan, \
             patch("boxagent.gateway.Router"), \
             patch("boxagent.gateway.Watchdog") as MockWD:
            mock_cli = MagicMock()
            MockACP.return_value = mock_cli
            MockChan.return_value = AsyncMock()
            mock_wd = MagicMock()
            mock_wd.run_forever = AsyncMock()
            MockWD.return_value = mock_wd

            await gw._start_bot("my-bot", bot_cfg)

        assert MockACP.call_args.kwargs["session_id"] == "saved-acp-session"
        gw._storage.load_session.assert_called_once_with(
            "my-bot",
            backend="codex-acp",
            workspace=str(tmp_path),
        )

    async def test_stop_persists_codex_session_reference(self, tmp_path):
        from boxagent.gateway import Gateway

        mock_config = MagicMock()
        mock_config.bots = {
            "test-bot": MagicMock(
                ai_backend="claude-cli",
                workspace=str(tmp_path),
            )
        }

        gw = Gateway(config=mock_config, config_dir=tmp_path)
        gw._storage = MagicMock()
        mock_ch = AsyncMock()
        mock_cli = AsyncMock()
        mock_cli.session_id = "sess_123"
        mock_cli.supports_session_persistence = True
        gw._channels = {"test-bot": mock_ch}
        gw._cli_processes = {"test-bot": mock_cli}
        gw._routers = {
            "test-bot": MagicMock(
                ai_backend="claude-cli",
                workspace=str(tmp_path),
            )
        }

        await gw.stop()

        gw._storage.save_session.assert_called_once_with(
            "test-bot",
            "sess_123",
            backend="claude-cli",
            workspace=str(tmp_path),
        )

    def test_sync_skills_uses_agents_dir_for_codex_acp(self, tmp_path):
        from boxagent.gateway import sync_skills

        workspace = tmp_path / "workspace"
        source_root = tmp_path / "skills-src"
        skill_dir = source_root / "demo-skill"
        skill_dir.mkdir(parents=True)

        linked = sync_skills(
            str(workspace),
            [str(source_root)],
            ai_backend="codex-acp",
        )

        assert linked == ["demo-skill"]
        assert (workspace / ".agents" / "skills" / "demo-skill").is_symlink()

    async def test_start_http_creates_unix_socket(self, tmp_path):
        from boxagent.gateway import Gateway
        from aiohttp import web

        mock_config = MagicMock()
        mock_config.bots = {}
        mock_config.node_id = "test-node"
        mock_config.api_port = 0

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        gw = Gateway(config=mock_config, config_dir=tmp_path, local_dir=local_dir)
        gw._scheduler = MagicMock()

        with patch.object(web, "UnixSite") as MockUnix:
            mock_unix = AsyncMock()
            MockUnix.return_value = mock_unix
            await gw._start_http()
            MockUnix.assert_called_once()
            assert str(local_dir / "api.sock") in str(MockUnix.call_args)
            mock_unix.start.assert_called_once()

        await gw._stop_http()

    async def test_start_http_no_tcp_when_port_zero(self, tmp_path):
        from boxagent.gateway import Gateway
        from aiohttp import web

        mock_config = MagicMock()
        mock_config.bots = {}
        mock_config.node_id = "test-node"
        mock_config.api_port = 0

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        gw = Gateway(config=mock_config, config_dir=tmp_path, local_dir=local_dir)
        gw._scheduler = MagicMock()

        with patch.object(web, "UnixSite") as MockUnix, \
             patch.object(web, "TCPSite") as MockTCP:
            MockUnix.return_value = AsyncMock()
            await gw._start_http()
            MockTCP.assert_not_called()

        await gw._stop_http()

    async def test_start_http_with_tcp_when_port_set(self, tmp_path):
        from boxagent.gateway import Gateway
        from aiohttp import web

        mock_config = MagicMock()
        mock_config.bots = {}
        mock_config.node_id = "test-node"
        mock_config.api_port = 19876

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        gw = Gateway(config=mock_config, config_dir=tmp_path, local_dir=local_dir)
        gw._scheduler = MagicMock()

        with patch.object(web, "UnixSite") as MockUnix, \
             patch.object(web, "TCPSite") as MockTCP:
            MockUnix.return_value = AsyncMock()
            mock_tcp = AsyncMock()
            MockTCP.return_value = mock_tcp
            await gw._start_http()
            MockTCP.assert_called_once()
            assert MockTCP.call_args[0][2] == 19876
            mock_tcp.start.assert_called_once()

        await gw._stop_http()

    async def test_start_http_writes_windows_port_file(self, tmp_path):
        from boxagent.gateway import Gateway
        from aiohttp import web

        mock_config = MagicMock()
        mock_config.bots = {}
        mock_config.node_id = "test-node"
        mock_config.api_port = 0

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        gw = Gateway(config=mock_config, config_dir=tmp_path, local_dir=local_dir)
        gw._scheduler = MagicMock()

        mock_socket = MagicMock()
        mock_socket.getsockname.return_value = ("127.0.0.1", 50762)
        mock_server = MagicMock(sockets=[mock_socket])
        mock_tcp = AsyncMock()
        mock_tcp._server = mock_server

        with patch("boxagent.gateway.sys.platform", "win32"), \
             patch.object(web, "TCPSite", return_value=mock_tcp) as MockTCP:
            await gw._start_http()
            MockTCP.assert_called_once()

        assert (local_dir / "api-port.txt").read_text(encoding="utf-8") == "50762\n"

        await gw._stop_http()

    async def test_stop_http_removes_socket(self, tmp_path):
        from boxagent.gateway import Gateway

        mock_config = MagicMock()
        mock_config.bots = {}
        mock_config.node_id = "test-node"
        mock_config.api_port = 0

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        gw = Gateway(config=mock_config, config_dir=tmp_path, local_dir=local_dir)

        # Simulate endpoint artifacts left behind
        sock = local_dir / "api.sock"
        sock.touch()
        port_file = local_dir / "api-port.txt"
        port_file.write_text("50762\n", encoding="utf-8")
        assert sock.exists()
        assert port_file.exists()

        await gw._stop_http()
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
        original_start_bot = gw._start_bot

        async def track_start_bot(name, cfg):
            started.append(name)

        with patch.object(gw, "_start_bot", side_effect=track_start_bot):
            with patch.object(gw, "_start_scheduler"):
                with patch.object(gw, "_start_http", new_callable=AsyncMock):
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

        async def track_start_bot(name, cfg):
            started.append(name)

        with patch.object(gw, "_start_bot", side_effect=track_start_bot):
            with patch.object(gw, "_start_scheduler"):
                with patch.object(gw, "_start_http", new_callable=AsyncMock):
                    await gw.start()

        assert "bot-a" in started
        assert "bot-b" in started
