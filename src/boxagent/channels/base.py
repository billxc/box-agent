"""Base types and protocols for channels."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Protocol


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

    channel: str  # "telegram" / "web"
    chat_id: str
    user_id: str
    text: str
    attachments: list[Attachment] = field(default_factory=list)
    reply_to: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class StreamHandle:
    """Handle for an in-progress streaming message on a channel."""

    message_id: str
    chat_id: str


class Channel(Protocol):
    """Protocol for messaging channels (Telegram, Web UI, etc.)."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send_text(
        self, chat_id: str, text: str, parse_mode: str = "Markdown"
    ) -> str: ...

    async def stream_start(self, chat_id: str) -> StreamHandle: ...
    async def stream_update(self, handle: StreamHandle, text: str) -> None: ...
    async def stream_end(self, handle: StreamHandle) -> str: ...

    async def show_typing(self, chat_id: str) -> None: ...

    on_message: Callable[[IncomingMessage], Awaitable[None]]
