"""Base types and protocols for channels."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Protocol

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

    channel: str  # "telegram" / "discord" / "web" (kept for backward compat)
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


class Channel(Protocol):
    """Protocol for messaging channels (Telegram, Web UI, etc.)."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send_text(
        self, chat_id: str, text: str, parse_mode: str = "Markdown",
        **kwargs,
    ) -> str: ...

    async def stream_start(self, chat_id: str, **kwargs) -> StreamHandle: ...
    async def stream_update(self, handle: StreamHandle, text: str) -> None: ...
    async def stream_end(self, handle: StreamHandle) -> str: ...

    async def show_typing(self, chat_id: str) -> None: ...

    on_message: Callable[[IncomingMessage], Awaitable[None]]
