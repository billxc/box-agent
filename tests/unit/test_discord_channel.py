"""Unit tests for DiscordChannel — mock discord.py client."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord as _discord
import pytest

from boxagent.channels.base import IncomingMessage, StreamHandle


def _make_mock_message(msg_id):
    """Create a mock discord.Message with given id."""
    msg = MagicMock()
    msg.id = msg_id
    msg.edit = AsyncMock()
    return msg


@pytest.fixture
def mock_client():
    """Mock discord.Client."""
    client = MagicMock()
    client.user = MagicMock()
    client.user.id = 999
    client.is_closed = MagicMock(return_value=False)
    client.close = AsyncMock()

    _msg_counter = iter(range(100, 10000))

    mock_channel = AsyncMock()

    def _send_side_effect(*args, **kwargs):
        msg = _make_mock_message(next(_msg_counter))
        return msg

    mock_channel.send = AsyncMock(side_effect=_send_side_effect)
    mock_channel.typing = AsyncMock()
    mock_channel.fetch_message = AsyncMock(
        side_effect=lambda mid: _make_mock_message(mid)
    )

    client.get_channel = MagicMock(return_value=mock_channel)
    client.fetch_channel = AsyncMock(return_value=mock_channel)
    client._mock_channel = mock_channel
    return client


@pytest.fixture
def make_channel(mock_client):
    """Factory for DiscordChannel with mocked client."""
    from boxagent.channels.discord import DiscordChannel

    def _make(on_message=None, tool_calls_display="summary", categories=None):
        channel = DiscordChannel(
            token="fake.token",
            tool_calls_display=tool_calls_display,
        )
        channel._client = mock_client
        if on_message:
            cats = categories if categories is not None else [42]
            channel.register_route(on_message, cats)
        return channel

    return _make


class TestSendText:
    async def test_send_text_calls_channel_send(self, make_channel, mock_client):
        """send_text() calls channel.send with correct text."""
        channel = make_channel()
        await channel.send_text("123", "Hello world")

        mock_client._mock_channel.send.assert_called_once_with("Hello world")

    async def test_long_message_splits(self, make_channel, mock_client):
        """Messages over 2000 chars are split into multiple sends."""
        channel = make_channel()
        long_text = "A" * 1000 + "\n\n" + "B" * 1000 + "\n\n" + "C" * 1000
        await channel.send_text("123", long_text)

        assert mock_client._mock_channel.send.call_count >= 2


class TestStreaming:
    async def test_stream_start_sends_initial_message(
        self, make_channel, mock_client
    ):
        """stream_start() sends placeholder and returns StreamHandle."""
        channel = make_channel()
        handle = await channel.stream_start("123")

        assert isinstance(handle, StreamHandle)
        assert handle.message_id == "100"
        assert handle.chat_id == "123"
        mock_client._mock_channel.send.assert_called_once()

    async def test_stream_update_throttled(self, make_channel, mock_client):
        """stream_update() throttles edits."""
        channel = make_channel()
        handle = StreamHandle(message_id="100", chat_id="123")

        for i in range(10):
            await channel.stream_update(handle, f"text chunk {i}")

        await asyncio.sleep(0.4)
        assert mock_client._mock_channel.fetch_message.call_count < 10

    async def test_stream_end_flushes(self, make_channel, mock_client):
        """stream_end() sends final edit."""
        channel = make_channel()
        handle = StreamHandle(message_id="100", chat_id="123")

        await channel.stream_update(handle, "partial")
        await channel.stream_end(handle)

        mock_client._mock_channel.fetch_message.assert_called()

    async def test_stream_update_flushes_on_char_threshold(
        self, make_channel, mock_client
    ):
        """If buffer exceeds 200 chars, flush immediately."""
        channel = make_channel()
        handle = StreamHandle(message_id="100", chat_id="123")

        await channel.stream_update(handle, "X" * 250)
        await asyncio.sleep(0.05)
        assert mock_client._mock_channel.fetch_message.call_count >= 1


class TestToolCallDisplay:
    def test_silent_mode_suppresses_tool_output(self, make_channel):
        """tool_calls=silent: no tool call text added."""
        channel = make_channel(tool_calls_display="silent")
        result = channel.format_tool_call("Bash", {"command": "ls"})
        assert result == ""

    def test_summary_mode_shows_name_only(self, make_channel):
        """tool_calls=summary: shows tool name emoji."""
        channel = make_channel(tool_calls_display="summary")
        result = channel.format_tool_call("Bash", {"command": "ls"})
        assert "Bash" in result
        assert "ls" not in result

    def test_detailed_mode_shows_input(self, make_channel):
        """tool_calls=detailed: shows tool name + truncated input."""
        channel = make_channel(tool_calls_display="detailed")
        result = channel.format_tool_call("Bash", {"command": "ls -la"})
        assert "Bash" in result
        assert "ls" in result


def _collect_edit_texts(mock_client) -> list[str]:
    """Extract all edited message texts."""
    texts = []
    for call in mock_client._mock_channel.fetch_message.call_args_list:
        msg_id = call.args[0] if call.args else call.kwargs.get("mid")
        texts.append(msg_id)
    return texts


class TestStreamSplit:
    """Tests for automatic stream message splitting at Discord limit."""

    async def test_stream_auto_splits_at_threshold(
        self, make_channel, mock_client
    ):
        """Buffer exceeding ~1800 chars triggers split to new message."""
        channel = make_channel()
        handle = await channel.stream_start("123")
        original_mid = handle.message_id

        # Feed 3 chunks of 700 chars each (total 2100 > 1800)
        for i in range(3):
            await channel.stream_update(handle, f"{i}" * 700)

        await channel.stream_end(handle)

        # handle should have been updated to a new message
        assert handle.message_id != original_mid
        # At least 2 send calls: initial placeholder + split
        assert mock_client._mock_channel.send.call_count >= 2

    async def test_short_stream_no_split(self, make_channel, mock_client):
        """Short messages don't trigger any split."""
        channel = make_channel()
        handle = await channel.stream_start("123")
        original_mid = handle.message_id

        await channel.stream_update(handle, "short text")
        await channel.stream_end(handle)

        assert handle.message_id == original_mid
        assert mock_client._mock_channel.send.call_count == 1


