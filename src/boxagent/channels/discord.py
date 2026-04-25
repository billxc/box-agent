"""Discord channel — send/receive messages via discord.py."""

import asyncio
import json
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import discord

from boxagent.channels.base import Attachment, IncomingMessage, StreamHandle
from boxagent.channels.splitter import split_message, _find_split_point

logger = logging.getLogger(__name__)

DISCORD_LIMIT = 2000
THROTTLE_MS = 300
FLUSH_CHAR_THRESHOLD = 200
STREAM_SPLIT_THRESHOLD = 1800  # Leave ~200 char margin for Discord limit


@dataclass
class DiscordChannel:
    """Discord bot channel using discord.py."""

    token: str
    allowed_users: list[int]
    tool_calls_display: str = "summary"
    on_message: object = None

    _client: discord.Client | None = field(default=None, repr=False)
    _connect_task: asyncio.Task | None = field(default=None, repr=False)
    _stream_buffers: dict[str, str] = field(default_factory=dict, repr=False)
    _stream_timers: dict[str, asyncio.TimerHandle] = field(
        default_factory=dict, repr=False
    )
    _stream_last_sent: dict[str, str] = field(
        default_factory=dict, repr=False
    )

    async def start(self) -> None:
        """Start Discord bot connection."""
        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)
        tree = discord.app_commands.CommandTree(self._client)

        # Register slash commands matching Telegram's command menu
        _COMMANDS = [
            ("new", "Start a fresh conversation"),
            ("resume", "List or restore a previous session"),
            ("compact", "Summarize and start new session with context"),
            ("model", "Show or switch model (e.g. /model sonnet)"),
            ("cd", "Show or switch workspace directory"),
            ("backend", "Show or switch AI backend"),
            ("status", "Show bot state and uptime"),
            ("cancel", "Cancel the current running task"),
            ("verbose", "Cycle tool call display mode"),
            ("exec", "Run a shell command (e.g. /exec ls -la)"),
            ("sync_skills", "Re-sync linked skill directories"),
            ("trust_workspace", "Trust current workspace for agent"),
            ("review_loop", "Multi-agent adversarial review loop"),
            ("sessions", "List Claude CLI sessions"),
            ("schedule", "Manage schedules (list/logs/show/run)"),
            ("version", "Show version and commit hash"),
            ("help", "Show available commands"),
        ]

        def _make_slash_callback(name: str):
            async def _slash(interaction: discord.Interaction, args: str = ""):
                await interaction.response.defer()
                if self.on_message:
                    text = f"/{name} {args}".strip() if args else f"/{name}"
                    incoming = IncomingMessage(
                        channel="discord",
                        chat_id=str(interaction.channel_id),
                        user_id=str(interaction.user.id),
                        text=text,
                    )
                    await self.on_message(incoming)
            return _slash

        for cmd_name, cmd_desc in _COMMANDS:
            cb = _make_slash_callback(cmd_name)
            tree.command(name=cmd_name, description=cmd_desc)(
                discord.app_commands.describe(args="Optional arguments")(cb)
            )

        @self._client.event
        async def on_ready():
            await tree.sync()
            logger.info(
                "Discord channel ready as %s (synced %d commands)",
                self._client.user, len(_COMMANDS),
            )

        @self._client.event
        async def on_message(message: discord.Message):
            await self._handle_incoming(message)

        self._connect_task = asyncio.create_task(
            self._client.start(self.token)
        )
        logger.info("Discord channel starting")

    async def stop(self) -> None:
        """Stop Discord bot connection."""
        if self._client and not self._client.is_closed():
            await self._client.close()
        if self._connect_task:
            self._connect_task.cancel()
            try:
                await self._connect_task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("Discord channel stopped")

    async def send_text(
        self, chat_id: str, text: str, parse_mode: str = "Markdown"
    ) -> str:
        """Send text message, splitting if too long."""
        channel = self._client.get_channel(int(chat_id))
        if channel is None:
            channel = await self._client.fetch_channel(int(chat_id))

        chunks = split_message(text, DISCORD_LIMIT)
        last_msg_id = ""
        for chunk in chunks:
            result = await channel.send(chunk)
            last_msg_id = str(result.id)
        return last_msg_id

    async def send_text_with_inline_keyboard(
        self,
        chat_id: str,
        text: str,
        buttons: list[tuple[str, str]],
        parse_mode: str = "Markdown",
    ) -> str:
        """Send text with button components.

        Each button is (label, callback_data). Pressing a button sends
        callback_data as a synthetic message.
        """
        channel = self._client.get_channel(int(chat_id))
        if channel is None:
            channel = await self._client.fetch_channel(int(chat_id))

        view = discord.ui.View(timeout=None)
        for label, data in buttons:
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)

            async def _callback(interaction: discord.Interaction, d=data):
                await interaction.response.defer()
                if self.on_message:
                    incoming = IncomingMessage(
                        channel="discord",
                        chat_id=str(interaction.channel_id),
                        user_id=str(interaction.user.id),
                        text=d,
                    )
                    await self.on_message(incoming)

            btn.callback = _callback
            view.add_item(btn)

        result = await channel.send(text, view=view)
        return str(result.id)

    async def stream_start(self, chat_id: str) -> StreamHandle:
        """Send initial placeholder message for streaming."""
        channel = self._client.get_channel(int(chat_id))
        if channel is None:
            channel = await self._client.fetch_channel(int(chat_id))

        result = await channel.send("...")
        handle = StreamHandle(
            message_id=str(result.id), chat_id=chat_id
        )
        self._stream_buffers[handle.message_id] = ""
        self._stream_last_sent[handle.message_id] = ""
        return handle

    async def stream_update(
        self, handle: StreamHandle, text: str
    ) -> None:
        """Buffer text and throttle edits to Discord.

        Auto-splits into a new message when buffer approaches 2000 chars.
        """
        mid = handle.message_id
        self._stream_buffers[mid] = self._stream_buffers.get(mid, "") + text

        # Auto-split: buffer approaching Discord limit
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
        """Cancel pending timer and send final edit."""
        mid = handle.message_id
        self._cancel_stream_timer(mid)
        await self._flush_stream(handle, final=True)
        self._stream_buffers.pop(mid, None)
        self._stream_last_sent.pop(mid, None)
        return mid

    async def show_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        channel = self._client.get_channel(int(chat_id))
        if channel is None:
            channel = await self._client.fetch_channel(int(chat_id))
        await channel.typing()

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
        """Format richer tool lifecycle updates using the same display modes."""
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
                msg = await self._fetch_message(handle.chat_id, int(mid))
                if msg:
                    await msg.edit(content=text)
                    self._stream_last_sent[mid] = text
            except Exception as e:
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

        # Edit old message with the 'keep' portion
        try:
            msg = await self._fetch_message(handle.chat_id, int(old_mid))
            if msg:
                await msg.edit(content=keep)
        except Exception as e:
            logger.warning("Failed to edit stream message on split: %s", e)

        # Clean up old message state
        self._stream_buffers.pop(old_mid, None)
        self._stream_last_sent.pop(old_mid, None)
        self._cancel_stream_timer(old_mid)

        # Send new message and update handle in-place
        channel = self._client.get_channel(int(handle.chat_id))
        if channel is None:
            channel = await self._client.fetch_channel(int(handle.chat_id))
        result = await channel.send(carry or "...")
        new_mid = str(result.id)
        handle.message_id = new_mid

        # Initialize new message state
        self._stream_buffers[new_mid] = carry
        self._stream_last_sent[new_mid] = carry if carry else ""

    async def _fetch_message(
        self, chat_id: str, message_id: int
    ) -> discord.Message | None:
        """Fetch a message object for editing."""
        try:
            channel = self._client.get_channel(int(chat_id))
            if channel is None:
                channel = await self._client.fetch_channel(int(chat_id))
            return await channel.fetch_message(message_id)
        except Exception as e:
            logger.warning("Failed to fetch message %d: %s", message_id, e)
            return None

    async def _handle_incoming(self, message: discord.Message) -> None:
        """Handle incoming Discord message."""
        if not self.on_message:
            return

        # Ignore messages from the bot itself
        if message.author == self._client.user:
            return

        attachments = []
        for att in message.attachments:
            try:
                dest = Path(tempfile.mkdtemp()) / att.filename
                await att.save(dest)
                att_type = "image" if att.content_type and att.content_type.startswith("image/") else "file"
                attachments.append(Attachment(
                    type=att_type,
                    file_path=str(dest),
                    file_name=att.filename,
                    mime_type=att.content_type or "application/octet-stream",
                    size=att.size,
                ))
            except Exception as e:
                logger.warning("Failed to download attachment: %s", e)

        incoming = IncomingMessage(
            channel="discord",
            chat_id=str(message.channel.id),
            user_id=str(message.author.id),
            text=message.content or "",
            attachments=attachments,
        )
        await self.on_message(incoming)
