"""Callback adapters for routing agent output to Telegram channels."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TextCollector:
    """Minimal callback that just collects text output (used by /compact)."""
    text: str = ""

    async def start_typing(self):
        pass

    def _stop_typing(self):
        pass

    async def on_stream(self, text: str) -> None:
        self.text += text

    async def on_tool_call(self, name: str, input: dict, result: str):
        pass

    async def on_tool_update(
        self,
        tool_call_id: str,
        title: str,
        status: str | None = None,
        input: object = None,
        output: object = None,
    ):
        pass

    async def on_error(self, error: str):
        pass

    async def on_file(self, path: str, caption: str = ""):
        pass

    async def on_image(self, path: str, caption: str = ""):
        pass


@dataclass
class ChannelCallback:
    """Routes agent streaming output to a Telegram channel."""
    channel: object
    chat_id: str
    _handle: object = None
    _typing_task: object = None
    _closed: bool = False
    collected_text: str = ""
    _stream_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _tool_started_shown: set[str] = field(default_factory=set)
    _late_stream_warned: bool = False
    _needs_paragraph_break_after_tool: bool = False

    async def start_typing(self):
        """Start a background loop that sends typing every 4s.

        Safe to call multiple times — stops any existing loop first.
        """
        if self._closed:
            return
        self._stop_typing()

        async def _loop():
            try:
                while True:
                    try:
                        await self.channel.show_typing(self.chat_id)
                    except Exception as e:
                        logger.warning("Typing failed: %s", e)
                    await asyncio.sleep(4)
            except asyncio.CancelledError:
                pass

        self._typing_task = asyncio.create_task(_loop())

    def _stop_typing(self):
        if self._typing_task and not self._typing_task.done():
            self._typing_task.cancel()
            self._typing_task = None

    async def close(self):
        """Stop background activity and ignore any late callback events."""
        self._closed = True
        self._stop_typing()
        if self._handle:
            try:
                await self.channel.stream_end(self._handle)
            except Exception:
                pass
            self._handle = None

    async def on_stream(self, text: str) -> None:
        if self._closed:
            if not self._late_stream_warned:
                logger.warning(
                    "Late stream chunk arrived after close; suppressing additional warnings (chat_id=%s)",
                    self.chat_id,
                )
                self._late_stream_warned = True
            return
        async with self._stream_lock:
            if self._closed:
                if not self._late_stream_warned:
                    logger.warning(
                        "Late stream chunk arrived after close; suppressing additional warnings (chat_id=%s)",
                        self.chat_id,
                    )
                    self._late_stream_warned = True
                return
            prefix = ""
            if self._needs_paragraph_break_after_tool and text.strip():
                prefix = "\n\n"
                self._needs_paragraph_break_after_tool = False
            self.collected_text += prefix + text
            self._stop_typing()
            if self._handle is None:
                self._handle = await self.channel.stream_start(self.chat_id)
            await self.channel.stream_update(self._handle, prefix + text)

    async def on_tool_call(self, name: str, input: dict, result: str):
        if self._closed:
            return
        if hasattr(self.channel, "format_tool_call"):
            fmt = self.channel.format_tool_call(name, input)
            if fmt:
                if self._handle:
                    await self.channel.stream_update(
                        self._handle, f"\n{fmt}\n"
                    )
                    self._needs_paragraph_break_after_tool = True
                else:
                    await self.channel.send_text(
                        self.chat_id, fmt, parse_mode=None
                    )
        # Restart typing — tool execution may take a long time
        await self.start_typing()

    async def on_tool_update(
        self,
        tool_call_id: str,
        title: str,
        status: str | None = None,
        input: object = None,
        output: object = None,
    ):
        if self._closed:
            return

        if status in {"pending", "in_progress"}:
            if tool_call_id in self._tool_started_shown:
                return
            self._tool_started_shown.add(tool_call_id)
            icon = "🔧"
        elif status == "completed":
            icon = "✅"
        elif status == "failed":
            icon = "❌"
        else:
            return

        display_text = title
        if hasattr(self.channel, "format_tool_update"):
            display_text = self.channel.format_tool_update(
                title,
                status=status,
                input=input,
                output=output,
            )

        if display_text:
            fmt = f"{icon} {display_text}"
            if self._handle:
                await self.channel.stream_update(self._handle, f"\n{fmt}\n")
                self._needs_paragraph_break_after_tool = True
            else:
                await self.channel.send_text(
                    self.chat_id, fmt, parse_mode=None
                )
        await self.start_typing()

    async def on_error(self, error: str):
        if self._closed:
            return
        self._closed = True
        self._stop_typing()
        if self._handle:
            await self.channel.stream_end(self._handle)
            self._handle = None
        await self.channel.send_text(self.chat_id, f"Error: {error}")

    async def on_file(self, path: str, caption: str = ""):
        pass

    async def on_image(self, path: str, caption: str = ""):
        pass


def log_turn(path: Path, bot: str, chat_id: str, user_text: str, assistant_text: str):
    """Append user + assistant records to a JSONL transcript file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for event, text in [("user", user_text), ("assistant", assistant_text)]:
                record = json.dumps(
                    {"ts": time.time(), "bot": bot, "chat_id": chat_id,
                     "event": event, "text": text},
                    ensure_ascii=False,
                )
                f.write(record + "\n")
    except Exception:
        logger.exception("Failed to write transcript")
