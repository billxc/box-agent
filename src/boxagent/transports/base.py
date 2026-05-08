"""Base types and protocols for channels."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Protocol, runtime_checkable

from boxagent.agent_env import ChannelInfo


@dataclass
class Attachment:
    """File attached to an incoming message."""

    type: str  # "image" / "file" / "voice" / "video"
    file_path: str  # Downloaded to local temp path
    file_name: str  # Original filename
    mime_type: str  # "image/png", "application/pdf", etc.
    size: int  # Bytes


@dataclass
class IncomingMessage:
    """Message received from a channel."""

    channel: str  # "telegram" / "web" (kept for backward compat)
    chat_id: str
    user_id: str
    text: str
    attachments: list[Attachment] = field(default_factory=list)
    reply_to: str | None = None
    via_workgroup: bool = False  # True when routed through workgroup delegation
    trusted: bool = False  # True for internal messages (skip auth check)
    timestamp: datetime = field(default_factory=datetime.now)
    channel_info: ChannelInfo | None = None  # Rich channel metadata


@dataclass
class StreamHandle:
    """Handle for an in-progress streaming message on a channel."""

    message_id: str
    chat_id: str
    webhook_name: str = ""  # bot name for webhook-based bus replies


@runtime_checkable
class Channel(Protocol):
    """Protocol for messaging channels (Telegram, Web UI, etc.).

    Routers talk to channels through this surface. Each concrete channel
    adds its own internal helpers (``inject`` for web, ``send_text_with_inline_keyboard``
    for Telegram, etc.) but the Router never reaches past this Protocol.

    Inbound: the channel sets up its own listener (Telegram polling, web
    SSE inject, etc.) and calls ``self.on_message(msg)`` when a message
    arrives. Outbound: the Router uses ``send_text`` / ``stream_*`` /
    ``on_tool_*`` to push content back.
    """

    # ── Inbound message wiring ──
    # Set externally (Router.handle_message). Channel invokes it on each
    # incoming message it receives.
    on_message: Callable[[IncomingMessage], Awaitable[None]]

    # ── Lifecycle ──
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    # ── Outbound: plain text ──
    async def send_text(
        self, chat_id: str, text: str, parse_mode: str = "Markdown",
        **kwargs,
    ) -> str:
        """Send a single message. Returns a channel-native message id."""
        ...

    # ── Outbound: streaming (incremental assistant output) ──
    async def stream_start(self, chat_id: str, **kwargs) -> StreamHandle:
        """Open a stream slot. Returns a handle for subsequent updates."""
        ...

    async def stream_update(self, handle: StreamHandle, text: str) -> None:
        """Append (or replace, channel-defined) the streaming buffer."""
        ...

    async def stream_end(self, handle: StreamHandle) -> str:
        """Finalize the stream. Returns the final message id."""
        ...

    # ── Typing indicator ──
    async def show_typing(self, chat_id: str) -> None:
        """Display the channel's 'typing…' affordance, if it has one."""
        ...

    # ── Tool call display (channels render their own way) ──
    # Both methods return True iff the channel rendered the tool into the
    # active stream — ChannelCallback uses that to decide whether the next
    # assistant chunk needs a paragraph break.

    async def on_tool_call(
        self,
        chat_id: str,
        tool_id: str,
        name: str,
        input: dict,
        result: str,
        *,
        stream_handle: StreamHandle | None = None,
        webhook_name: str = "",
    ) -> bool:
        """Render a tool call (paired call+result, fired once at completion)."""
        ...

    async def on_tool_update(
        self,
        chat_id: str,
        tool_call_id: str,
        title: str,
        *,
        status: str | None = None,
        input: object = None,
        output: object = None,
        stream_handle: StreamHandle | None = None,
        webhook_name: str = "",
    ) -> bool:
        """Render a tool lifecycle update (status: pending/in_progress/completed/failed)."""
        ...
