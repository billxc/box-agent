"""Telegram channel — send/receive messages via aiogram 3."""

import asyncio
import json
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.types import Message as AiogramMessage

from boxagent.channels.base import Attachment, IncomingMessage, StreamHandle
from boxagent.channels.mdv2 import md_to_mdv2 as _md_to_mdv2
from boxagent.channels.splitter import split_message, _find_split_point

logger = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096
THROTTLE_MS = 300
FLUSH_CHAR_THRESHOLD = 200
STREAM_SPLIT_THRESHOLD = 3800  # Leave ~300 char margin for Telegram limit


@dataclass
class TelegramChannel:
    """Telegram bot channel using aiogram 3."""

    token: str
    allowed_users: list[int]
    tool_calls_display: str = "summary"
    on_message: object = None

    _bot: Bot | None = field(default=None, repr=False)
    _dp: Dispatcher | None = field(default=None, repr=False)
    _polling_task: asyncio.Task | None = field(default=None, repr=False)
    _stream_buffers: dict[str, str] = field(default_factory=dict, repr=False)
    _stream_timers: dict[str, asyncio.TimerHandle] = field(
        default_factory=dict, repr=False
    )
    _stream_last_sent: dict[str, str] = field(
        default_factory=dict, repr=False
    )

    async def start(self) -> None:
        """Start bot polling."""
        self._bot = Bot(token=self.token)
        self._dp = Dispatcher()
        self._dp.message.register(self._handle_incoming)
        self._dp.callback_query.register(self._handle_callback_query)

        # Register slash commands in Telegram's menu
        from aiogram.types import BotCommand
        await self._bot.set_my_commands([
            BotCommand(command="new", description="Start a fresh conversation"),
            BotCommand(command="resume", description="List or restore a previous session"),
            BotCommand(command="compact", description="Summarize and start new session with context"),
            BotCommand(command="model", description="Show or switch model (e.g. /model sonnet)"),
            BotCommand(command="cd", description="Show or switch workspace directory"),
            BotCommand(command="backend", description="Show or switch AI backend"),
            BotCommand(command="status", description="Show bot state and uptime"),
            BotCommand(command="cancel", description="Cancel the current running task"),
            BotCommand(command="verbose", description="Cycle tool call display mode"),
            BotCommand(command="exec", description="Run a shell command (e.g. /exec ls -la)"),
            BotCommand(command="sync_skills", description="Re-sync linked skill directories"),
            BotCommand(command="trust_workspace", description="Trust current workspace for agent"),
            BotCommand(command="review_loop", description="Multi-agent adversarial review loop"),
            BotCommand(command="version", description="Show version and commit hash"),
            BotCommand(command="help", description="Show available commands"),
        ])

        self._polling_task = asyncio.create_task(
            self._dp.start_polling(self._bot, handle_signals=False)
        )
        logger.info("Telegram channel started")

    async def stop(self) -> None:
        """Stop bot polling."""
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        if self._bot:
            await self._bot.session.close()
        logger.info("Telegram channel stopped")

    async def send_text(
        self, chat_id: str, text: str, parse_mode: str = "MarkdownV2"
    ) -> str:
        """Send text message, splitting if too long.

        Falls back to plain text if MarkdownV2 parsing fails.
        """
        chunks = split_message(text, TELEGRAM_LIMIT)
        last_msg_id = ""
        for chunk in chunks:
            send_text = _md_to_mdv2(chunk) if parse_mode == "MarkdownV2" else chunk
            try:
                result = await self._bot.send_message(
                    chat_id=chat_id, text=send_text, parse_mode=parse_mode
                )
            except Exception:
                if parse_mode is not None:
                    logger.debug("MarkdownV2 send failed, retrying as plain text")
                    result = await self._bot.send_message(
                        chat_id=chat_id, text=chunk, parse_mode=None
                    )
                else:
                    raise
            last_msg_id = str(result.message_id)
        return last_msg_id

    async def send_text_with_inline_keyboard(
        self,
        chat_id: str,
        text: str,
        buttons: list[tuple[str, str]],
        parse_mode: str = "MarkdownV2",
    ) -> str:
        """Send text with inline keyboard buttons.

        Each button is (label, callback_data). callback_data is sent back
        as a synthetic message when the user taps the button.
        """
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        # Build keyboard: one button per row
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=label, callback_data=data)]
                for label, data in buttons
            ]
        )
        send_text = _md_to_mdv2(text) if parse_mode == "MarkdownV2" else text
        try:
            result = await self._bot.send_message(
                chat_id=chat_id, text=send_text, parse_mode=parse_mode,
                reply_markup=keyboard,
            )
        except Exception:
            if parse_mode is not None:
                logger.debug("MarkdownV2 send failed, retrying as plain text")
                result = await self._bot.send_message(
                    chat_id=chat_id, text=text, parse_mode=None,
                    reply_markup=keyboard,
                )
            else:
                raise
        return str(result.message_id)

    async def stream_start(self, chat_id: str) -> StreamHandle:
        """Send initial placeholder message for streaming."""
        result = await self._bot.send_message(
            chat_id=chat_id, text="...", parse_mode=None
        )
        handle = StreamHandle(
            message_id=str(result.message_id), chat_id=chat_id
        )
        self._stream_buffers[handle.message_id] = ""
        self._stream_last_sent[handle.message_id] = ""
        return handle

    async def stream_update(
        self, handle: StreamHandle, text: str
    ) -> None:
        """Buffer text and throttle edits to Telegram.

        Auto-splits into a new message when buffer approaches 4096 chars.
        """
        mid = handle.message_id
        self._stream_buffers[mid] = self._stream_buffers.get(mid, "") + text

        # Auto-split: buffer approaching Telegram limit
        if len(self._stream_buffers[mid]) >= STREAM_SPLIT_THRESHOLD:
            self._cancel_stream_timer(mid)
            await self._split_stream(handle)
            return

        last_sent = self._stream_last_sent.get(mid, "")
        new_chars = len(self._stream_buffers[mid]) - len(last_sent)

        if new_chars >= FLUSH_CHAR_THRESHOLD:
            self._cancel_stream_timer(mid)
            await self._flush_stream(handle)
        elif mid not in self._stream_timers:
            loop = asyncio.get_running_loop()
            self._stream_timers[mid] = loop.call_later(
                THROTTLE_MS / 1000.0,
                lambda h=handle: asyncio.ensure_future(
                    self._flush_stream(h)
                ),
            )

    async def stream_end(self, handle: StreamHandle) -> str:
        """Cancel pending timer and send final edit with Markdown."""
        mid = handle.message_id
        self._cancel_stream_timer(mid)
        await self._flush_stream(handle, final=True)
        self._stream_buffers.pop(mid, None)
        self._stream_last_sent.pop(mid, None)
        return mid

    async def show_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        from aiogram.enums import ChatAction

        await self._bot.send_chat_action(
            chat_id=int(chat_id), action=ChatAction.TYPING
        )

    def _truncate_tool_payload(self, value: object, limit: int = 200) -> str:
        """Render tool payloads for chat display with a size cap."""
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False)
        if len(text) > limit:
            text = text[:limit] + "..."
        return text

    def format_tool_call(self, name: str, input: dict) -> str:
        """Format tool call for display based on configured mode."""
        if self.tool_calls_display == "silent":
            return ""
        if self.tool_calls_display == "summary":
            return f"\U0001f527 {name}"
        input_str = self._truncate_tool_payload(input)
        return f"\U0001f527 {name}: {input_str}"

    def _short_tool_title(self, title: str) -> str:
        """Return a shorter terminal-state label for tool summaries."""
        parts = title.split(maxsplit=1)
        if len(parts) == 2:
            return parts[1]
        return title

    def format_tool_update(
        self,
        title: str,
        status: str | None = None,
        input: object = None,
        output: object = None,
    ) -> str:
        """Format richer ACP tool lifecycle updates using the same display modes."""
        if self.tool_calls_display == "silent":
            return ""
        if self.tool_calls_display == "summary":
            if status in {"completed", "failed"}:
                return self._short_tool_title(title)
            return title

        detail = None
        if status in {"pending", "in_progress"} and input is not None:
            detail = self._truncate_tool_payload(input)
        elif status in {"completed", "failed"} and output is not None:
            detail = self._truncate_tool_payload(output)
        elif input is not None:
            detail = self._truncate_tool_payload(input)

        if detail:
            return f"{title}: {detail}"
        return title

    def _cancel_stream_timer(self, message_id: str) -> None:
        timer = self._stream_timers.pop(message_id, None)
        if timer:
            timer.cancel()

    async def _flush_stream(self, handle: StreamHandle, *, final: bool = False) -> None:
        mid = handle.message_id
        text = self._stream_buffers.get(mid, "")
        last = self._stream_last_sent.get(mid, "")
        if text and (text != last or final):
            try:
                send_text = _md_to_mdv2(text) if final else text
                if final:
                    logger.debug("Final flush mid=%s, mdv2 len=%d", mid, len(send_text))
                await self._bot.edit_message_text(
                    chat_id=handle.chat_id,
                    message_id=int(mid),
                    text=send_text,
                    **({"parse_mode": "MarkdownV2"} if final else {}),
                )
                self._stream_last_sent[mid] = text
            except Exception as e:
                if final:
                    # MarkdownV2 failed, retry plain text
                    try:
                        await self._bot.edit_message_text(
                            chat_id=handle.chat_id,
                            message_id=int(mid),
                            text=text,
                        )
                        self._stream_last_sent[mid] = text
                    except Exception as e2:
                        logger.warning("Failed to edit stream message: %s", e2)
                else:
                    logger.warning("Failed to edit stream message: %s", e)
        self._stream_timers.pop(mid, None)

    async def _split_stream(self, handle: StreamHandle) -> None:
        """Finalize current message at a safe split point and start a new one."""
        old_mid = handle.message_id
        full_text = self._stream_buffers.get(old_mid, "")

        # Find safe split point
        split_at = _find_split_point(full_text, STREAM_SPLIT_THRESHOLD)
        keep = full_text[:split_at].rstrip()
        carry = full_text[split_at:].lstrip("\n")

        # Edit old message with the 'keep' portion (MarkdownV2 + fallback)
        try:
            await self._bot.edit_message_text(
                chat_id=handle.chat_id,
                message_id=int(old_mid),
                text=_md_to_mdv2(keep),
                parse_mode="MarkdownV2",
            )
        except Exception:
            try:
                await self._bot.edit_message_text(
                    chat_id=handle.chat_id,
                    message_id=int(old_mid),
                    text=keep,
                )
            except Exception as e:
                logger.warning("Failed to edit stream message on split: %s", e)

        # Clean up old message state
        self._stream_buffers.pop(old_mid, None)
        self._stream_last_sent.pop(old_mid, None)
        self._cancel_stream_timer(old_mid)

        # Send new message and update handle in-place
        result = await self._bot.send_message(
            chat_id=handle.chat_id, text=carry or "...", parse_mode=None
        )
        new_mid = str(result.message_id)
        handle.message_id = new_mid

        # Initialize new message state
        self._stream_buffers[new_mid] = carry
        self._stream_last_sent[new_mid] = carry if carry else ""

    async def _handle_incoming(self, message: AiogramMessage) -> None:
        if not self.on_message:
            return

        attachments = []
        # Download photos
        if message.photo:
            # Telegram sends multiple sizes; take the largest
            photo = message.photo[-1]
            try:
                file = await self._bot.get_file(photo.file_id)
                dest = Path(tempfile.mkdtemp()) / f"{photo.file_id}.jpg"
                await self._bot.download_file(file.file_path, dest)
                attachments.append(Attachment(
                    type="image",
                    file_path=str(dest),
                    file_name=f"{photo.file_id}.jpg",
                    mime_type="image/jpeg",
                    size=photo.file_size or 0,
                ))
            except Exception as e:
                logger.warning("Failed to download photo: %s", e)

        # Download documents
        if message.document:
            doc = message.document
            try:
                file = await self._bot.get_file(doc.file_id)
                dest = Path(tempfile.mkdtemp()) / (doc.file_name or doc.file_id)
                await self._bot.download_file(file.file_path, dest)
                attachments.append(Attachment(
                    type="file",
                    file_path=str(dest),
                    file_name=doc.file_name or doc.file_id,
                    mime_type=doc.mime_type or "application/octet-stream",
                    size=doc.file_size or 0,
                ))
            except Exception as e:
                logger.warning("Failed to download document: %s", e)

        incoming = IncomingMessage(
            channel="telegram",
            chat_id=str(message.chat.id),
            user_id=str(message.from_user.id) if message.from_user else "",
            text=message.text or message.caption or "",
            attachments=attachments,
        )
        await self.on_message(incoming)

    async def _handle_callback_query(self, callback_query) -> None:
        """Handle inline keyboard button presses.

        Forwards callback_data as a synthetic text message to on_message.
        """
        if not self.on_message:
            return

        await callback_query.answer()

        data = callback_query.data or ""
        if not data:
            return

        user = callback_query.from_user
        chat_id = str(callback_query.message.chat.id) if callback_query.message else ""
        user_id = str(user.id) if user else ""

        incoming = IncomingMessage(
            channel="telegram",
            chat_id=chat_id,
            user_id=user_id,
            text=data,
        )
        await self.on_message(incoming)
