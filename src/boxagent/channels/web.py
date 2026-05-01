"""Web chat channel — delivers messages to browser clients via SSE queues."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from boxagent.agent_env import ChannelInfo
from boxagent.channels.base import Attachment, IncomingMessage, StreamHandle

logger = logging.getLogger(__name__)


@dataclass
class WebChannel:
    """In-process channel that routes between the Router and browser clients.

    HTTP/SSE routes live in `gateway.py`; this object owns the per-`chat_id`
    fan-out queues and implements the Channel protocol so the Router can
    stream replies back unchanged.
    """

    bot_name: str
    on_message: Callable[[IncomingMessage], Awaitable[None]] | None = None
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
        mid = self._allocate_id()
        self._publish(chat_id, {
            "type": "message",
            "message_id": mid,
            "role": "assistant",
            "text": text,
        })
        return mid

    async def stream_start(self, chat_id: str, **kwargs) -> StreamHandle:
        mid = self._allocate_id()
        self._stream_buffers[mid] = ""
        self._publish(chat_id, {
            "type": "stream_start",
            "message_id": mid,
            "role": "assistant",
        })
        return StreamHandle(message_id=mid, chat_id=chat_id)

    async def stream_update(self, handle: StreamHandle, text: str) -> None:
        mid = handle.message_id
        prev = self._stream_buffers.get(mid, "")
        if text == prev:
            return
        delta = text[len(prev):] if text.startswith(prev) else text
        self._stream_buffers[mid] = text
        self._publish(handle.chat_id, {
            "type": "stream_delta",
            "message_id": mid,
            "delta": delta,
            "text": text,
        })

    async def stream_end(self, handle: StreamHandle) -> str:
        mid = handle.message_id
        text = self._stream_buffers.pop(mid, "")
        self._publish(handle.chat_id, {
            "type": "stream_end",
            "message_id": mid,
            "text": text,
        })
        return mid

    async def show_typing(self, chat_id: str) -> None:
        self._publish(chat_id, {"type": "typing"})

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
        await self.on_message(msg)

    def _allocate_id(self) -> str:
        self._next_msg_id += 1
        return f"web-{self.bot_name}-{self._next_msg_id}"