class TestIncomingMessage:
    async def test_ignores_bot_own_messages(self, make_channel, mock_client):
        """Bot's own messages are ignored."""
        received = []
        channel = make_channel(on_message=lambda m: received.append(m))

        # Simulate a message from the bot itself
        msg = MagicMock()
        msg.author = mock_client.user
        msg.type = _discord.MessageType.default
        msg.content = "my own message"
        msg.channel = MagicMock()
        msg.channel.id = 123
        msg.channel.category_id = 42
        msg.attachments = []

        await channel._handle_incoming(msg)
        assert len(received) == 0

    async def test_handles_user_message(self, make_channel, mock_client):
        """User messages are forwarded to the registered route callback."""
        received = []

        async def on_msg(m):
            received.append(m)

        channel = make_channel(on_message=on_msg)

        msg = MagicMock()
        msg.author = MagicMock()
        msg.author.id = 456
        msg.type = _discord.MessageType.default
        msg.content = "hello"
        msg.channel = MagicMock()
        msg.channel.id = 789
        msg.channel.category_id = 42  # matches registered route
        msg.attachments = []

        await channel._handle_incoming(msg)
        assert len(received) == 1
        assert received[0].text == "hello"
        assert received[0].chat_id == "789"
        assert received[0].user_id == "456"
        assert received[0].channel == "discord"

    async def test_ignores_unregistered_category(self, make_channel, mock_client):
        """Messages from unregistered categories are silently ignored."""
        received = []

        async def on_msg(m):
            received.append(m)

        channel = make_channel(on_message=on_msg, categories=[42])

        msg = MagicMock()
        msg.author = MagicMock()
        msg.author.id = 456
        msg.type = _discord.MessageType.default
        msg.content = "hello"
        msg.channel = MagicMock()
        msg.channel.id = 789
        msg.channel.category_id = 99  # not registered
        msg.attachments = []

        await channel._handle_incoming(msg)
        assert len(received) == 0

    async def test_dm_routed_when_registered(self, make_channel, mock_client):
        """DM messages route to callback registered with DM_CATEGORY."""
        from boxagent.channels.discord import DM_CATEGORY

        received = []

        async def on_msg(m):
            received.append(m)

        channel = make_channel(on_message=on_msg, categories=[DM_CATEGORY])

        msg = MagicMock()
        msg.author = MagicMock()
        msg.author.id = 456
        msg.type = _discord.MessageType.default
        msg.content = "dm message"
        msg.channel = MagicMock(spec=_discord.DMChannel)
        msg.channel.id = 789
        msg.attachments = []

        await channel._handle_incoming(msg)
        assert len(received) == 1
        assert received[0].text == "dm message"
