"""Router-level integration tests for /cancel behavior."""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

from boxagent.channels.base import IncomingMessage
from boxagent.router import Router


@dataclass
class _FakeBusyBackend:
    """Small controllable backend for router-level /cancel tests."""

    state: str = "idle"
    session_id: str | None = None
    supports_session_persistence: bool = False

    def __post_init__(self):
        self._entered_busy = asyncio.Event()
        self._cancelled = asyncio.Event()
        self.send_calls = []
        self.cancel_calls = 0

    async def send(self, prompt, callback, model="", chat_id="", append_system_prompt=""):
        self.send_calls.append(prompt)
        self.state = "busy"
        self._entered_busy.set()
        await callback.on_stream("working...")
        await self._cancelled.wait()
        self.state = "idle"

    async def cancel(self):
        self.cancel_calls += 1
        self._cancelled.set()

    async def wait_until_busy(self):
        await asyncio.wait_for(self._entered_busy.wait(), timeout=2)


def _msg(text: str) -> IncomingMessage:
    return IncomingMessage(
        channel="telegram",
        chat_id="123",
        user_id="123",
        text=text,
    )


class TestRouterCancelIntegration:
    async def test_cancel_interrupts_inflight_turn_via_router(self):
        backend = _FakeBusyBackend()
        channel = AsyncMock()
        channel.send_text = AsyncMock()
        channel.show_typing = AsyncMock()
        channel.stream_start = AsyncMock(
            return_value=SimpleNamespace(message_id="m1", chat_id="123")
        )
        channel.stream_update = AsyncMock()
        channel.stream_end = AsyncMock()
        channel.format_tool_call = lambda name, inp: ""

        router = Router(
            cli_process=backend,
            channel=channel,
            allowed_users=[123],
            bot_name="test-bot",
            display_name="Demo Bot",
        )

        prompt_task = asyncio.create_task(
            router.handle_message(_msg("run a long task"))
        )
        await backend.wait_until_busy()
        assert backend.state == "busy"

        await router.handle_message(_msg("/cancel"))
        await asyncio.wait_for(prompt_task, timeout=2)

        assert backend.cancel_calls == 1
        assert backend.state == "idle"
        channel.send_text.assert_any_call("123", "Cancelled current task.")
        channel.stream_update.assert_called()
        channel.stream_end.assert_called_once()
