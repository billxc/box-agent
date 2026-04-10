"""Unit tests for TelegramChannel — mock aiogram bot."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from boxagent.channels.base import IncomingMessage, StreamHandle


@pytest.fixture
def mock_bot():
    """Mock aiogram Bot."""
    bot = AsyncMock()
    _msg_counter = iter(range(100, 10000))
    bot.send_message = AsyncMock(
        side_effect=lambda **kw: MagicMock(message_id=next(_msg_counter))
    )
    bot.edit_message_text = AsyncMock()
    return bot


@pytest.fixture
def make_channel(mock_bot):
    """Factory for TelegramChannel with mocked bot."""
    from boxagent.channels.telegram import TelegramChannel

    def _make(on_message=None, tool_calls_display="summary"):
        channel = TelegramChannel(
            token="fake:token",
            allowed_users=[123456],
            tool_calls_display=tool_calls_display,
        )
        channel._bot = mock_bot
        if on_message:
            channel.on_message = on_message
        return channel

    return _make


class TestSendText:
    async def test_send_text_calls_bot(self, make_channel, mock_bot):
        """send_text() calls bot.send_message with correct args."""
        channel = make_channel()
        await channel.send_text("123", "Hello world")

        mock_bot.send_message.assert_called_once_with(
            chat_id="123", text="Hello world", parse_mode="MarkdownV2"
        )

    async def test_long_message_splits(self, make_channel, mock_bot):
        """Messages over 4096 chars are split into multiple sends."""
        channel = make_channel()
        long_text = "A" * 2000 + "\n\n" + "B" * 2000 + "\n\n" + "C" * 2000
        await channel.send_text("123", long_text)

        assert mock_bot.send_message.call_count >= 2

    async def test_markdown_fallback_to_plain(self, make_channel, mock_bot):
        """send_text() retries as plain text when Markdown parsing fails."""
        from aiogram.exceptions import TelegramBadRequest

        channel = make_channel()
        # First call (Markdown) raises, second call (plain) succeeds
        mock_bot.send_message.side_effect = [
            TelegramBadRequest(method="sendMessage", message="can't parse entities"),
            MagicMock(message_id=101),
        ]
        await channel.send_text("123", "bad *markdown")

        assert mock_bot.send_message.call_count == 2
        # Second call should have parse_mode=None
        second_call = mock_bot.send_message.call_args_list[1]
        assert second_call.kwargs.get("parse_mode") is None or \
               (len(second_call.args) == 0 and second_call[1]["parse_mode"] is None)


class TestStreaming:
    async def test_stream_start_sends_initial_message(
        self, make_channel, mock_bot
    ):
        """stream_start() sends placeholder and returns StreamHandle."""
        channel = make_channel()
        handle = await channel.stream_start("123")

        assert isinstance(handle, StreamHandle)
        assert handle.message_id == "100"
        assert handle.chat_id == "123"
        mock_bot.send_message.assert_called_once()

    async def test_stream_update_throttled(self, make_channel, mock_bot):
        """stream_update() throttles edits: max one per 300ms."""
        channel = make_channel()
        handle = StreamHandle(message_id="100", chat_id="123")

        # Send multiple rapid updates
        for i in range(10):
            await channel.stream_update(handle, f"text chunk {i}")

        # Should NOT have called edit 10 times (throttled)
        await asyncio.sleep(0.4)
        assert mock_bot.edit_message_text.call_count < 10

    async def test_stream_end_flushes_and_sends_final(
        self, make_channel, mock_bot
    ):
        """stream_end() cancels pending timer and sends final edit."""
        channel = make_channel()
        handle = StreamHandle(message_id="100", chat_id="123")

        await channel.stream_update(handle, "partial")
        await channel.stream_end(handle)

        mock_bot.edit_message_text.assert_called()
        last_call = mock_bot.edit_message_text.call_args
        assert "partial" in last_call.kwargs.get(
            "text", last_call.args[0] if last_call.args else ""
        )

    async def test_stream_update_flushes_on_char_threshold(
        self, make_channel, mock_bot
    ):
        """If buffer exceeds 200 chars, flush immediately."""
        channel = make_channel()
        handle = StreamHandle(message_id="100", chat_id="123")

        await channel.stream_update(handle, "X" * 250)
        await asyncio.sleep(0.05)
        assert mock_bot.edit_message_text.call_count >= 1


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


def _collect_final_texts(mock_bot) -> list[str]:
    """Extract the final edit_message_text content per message_id."""
    finals: dict[str, str] = {}
    for call in mock_bot.edit_message_text.call_args_list:
        mid = str(call.kwargs.get("message_id", ""))
        text = call.kwargs.get("text", "")
        finals[mid] = text
    return list(finals.values())


class TestStreamSplit:
    """Tests for automatic stream message splitting at Telegram limit."""

    async def test_stream_auto_splits_at_threshold(
        self, make_channel, mock_bot
    ):
        """Buffer exceeding ~3800 chars triggers split to new message."""
        channel = make_channel()
        handle = await channel.stream_start("123")
        original_mid = handle.message_id

        # Feed 4 chunks of 1000 chars each
        for i in range(4):
            await channel.stream_update(handle, f"{i}" * 1000)

        await channel.stream_end(handle)

        # handle should have been updated to a new message
        assert handle.message_id != original_mid
        # At least 2 send_message calls: initial placeholder + split
        assert mock_bot.send_message.call_count >= 2

    async def test_stream_end_after_split(self, make_channel, mock_bot):
        """stream_end() works correctly after a split happened."""
        channel = make_channel()
        handle = await channel.stream_start("123")

        await channel.stream_update(handle, "A" * 5000)
        new_mid = handle.message_id
        # Add more text so stream_end has something to flush
        await channel.stream_update(handle, "B" * 100)
        await channel.stream_end(handle)

        # Final edit should target the new message id
        last_edit = mock_bot.edit_message_text.call_args
        assert str(last_edit.kwargs["message_id"]) == new_mid

        # Internal state should be cleaned up
        assert new_mid not in channel._stream_buffers
        assert new_mid not in channel._stream_last_sent

    async def test_stream_split_respects_code_fence(
        self, make_channel, mock_bot
    ):
        """Split should not break inside a code fence."""
        channel = make_channel()
        handle = await channel.stream_start("123")

        # Build text: open code fence near the limit boundary
        code_block = "```python\n" + "x = 1\n" * 600 + "```\n"
        await channel.stream_update(handle, code_block)
        await channel.stream_update(handle, "after code " * 50)
        await channel.stream_end(handle)

        finals = _collect_final_texts(mock_bot)
        for text in finals:
            fence_count = text.count("```")
            # Each chunk should have balanced fences (0 or even)
            assert fence_count % 2 == 0, (
                f"Unbalanced code fence in chunk ({fence_count} fences): "
                f"{text[:100]}..."
            )

    async def test_stream_split_preserves_all_text(
        self, make_channel, mock_bot
    ):
        """All text is preserved across splits — nothing lost or duplicated."""
        channel = make_channel()
        handle = await channel.stream_start("123")

        original = "".join(f"{i:010d}" for i in range(1000))  # ~10000 chars, pure digits
        # Feed in small pieces
        for i in range(0, len(original), 100):
            await channel.stream_update(handle, original[i : i + 100])
        await channel.stream_end(handle)

        finals = _collect_final_texts(mock_bot)
        reassembled = "".join(finals)
        assert reassembled == original

    async def test_short_stream_no_split(self, make_channel, mock_bot):
        """Short messages don't trigger any split."""
        channel = make_channel()
        handle = await channel.stream_start("123")
        original_mid = handle.message_id

        await channel.stream_update(handle, "short text")
        await channel.stream_end(handle)

        assert handle.message_id == original_mid
        # Only 1 send_message (the initial placeholder)
        assert mock_bot.send_message.call_count == 1

    async def test_tool_call_counts_toward_limit(
        self, make_channel, mock_bot
    ):
        """Tool call text injected via stream_update counts toward the split threshold."""
        channel = make_channel()
        handle = await channel.stream_start("123")
        original_mid = handle.message_id

        await channel.stream_update(handle, "X" * 3500)
        # Simulate tool call text pushed through stream_update
        await channel.stream_update(handle, "\n🔧 Bash\n")
        await channel.stream_update(handle, "Y" * 500)
        await channel.stream_end(handle)

        # Total > 3800, should have split
        assert handle.message_id != original_mid

    async def test_multiple_splits(self, make_channel, mock_bot):
        """Very long output (15000 chars) splits into multiple messages."""
        channel = make_channel()
        handle = await channel.stream_start("123")

        # Feed 15000 chars in 500-char chunks
        total = "A" * 15000
        for i in range(0, len(total), 500):
            await channel.stream_update(handle, total[i : i + 500])
        await channel.stream_end(handle)

        # Should have at least 4 messages (15000 / 3800 ≈ 4)
        assert mock_bot.send_message.call_count >= 4

        # Each final text should be ≤ 4096
        finals = _collect_final_texts(mock_bot)
        for text in finals:
            assert len(text) <= 4096, f"Chunk too long: {len(text)}"

        # All text preserved
        assert "".join(finals) == total


