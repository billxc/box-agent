"""Unit tests for WebChannel."""

import asyncio
import pytest

from boxagent.transports.base import IncomingMessage, StreamHandle
from boxagent.transports.web import WebChannel


@pytest.fixture
def channel():
    return WebChannel(bot_name="bot1")


async def _drain(q: asyncio.Queue, n: int, timeout: float = 1.0) -> list[dict]:
    out: list[dict] = []
    for _ in range(n):
        out.append(await asyncio.wait_for(q.get(), timeout=timeout))
    return out


class TestSubscribe:
    async def test_subscribe_unsubscribe_lifecycle(self, channel):
        q = channel.subscribe("c1")
        assert "c1" in channel._subscribers
        channel.unsubscribe("c1", q)
        assert "c1" not in channel._subscribers

    async def test_unsubscribe_unknown_no_raise(self, channel):
        channel.unsubscribe("c1", asyncio.Queue())  # no-op

    async def test_publish_only_to_matching_chat(self, channel):
        q1 = channel.subscribe("c1")
        q2 = channel.subscribe("c2")
        await channel.send_text("c1", "hi")
        msg = await asyncio.wait_for(q1.get(), timeout=0.5)
        assert msg["text"] == "hi"
        assert q2.empty()


class TestStream:
    async def test_send_text_emits_event(self, channel):
        q = channel.subscribe("c1")
        message_id = await channel.send_text("c1", "hello")
        ev = await asyncio.wait_for(q.get(), timeout=0.5)
        assert ev["type"] == "message"
        assert ev["role"] == "assistant"
        assert ev["text"] == "hello"
        assert ev["message_id"] == message_id

    async def test_stream_lifecycle_emits_events(self, channel):
        q = channel.subscribe("c1")
        h = await channel.stream_start("c1")
        await channel.stream_update(h, "Hel")
        await channel.stream_update(h, "lo")
        await channel.stream_end(h)
        events = await _drain(q, 4)
        assert [e["type"] for e in events] == [
            "stream_start", "stream_delta", "stream_delta", "stream_end",
        ]
        assert events[1]["delta"] == "Hel"
        assert events[1]["text"] == "Hel"
        assert events[2]["delta"] == "lo"
        assert events[2]["text"] == "Hello"
        assert events[3]["text"] == "Hello"

    async def test_stream_update_dedupe_same_text(self, channel):
        # Empty chunk is a no-op (router never sends empty deltas in practice)
        q = channel.subscribe("c1")
        h = await channel.stream_start("c1")
        await channel.stream_update(h, "abc")
        await channel.stream_update(h, "")  # no-op
        await channel.stream_end(h)
        events = await _drain(q, 3)
        assert [e["type"] for e in events] == ["stream_start", "stream_delta", "stream_end"]


class TestInject:
    async def test_inject_dispatches_incoming(self, channel):
        received: list[IncomingMessage] = []

        async def on_msg(m):
            received.append(m)

        channel.on_message = on_msg
        await channel.inject(chat_id="c1", text="hi from web")
        assert len(received) == 1
        msg = received[0]
        assert msg.channel == "web"
        assert msg.chat_id == "c1"
        assert msg.text == "hi from web"
        assert msg.trusted is True
        assert msg.channel_info.platform == "web"

    async def test_inject_echoes_user_event(self, channel):
        async def on_msg(_):
            pass
        channel.on_message = on_msg
        q = channel.subscribe("c1")
        await channel.inject(chat_id="c1", text="hello")
        ev = await asyncio.wait_for(q.get(), timeout=0.5)
        assert ev["role"] == "user"
        assert ev["text"] == "hello"

    async def test_inject_without_handler_raises(self, channel):
        with pytest.raises(RuntimeError):
            await channel.inject(chat_id="c1", text="x")


class TestShowTyping:
    async def test_show_typing_emits_event(self, channel):
        q = channel.subscribe("c1")
        await channel.show_typing("c1")
        ev = await asyncio.wait_for(q.get(), timeout=0.5)
        assert ev["type"] == "typing"


class TestStop:
    async def test_stop_signals_close(self, channel):
        q = channel.subscribe("c1")
        await channel.stop()
        ev = await asyncio.wait_for(q.get(), timeout=0.5)
        assert ev["type"] == "_close"
        assert channel._subscribers == {}
