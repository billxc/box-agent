"""Regression test for late ACP stream chunks arriving after router close."""

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from boxagent.channels.base import IncomingMessage
from boxagent.router import Router


@dataclass
class _FakeLateChunkBackend:
    """Backend that returns before all stream callbacks finish."""

    session_id: str = "sess-late"
    state: str = "idle"
    supports_session_persistence: bool = False
    late_task: asyncio.Task | None = field(default=None, init=False)

    async def send(self, prompt, callback, model="", chat_id=""):
        self.state = "busy"
        await callback.on_stream("到")

        late_chunks = [
            "上一",
            "条",
            "为",
            "止",
            "，我们",
            "已经",
            "来",
            "回",
            "说",
            "了",
            " `",
            "10",
            "`",
            " ",
            "轮",
            "。\n\n",
            "如果",
            "把",
            "我",
            "现在",
            "这",
            "条",
            "也",
            "算",
            "上",
            "，就是",
            "第",
            " `",
            "11",
            "`",
            " ",
            "轮",
            "。",
        ]

        async def _emit_late_chunks():
            await asyncio.sleep(0)
            for chunk in late_chunks:
                await callback.on_stream(chunk)

        self.late_task = asyncio.create_task(_emit_late_chunks())
        self.state = "idle"

    async def drain_output(self):
        if self.late_task is not None:
            await self.late_task


def _msg(text: str) -> IncomingMessage:
    return IncomingMessage(
        channel="telegram",
        chat_id="123",
        user_id="123",
        text=text,
    )


@pytest.mark.asyncio
async def test_router_should_not_truncate_late_stream_chunks(tmp_path: Path, caplog):
    backend = _FakeLateChunkBackend()
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
        local_dir=tmp_path,
    )

    expected = (
        "到上一条为止，我们已经来回说了 `10` 轮。\n\n"
        "如果把我现在这条也算上，就是第 `11` 轮。"
    )

    with caplog.at_level("WARNING", logger="boxagent.router"):
        await router.handle_message(_msg("我们说过几轮"))
        assert backend.late_task is not None
        await backend.late_task

    transcript = tmp_path / "transcripts" / "sess-late.jsonl"
    rows = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["event"] == "assistant"
    assert rows[-1]["text"] == expected
    assert not any(
        "Late stream chunk ignored" in rec.message for rec in caplog.records
    )