class TestStreamMarkdown:
    """Tests for MarkdownV2 rendering on final stream messages."""

    async def test_stream_end_uses_mdv2(self, make_channel, mock_bot):
        """stream_end sends final edit with parse_mode=MarkdownV2."""
        channel = make_channel()
        handle = await channel.stream_start("123")

        await channel.stream_update(handle, "**bold** text")
        await channel.stream_end(handle)

        # Last edit should have parse_mode=MarkdownV2
        last_edit = mock_bot.edit_message_text.call_args
        assert last_edit.kwargs.get("parse_mode") == "MarkdownV2"

    async def test_stream_end_mdv2_fallback(self, make_channel, mock_bot):
        """stream_end falls back to plain text when MarkdownV2 fails."""
        channel = make_channel()
        handle = await channel.stream_start("123")

        await channel.stream_update(handle, "broken *markdown")

        # First edit (MarkdownV2) raises, second edit (plain) succeeds
        from aiogram.exceptions import TelegramBadRequest
        call_count = [0]

        async def side_effect(**kwargs):
            call_count[0] += 1
            if kwargs.get("parse_mode") == "MarkdownV2":
                raise TelegramBadRequest(
                    method="editMessageText",
                    message="can't parse entities",
                )
            return MagicMock()

        mock_bot.edit_message_text = AsyncMock(side_effect=side_effect)
        await channel.stream_end(handle)

        # Should have retried without parse_mode
        assert call_count[0] == 2

    async def test_split_finalized_with_mdv2(self, make_channel, mock_bot):
        """When stream splits, the finalized old message uses MarkdownV2."""
        channel = make_channel()
        handle = await channel.stream_start("123")

        await channel.stream_update(handle, "**bold** " * 500)
        await channel.stream_end(handle)

        # Find the edit call for the first message (id=100)
        mdv2_calls = [
            c for c in mock_bot.edit_message_text.call_args_list
            if c.kwargs.get("parse_mode") == "MarkdownV2"
        ]
        assert len(mdv2_calls) >= 1
