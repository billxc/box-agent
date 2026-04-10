"""Unit tests for tool call display formatting."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from boxagent.channels.base import IncomingMessage
from boxagent.channels.telegram import TelegramChannel
from boxagent.router import Router


class TestFormatToolCall:
    def test_silent_returns_empty(self):
        ch = TelegramChannel(
            token="x", allowed_users=[], tool_calls_display="silent"
        )
        assert ch.format_tool_call("Bash", {"command": "ls"}) == ""

    def test_summary_shows_name_only(self):
        ch = TelegramChannel(
            token="x", allowed_users=[], tool_calls_display="summary"
        )
        result = ch.format_tool_call("Bash", {"command": "ls"})
        assert "Bash" in result
        assert "ls" not in result
        assert "\U0001f527" in result  # wrench emoji

    def test_detailed_shows_input(self):
        ch = TelegramChannel(
            token="x", allowed_users=[], tool_calls_display="detailed"
        )
        result = ch.format_tool_call("Bash", {"command": "ls -la"})
        assert "Bash" in result
        assert "ls -la" in result

    def test_detailed_truncates_long_input(self):
        ch = TelegramChannel(
            token="x", allowed_users=[], tool_calls_display="detailed"
        )
        long_input = {"command": "A" * 500}
        result = ch.format_tool_call("Bash", long_input)
        assert len(result) < 300
        assert "..." in result


class TestFormatToolUpdate:
    def test_silent_returns_empty(self):
        ch = TelegramChannel(
            token="x", allowed_users=[], tool_calls_display="silent"
        )
        assert ch.format_tool_update("Run pwd", status="completed") == ""

    def test_summary_shows_title_only_for_start(self):
        ch = TelegramChannel(
            token="x", allowed_users=[], tool_calls_display="summary"
        )
        result = ch.format_tool_update(
            "Run pwd", status="in_progress", input={"command": "pwd"}
        )
        assert result == "Run pwd"

    def test_summary_shows_short_name_for_completed(self):
        ch = TelegramChannel(
            token="x", allowed_users=[], tool_calls_display="summary"
        )
        result = ch.format_tool_update(
            "Run pwd", status="completed", output={"stdout": "/tmp/acp-test\n"}
        )
        assert result == "pwd"

    def test_summary_shows_short_name_for_failed(self):
        ch = TelegramChannel(
            token="x", allowed_users=[], tool_calls_display="summary"
        )
        result = ch.format_tool_update(
            "Read AGENTS.md", status="failed", output={"error": "missing"}
        )
        assert result == "AGENTS.md"

    def test_detailed_shows_input_for_start(self):
        ch = TelegramChannel(
            token="x", allowed_users=[], tool_calls_display="detailed"
        )
        result = ch.format_tool_update(
            "Run pwd", status="in_progress", input={"command": "pwd"}
        )
        assert "Run pwd" in result
        assert "pwd" in result

    def test_detailed_shows_output_for_terminal_state(self):
        ch = TelegramChannel(
            token="x", allowed_users=[], tool_calls_display="detailed"
        )
        result = ch.format_tool_update(
            "Run pwd", status="completed", output={"stdout": "/tmp/acp-test\n"}
        )
        assert "Run pwd" in result
        assert "/tmp/acp-test" in result


def _msg(text):
    return IncomingMessage(
        channel="telegram", chat_id="123", user_id="123", text=text,
    )


class TestVerboseSwitchesFormat:
    """Verify /verbose actually changes format_tool_call output."""

    @pytest.fixture
    def channel(self):
        return TelegramChannel(
            token="x", allowed_users=[], tool_calls_display="summary"
        )

    @pytest.fixture
    def router(self, channel):
        cli = AsyncMock()
        cli.send = AsyncMock()
        cli.cancel = AsyncMock()
        cli.state = "idle"
        cli.session_id = None
        # Mock send_text so it doesn't hit real Telegram
        channel.send_text = AsyncMock()
        return Router(
            cli_process=cli,
            channel=channel,
            allowed_users=[123],
            bot_name="test-bot",
        )

    async def test_verbose_changes_format_output(self, router, channel):
        """After /verbose, format_tool_call output actually changes."""
        # Start at summary
        assert channel.tool_calls_display == "summary"
        summary_out = channel.format_tool_call("Bash", {"command": "ls"})
        assert "ls" not in summary_out  # summary hides input

        # Switch to detailed
        await router.handle_message(_msg("/verbose"))
        assert channel.tool_calls_display == "detailed"
        detailed_out = channel.format_tool_call("Bash", {"command": "ls"})
        assert "ls" in detailed_out  # detailed shows input

        # Switch to silent
        await router.handle_message(_msg("/verbose"))
        assert channel.tool_calls_display == "silent"
        silent_out = channel.format_tool_call("Bash", {"command": "ls"})
        assert silent_out == ""  # silent returns nothing

        # Back to summary
        await router.handle_message(_msg("/verbose"))
        assert channel.tool_calls_display == "summary"
        back_out = channel.format_tool_call("Bash", {"command": "ls"})
        assert back_out == summary_out  # same as original

    async def test_verbose_on_real_channel_object(self, router, channel):
        """The real TelegramChannel attribute is mutated, not a mock."""
        assert isinstance(channel, TelegramChannel)
        await router.handle_message(_msg("/verbose"))
        # Should be on the real object, not a mock attribute
        assert channel.tool_calls_display == "detailed"
        assert channel.__class__ == TelegramChannel
