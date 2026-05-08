"""Tests for Channel protocol + MockChannel test double."""

import pytest

from boxagent.transports.base import Channel, IncomingMessage, StreamHandle
from boxagent.testing.mocks import (
    MockChannel,
    StreamRecord,
    ToolCallRecord,
    ToolUpdateRecord,
)


class TestProtocol:
    def test_mock_satisfies_runtime_protocol(self):
        channel = MockChannel()
        assert isinstance(channel, Channel)

    def test_telegram_satisfies_protocol(self):
        from boxagent.transports.telegram.channel import TelegramChannel
        ch = TelegramChannel(token="123:ABC", allowed_users=[1])
        assert isinstance(ch, Channel)

    def test_web_satisfies_protocol(self):
        from boxagent.transports.web.channel import WebChannel
        ch = WebChannel(bot_name="t")
        assert isinstance(ch, Channel)


class TestMockChannel:
    @pytest.mark.asyncio
    async def test_send_text_records(self):
        ch = MockChannel()
        msg_id = await ch.send_text("123", "hello")
        assert ch.sent_texts == [("123", "hello")]
        assert msg_id == "mock-1"

    @pytest.mark.asyncio
    async def test_stream_lifecycle(self):
        ch = MockChannel()
        handle = await ch.stream_start("123")
        await ch.stream_update(handle, "alpha")
        await ch.stream_update(handle, "alpha beta")
        msg_id = await ch.stream_end(handle)

        assert msg_id == "mock-1"
        assert len(ch.streams) == 1
        s = ch.streams[0]
        assert s.chat_id == "123"
        assert s.chunks == ["alpha", "alpha beta"]
        assert s.final_text == "alpha beta"
        assert s.closed is True

    @pytest.mark.asyncio
    async def test_tool_call_recorded(self):
        ch = MockChannel()
        used = await ch.on_tool_call(
            "123", "tool-1", "Bash", {"cmd": "ls"}, "out",
        )
        assert used is False
        assert ch.tool_calls == [ToolCallRecord(
            chat_id="123", tool_id="tool-1", name="Bash",
            input={"cmd": "ls"}, result="out",
        )]

    @pytest.mark.asyncio
    async def test_tool_update_recorded(self):
        ch = MockChannel()
        used = await ch.on_tool_update(
            "123", "tc-7", "Run pwd", status="completed", output="/tmp",
        )
        assert used is False
        assert ch.tool_updates == [ToolUpdateRecord(
            chat_id="123", tool_call_id="tc-7", title="Run pwd",
            status="completed", input=None, output="/tmp",
        )]

    @pytest.mark.asyncio
    async def test_tool_methods_can_signal_stream_break(self):
        ch = MockChannel(tool_call_uses_stream=True, tool_update_uses_stream=True)
        used_call = await ch.on_tool_call("c", "t", "n", {}, "r")
        used_update = await ch.on_tool_update("c", "t", "title")
        assert used_call is True
        assert used_update is True

    @pytest.mark.asyncio
    async def test_typing_recorded(self):
        ch = MockChannel()
        await ch.show_typing("123")
        await ch.show_typing("456")
        assert ch.typing_calls == ["123", "456"]

    @pytest.mark.asyncio
    async def test_lifecycle(self):
        ch = MockChannel()
        await ch.start()
        await ch.stop()
        assert ch.started is True
        assert ch.stopped is True

    @pytest.mark.asyncio
    async def test_deliver_invokes_on_message(self):
        ch = MockChannel()
        received: list[IncomingMessage] = []

        async def handler(msg):
            received.append(msg)

        ch.on_message = handler
        msg = IncomingMessage(
            channel="mock", chat_id="123", user_id="42", text="hi",
        )
        await ch.deliver(msg)
        assert received == [msg]

    @pytest.mark.asyncio
    async def test_deliver_without_handler_raises(self):
        ch = MockChannel()
        msg = IncomingMessage(
            channel="mock", chat_id="123", user_id="42", text="hi",
        )
        with pytest.raises(RuntimeError, match="on_message is unset"):
            await ch.deliver(msg)
