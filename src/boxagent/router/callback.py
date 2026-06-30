"""Callback adapters for routing agent output to Telegram channels."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from boxagent.transports.base import Channel, StreamHandle

logger = logging.getLogger(__name__)


@dataclass
class TextCollector:
    """Minimal callback that just collects text output (used by /compact)."""
    text: str = ""

    async def start_typing(self):
        pass

    def _stop_typing(self):
        pass

    async def on_stream(self, text: str, parent_tool_id: str = "") -> None:
        self.text += text

    async def on_tool_call(self, name: str, input: dict, result: str, tool_id: str = ""):
        pass

    async def on_tool_update(
        self,
        tool_call_id: str,
        title: str,
        status: str | None = None,
        input=None,
        output=None,
    ):
        pass

    async def on_error(self, error: str):
        pass

    async def on_file(self, path: str, caption: str = ""):
        pass

    async def on_image(self, path: str, caption: str = ""):
        pass

    async def on_compact_event(self, event: str, metadata: dict | None = None):
        pass


@dataclass
class ChannelCallback:
    """Routes agent streaming output to a channel."""
    channel: "Channel"
    chat_id: str
    webhook_name: str = ""  # bot name for webhook-based bus replies
    _handle: "StreamHandle | None" = None
    _typing_task: asyncio.Task | None = None
    _closed: bool = False
    collected_text: str = ""
    _stream_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
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

    async def on_stream(self, text: str, parent_tool_id: str = "") -> None:
        if self._closed:
            if not self._late_stream_warned:
                logger.warning(
                    "Late stream chunk arrived after close; suppressing additional warnings (chat_id=%s)",
                    self.chat_id,
                )
                self._late_stream_warned = True
            return
        # Subagent text (from a Task spawn) is internal chatter — would
        # pollute the main assistant bubble if appended. Drop it on the
        # floor; the parent Task tool result is what surfaces the
        # subagent's outcome.
        if parent_tool_id:
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
                self._handle = await self.channel.stream_start(
                    self.chat_id, webhook_name=self.webhook_name,
                )
            await self.channel.stream_update(self._handle, prefix + text)

    async def on_tool_call(
        self, name: str, input: dict, result: str, tool_id: str = "",
        parent_tool_id: str = "",
    ):
        if self._closed:
            return
        # Polymorphic: each channel renders tool calls its own way.
        # Returns True iff the channel emitted a stream update; in that case
        # we must insert a paragraph break before further assistant text.
        used_stream = await self.channel.on_tool_call(
            self.chat_id, tool_id, name, input, result,
            stream_handle=self._handle, webhook_name=self.webhook_name,
            parent_tool_id=parent_tool_id,
        )
        if used_stream:
            self._needs_paragraph_break_after_tool = True
        # Tool execution may take a while — restart typing indicator.
        await self.start_typing()

    async def on_tool_update(
        self,
        tool_call_id: str,
        title: str,
        status: str | None = None,
        input: object = None,
        output: object = None,
        parent_tool_id: str = "",
    ):
        if self._closed:
            return
        used_stream = await self.channel.on_tool_update(
            self.chat_id, tool_call_id, title,
            status=status, input=input, output=output,
            stream_handle=self._handle, webhook_name=self.webhook_name,
            parent_tool_id=parent_tool_id,
        )
        if used_stream:
            self._needs_paragraph_break_after_tool = True
        await self.start_typing()

    async def on_error(self, error: str):
        if self._closed:
            return
        self._closed = True
        self._stop_typing()
        if self._handle:
            await self.channel.stream_end(self._handle)
            self._handle = None
        await self.channel.send_text(
            self.chat_id, f"Error: {error}",
            webhook_name=self.webhook_name,
        )

    async def on_file(self, path: str, caption: str = ""):
        pass

    async def on_image(self, path: str, caption: str = ""):
        pass

    async def on_compact_event(self, event: str, metadata: dict | None = None):
        """Surface autocompact lifecycle to the user.

        SDK autocompact takes ~2 minutes; without a notification the chat
        looks frozen. Send "compacting..." on start and the actual ratio
        on boundary (which carries the metadata).
        """
        if self._closed:
            return
        if event == "compacting":
            try:
                await self.channel.send_text(
                    self.chat_id,
                    "⏳ Context full — Claude is auto-compacting (this may take a minute)...",
                    webhook_name=self.webhook_name,
                )
            except Exception:
                logger.exception("Failed to send compacting notice")
        elif event == "boundary" and metadata:
            pre = int(metadata.get("pre_tokens", 0) or 0)
            post = int(metadata.get("post_tokens", 0) or 0)
            duration_s = int((metadata.get("duration_ms", 0) or 0) / 1000)
            trigger = metadata.get("trigger", "auto")
            try:
                await self.channel.send_text(
                    self.chat_id,
                    f"✅ Compacted ({trigger}): {pre // 1000}k → {post // 1000}k "
                    f"tokens in {duration_s}s",
                    webhook_name=self.webhook_name,
                )
            except Exception:
                logger.exception("Failed to send compacted notice")


def log_turn(path: Path, bot: str, chat_id: str, user_text: str, assistant_text: str):
    """Append user + assistant records to a JSONL transcript file.

    Also emits a lightweight `agent.turn` event into the event log
    (summary only — full text stays in the transcript file)."""
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

    from boxagent.log import Category, log

    log.info(
        Category.AGENT_TURN,
        f"turn: {bot}/{chat_id}",
        bot=bot,
        chat_id=chat_id,
        user_len=len(user_text),
        assistant_len=len(assistant_text),
    )
