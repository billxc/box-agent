"""Web chat channel — delivers messages to browser clients via SSE queues."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from boxagent.agent_env import ChannelInfo
from boxagent.transports.base import Attachment, Channel, IncomingMessage, StreamHandle

logger = logging.getLogger(__name__)


@dataclass
class WebChannel(Channel):
    """In-process pub/sub for web UI sessions.

    Frontend connects via SSE on /api/stream, server fans out events from
    bots via _publish.
    """

    """In-process channel that routes between the Router and browser clients.

    HTTP/SSE routes live in `gateway.py`; this object owns the per-`chat_id`
    fan-out queues and implements the Channel protocol so the Router can
    stream replies back unchanged.
    """

    bot_name: str
    on_message: Callable[[IncomingMessage], Awaitable[None]] | None = None
    tool_calls_display: str = "summary"
    _subscribers: dict[str, list[asyncio.Queue]] = field(default_factory=dict)
    _stream_buffers: dict[str, str] = field(default_factory=dict)
    _next_msg_id: int = 0

    async def start(self) -> None:  # noqa: D401 — protocol no-op
        return

    async def stop(self) -> None:
        for queues in self._subscribers.values():
            for q in queues:
                q.put_nowait({"type": "_close"})
        self._subscribers.clear()

    # --- subscription management ---

    def subscribe(self, chat_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._subscribers.setdefault(chat_id, []).append(q)
        return q

    def unsubscribe(self, chat_id: str, q: asyncio.Queue) -> None:
        queues = self._subscribers.get(chat_id)
        if not queues:
            return
        try:
            queues.remove(q)
        except ValueError:
            pass
        if not queues:
            self._subscribers.pop(chat_id, None)

    def _publish(self, chat_id: str, event: dict) -> None:
        event.setdefault("ts", time.time())
        for q in self._subscribers.get(chat_id, ()):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("web subscriber queue full (chat_id=%s); dropping event", chat_id)

    # --- Channel protocol ---

    async def send_text(
        self, chat_id: str, text: str, parse_mode: str = "Markdown", **kwargs,
    ) -> str:
        message_id = self._allocate_id()
        self._publish(chat_id, {
            "type": "message",
            "message_id": message_id,
            "role": "assistant",
            "text": text,
        })
        return message_id

    async def stream_start(self, chat_id: str, **kwargs) -> StreamHandle:
        message_id = self._allocate_id()
        self._stream_buffers[message_id] = ""
        self._publish(chat_id, {
            "type": "stream_start",
            "message_id": message_id,
            "role": "assistant",
        })
        return StreamHandle(message_id=message_id, chat_id=chat_id)

    async def stream_update(self, handle: StreamHandle, text: str) -> None:
        """Append `text` to the message buffer and emit a delta event.

        Router callback passes incremental chunks (matching Telegram's
        contract), not the full accumulated message — so we accumulate here.
        """
        message_id = handle.message_id
        if not text:
            return
        prev = self._stream_buffers.get(message_id, "")
        new_full = prev + text
        self._stream_buffers[message_id] = new_full
        self._publish(handle.chat_id, {
            "type": "stream_delta",
            "message_id": message_id,
            "delta": text,
            "text": new_full,
        })

    async def stream_end(self, handle: StreamHandle) -> str:
        message_id = handle.message_id
        text = self._stream_buffers.pop(message_id, "")
        self._publish(handle.chat_id, {
            "type": "stream_end",
            "message_id": message_id,
            "text": text,
        })
        return message_id

    async def show_typing(self, chat_id: str) -> None:
        self._publish(chat_id, {"type": "typing"})

    # ── Polymorphic tool-call rendering ──
    # ChannelCallback delegates here; WebChannel publishes structured events
    # consumed by the frontend's renderToolCall / renderToolResult.

    async def on_tool_call(
        self, chat_id: str, tool_id: str, name: str, input: dict, result: str,
        *, stream_handle=None, webhook_name: str = "", parent_tool_id: str = "",
    ) -> bool:
        """Publish a tool_call card event. If ``result`` is non-empty (Codex
        single-shot), immediately publish the matching tool_result too."""
        self._publish(chat_id, {
            "type": "tool_call",
            "tool_id": tool_id or self._allocate_id(),
            "name": name,
            "args": input,
            "parent_tool_id": parent_tool_id,
        })
        if result:
            self._publish(chat_id, {
                "type": "tool_result",
                "tool_id": tool_id,
                "ok": True,
                "summary": result[:200],
                "parent_tool_id": parent_tool_id,
            })
        return False  # never streams into a text handle

    async def on_tool_update(
        self, chat_id: str, tool_call_id: str, title: str,
        status: str | None = None, input: object = None, output: object = None,
        *, stream_handle=None, webhook_name: str = "", parent_tool_id: str = "",
    ) -> bool:
        """Map a tool lifecycle update to a structured tool_result event."""
        if status == "completed":
            self._publish(chat_id, {
                "type": "tool_result",
                "tool_id": tool_call_id,
                "ok": True,
                "summary": str(output)[:200] if output else "",
                "parent_tool_id": parent_tool_id,
            })
        elif status == "failed":
            self._publish(chat_id, {
                "type": "tool_result",
                "tool_id": tool_call_id,
                "ok": False,
                "error": str(output)[:200] if output else title,
                "parent_tool_id": parent_tool_id,
            })
        # pending / in_progress: nothing to render on result side.
        return False

    # --- Inbound from HTTP layer ---

    async def inject(
        self,
        chat_id: str,
        text: str,
        user_id: str = "web",
        attachments: list[Attachment] | None = None,
    ) -> None:
        if not self.on_message:
            raise RuntimeError("WebChannel.on_message is not wired")
        # Echo the user's own message to other tabs viewing the same chat_id
        self._publish(chat_id, {
            "type": "message",
            "message_id": self._allocate_id(),
            "role": "user",
            "text": text,
        })
        msg = IncomingMessage(
            channel="web",
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            attachments=attachments or [],
            trusted=True,
            channel_info=ChannelInfo(platform="web"),
        )
        # Reply streams back over the SSE channel, so /api/send must not block
        # on the turn: cross-machine the POST is relayed with a 30s cap, so a
        # long reply returned 504 "host timeout" and the whole turn was lost.
        asyncio.create_task(self.on_message(msg))

    def _allocate_id(self) -> str:
        self._next_msg_id += 1
        return f"web-{self.bot_name}-{self._next_msg_id}"
