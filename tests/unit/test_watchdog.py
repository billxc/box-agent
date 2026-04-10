"""Unit tests for Watchdog — process liveness monitoring."""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestWatchdog:
    async def test_detects_dead_process(self):
        from boxagent.watchdog import Watchdog

        mock_cli = MagicMock()
        mock_cli.state = "dead"
        mock_cli.session_id = "sess_old"

        mock_channel = AsyncMock()
        mock_channel.send_text = AsyncMock()

        restart_called = asyncio.Event()
        original_restart = None

        async def fake_restart():
            restart_called.set()

        wd = Watchdog(
            cli_process=mock_cli,
            channel=mock_channel,
            chat_id="123",
            bot_name="test-bot",
            on_restart=fake_restart,
            check_interval=0.1,
            restart_delay=0.0,  # no delay in tests
        )

        task = asyncio.create_task(wd.run_once())
        await task

        # Should have called on_restart
        assert restart_called.is_set()
        # Should have notified user
        mock_channel.send_text.assert_called_once()

    async def test_healthy_process_no_action(self):
        from boxagent.watchdog import Watchdog

        mock_cli = MagicMock()
        mock_cli.state = "idle"

        mock_channel = AsyncMock()
        restart_called = False

        async def fake_restart():
            nonlocal restart_called
            restart_called = True

        wd = Watchdog(
            cli_process=mock_cli,
            channel=mock_channel,
            chat_id="123",
            bot_name="test-bot",
            on_restart=fake_restart,
        )

        await wd.run_once()

        assert not restart_called
        mock_channel.send_text.assert_not_called()

    async def test_busy_process_no_action(self):
        from boxagent.watchdog import Watchdog

        mock_cli = MagicMock()
        mock_cli.state = "busy"

        mock_channel = AsyncMock()
        restart_called = False

        async def fake_restart():
            nonlocal restart_called
            restart_called = True

        wd = Watchdog(
            cli_process=mock_cli,
            channel=mock_channel,
            chat_id="123",
            bot_name="test-bot",
            on_restart=fake_restart,
        )

        await wd.run_once()

        assert not restart_called
