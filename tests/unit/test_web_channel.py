"""Unit tests for WebChannel (publish-only; browsers subscribe via ChatBus/bus)."""

import asyncio
import pytest

from boxagent.bus.subscriber import QueueSubscriber
from boxagent.transports.base import IncomingMessage, StreamHandle
from boxagent.transports.web import WebChannel


@pytest.fixture
def channel():
    return WebChannel(bot_name="bot1")


def _observe(channel, chat_id: str = "c1"):
    """Subscribe to a chat topic on the channel's bus the way ChatBus does;
    return (queue, subscription). WebChannel itself owns no queues now."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
    subscription = channel.message_bus.subscribe(
        channel._topic(chat_id), QueueSubscriber(queue, chat_id),
    )
    return queue, subscription


async def _drain(q: asyncio.Queue, n: int, timeout: float = 1.0) -> list[dict]:
    out: list[dict] = []
    for _ in range(n):
        out.append(await asyncio.wait_for(q.get(), timeout=timeout))
    return out


class TestSubscribe:
    async def test_bus_subscription_lifecycle(self, channel):
        queue, subscription = _observe(channel, "c1")
        await channel.send_text("c1", "hi")
        assert (await asyncio.wait_for(queue.get(), timeout=0.5))["text"] == "hi"
        # A closed subscription receives nothing further.
        subscription.close()
        await channel.send_text("c1", "again")
        assert queue.empty()

    async def test_subscription_close_is_idempotent(self, channel):
        _queue, subscription = _observe(channel, "c1")
        subscription.close()
        subscription.close()  # no-op, no raise

    async def test_publish_only_to_matching_chat(self, channel):
        q1, _s1 = _observe(channel, "c1")
        q2, _s2 = _observe(channel, "c2")
        await channel.send_text("c1", "hi")
        msg = await asyncio.wait_for(q1.get(), timeout=0.5)
        assert msg["text"] == "hi"
        assert q2.empty()


class TestStream:
    async def test_send_text_emits_event(self, channel):
        q, _s = _observe(channel, "c1")
        message_id = await channel.send_text("c1", "hello")
        ev = await asyncio.wait_for(q.get(), timeout=0.5)
        assert ev["type"] == "message"
        assert ev["role"] == "assistant"
        assert ev["text"] == "hello"
        assert ev["message_id"] == message_id

    async def test_stream_lifecycle_emits_events(self, channel):
        q, _s = _observe(channel, "c1")
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
        q, _s = _observe(channel, "c1")
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
        # Dispatch is fire-and-forget; let the spawned turn run.
        await asyncio.sleep(0)
        assert len(received) == 1
        msg = received[0]
        assert msg.channel == "web"
        assert msg.chat_id == "c1"
        assert msg.text == "hi from web"
        assert msg.trusted is True
        assert msg.channel_info.platform == "web"

    async def test_inject_returns_before_slow_turn_completes(self, channel):
        """Regression: /api/send must not block on the full turn (cross-machine
        the POST is capped at 30s → long replies were 504'd and dropped)."""
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(_):
            started.set()
            await release.wait()

        channel.on_message = slow
        # Returns immediately even though the turn is still running.
        await asyncio.wait_for(channel.inject(chat_id="c1", text="x"), timeout=0.5)
        await asyncio.wait_for(started.wait(), timeout=0.5)  # turn runs in background
        release.set()
        await asyncio.sleep(0)  # let the background turn finish

    async def test_inject_echoes_user_event(self, channel):
        async def on_msg(_):
            pass
        channel.on_message = on_msg
        q, _s = _observe(channel, "c1")
        await channel.inject(chat_id="c1", text="hello")
        ev = await asyncio.wait_for(q.get(), timeout=0.5)
        assert ev["role"] == "user"
        assert ev["text"] == "hello"

    async def test_inject_without_handler_raises(self, channel):
        with pytest.raises(RuntimeError):
            await channel.inject(chat_id="c1", text="x")


class TestShowTyping:
    async def test_show_typing_emits_event(self, channel):
        q, _s = _observe(channel, "c1")
        await channel.show_typing("c1")
        ev = await asyncio.wait_for(q.get(), timeout=0.5)
        assert ev["type"] == "typing"


class TestStop:
    async def test_stop_is_clean_and_clears_buffers(self, channel):
        _q, _s = _observe(channel, "c1")
        h = await channel.stream_start("c1")
        await channel.stream_update(h, "partial")
        await channel.stop()  # publish-only: no _close to fan out, just clean up
        assert channel._stream_buffers == {}
