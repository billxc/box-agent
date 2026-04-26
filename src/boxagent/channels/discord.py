"""Discord channel — send/receive messages via discord.py."""

import asyncio
import json
import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

import discord

from boxagent.channels.base import Attachment, IncomingMessage, StreamHandle
from boxagent.channels.md_format import md_to_discord
from boxagent.channels.splitter import split_message, _find_split_point

logger = logging.getLogger(__name__)

DISCORD_LIMIT = 2000
THROTTLE_MS = 300
FLUSH_CHAR_THRESHOLD = 200
STREAM_SPLIT_THRESHOLD = 1800  # Leave ~200 char margin for Discord limit

# Sentinel key for DM messages in the category routing map.
DM_CATEGORY: str = "DM"

# Category key type: int for guild category IDs, str for DM_CATEGORY sentinel.
CategoryKey = Union[int, str]


@dataclass
class DiscordChannel:
    """Discord bot channel using discord.py.

    Supports multiple BA bots sharing a single Discord client connection.
    Each bot registers its own message handler for specific channel categories
    via ``register_route()``, or for specific channels via
    ``register_channel_route()`` (used by workgroups).
    """

    token: str
    tool_calls_display: str = "summary"

    # Category → message callback routing map.
    # Populated via register_route(); keyed by category_id (int) or DM_CATEGORY.
    _category_map: dict[CategoryKey, Callable] = field(
        default_factory=dict, repr=False
    )

    # Channel → message callback routing map (takes priority over category).
    # Populated via register_channel_route(); keyed by channel_id (int).
    _channel_map: dict[int, Callable] = field(
        default_factory=dict, repr=False
    )

    _client: discord.Client | None = field(default=None, repr=False)
    _connect_task: asyncio.Task | None = field(default=None, repr=False)
    _stream_buffers: dict[str, str] = field(default_factory=dict, repr=False)
    _stream_timers: dict[str, asyncio.TimerHandle] = field(
        default_factory=dict, repr=False
    )
    _stream_last_sent: dict[str, str] = field(
        default_factory=dict, repr=False
    )
    _pending_interactions: dict[str, discord.Interaction] = field(
        default_factory=dict, repr=False
    )

    # Webhooks keyed by "bot_name" for workgroup virtual identities.
    _webhooks: dict[str, discord.Webhook] = field(
        default_factory=dict, repr=False
    )
    # Webhook IDs whose messages should be processed (not filtered).
    _allowed_webhook_ids: set[int] = field(
        default_factory=set, repr=False
    )

    def register_route(
        self,
        on_message: Callable,
        categories: list[CategoryKey],
    ) -> None:
        """Register a message handler for the given channel categories.

        Args:
            on_message: Async callback ``(IncomingMessage) -> None``.
            categories: List of guild category IDs (int) or ``DM_CATEGORY``.
        """
        for cat in categories:
            if cat in self._category_map:
                raise ValueError(
                    f"Discord category {cat!r} is already registered to another route"
                )
            self._category_map[cat] = on_message

    def register_channel_route(
        self,
        on_message: Callable,
        channel_id: int,
    ) -> None:
        """Register a message handler for a specific Discord channel.

        Used by workgroups where each bot has its own channel.

        Args:
            on_message: Async callback ``(IncomingMessage) -> None``.
            channel_id: Discord channel ID.
        """
        if channel_id in self._channel_map:
            raise ValueError(
                f"Discord channel {channel_id} is already registered to another route"
            )
        self._channel_map[channel_id] = on_message
        logger.info("Registered channel route: %d", channel_id)

    @staticmethod
    def _get_category_key(channel: object) -> CategoryKey | None:
        """Derive the routing key from a Discord channel object."""
        if isinstance(channel, discord.DMChannel):
            return DM_CATEGORY
        return getattr(channel, "category_id", None)

    def _resolve_callback(self, channel: object) -> Callable | None:
        """Find the registered callback for the given Discord channel."""
        # Check channel-specific route first (workgroup)
        channel_id = getattr(channel, "id", None)
        if channel_id is not None and channel_id in self._channel_map:
            return self._channel_map[channel_id]
        # Fall back to category-based routing
        key = self._get_category_key(channel)
        return self._category_map.get(key)

    async def _ensure_webhook(self, bot_name: str, chat_id: str) -> discord.Webhook | None:
        """Get or create a webhook for a named identity in a channel."""
        key = bot_name.lower()
        # Key by "name:channel" to support same identity in multiple channels
        cache_key = f"{key}:{chat_id}"
        if cache_key in self._webhooks:
            return self._webhooks[cache_key]
        try:
            channel = await self._resolve_channel(chat_id)
            # Discord forbids "discord" in webhook names
            safe_name = key.replace("discord", "dc")
            webhook_name = f"ba-{safe_name}"
            # Reuse existing webhook if found
            existing = await channel.webhooks()
            for wh in existing:
                if wh.name == webhook_name:
                    self._webhooks[cache_key] = wh
                    logger.info("Reusing webhook '%s' in channel %s", webhook_name, chat_id)
                    return wh
            # Create new webhook
            wh = await channel.create_webhook(name=webhook_name)
            self._webhooks[cache_key] = wh
            logger.info("Created webhook '%s' in channel %s", webhook_name, chat_id)
            return wh
        except Exception as e:
            logger.warning("Failed to ensure webhook for '%s': %s", bot_name, e)
            return None

    async def ensure_allowed_webhook(self, name: str, chat_id: str) -> discord.Webhook | None:
        """Get or create a webhook whose messages are NOT filtered.

        Messages from this webhook pass through ``_handle_incoming`` and
        are routed to the appropriate handler like normal user messages.
        """
        wh = await self._ensure_webhook(name, chat_id)
        if wh:
            self._allowed_webhook_ids.add(wh.id)
        return wh

    async def create_text_channel(self, category_id: int, name: str) -> int:
        """Create a text channel under a category. Returns the new channel ID."""
        category = self._client.get_channel(category_id)
        if category is None:
            category = await self._client.fetch_channel(category_id)
        channel = await category.guild.create_text_channel(name, category=category)
        logger.info("Created Discord channel #%s (ID: %d) in category %d", name, channel.id, category_id)
        return channel.id

    async def delete_text_channel(self, channel_id: int) -> bool:
        """Delete a text channel by ID. Returns True if deleted."""
        channel = self._client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(channel_id)
            except Exception:
                logger.warning("Discord channel %d not found, skipping delete", channel_id)
                return False
        await channel.delete(reason="specialist deleted")
        logger.info("Deleted Discord channel #%s (ID: %d)", channel.name, channel_id)
        return True

    async def send_via_webhook(
        self, channel_id: int, webhook_name: str, text: str,
    ) -> str:
        """Send a message in a channel using a named webhook identity.

        Used by workgroup admin to post tasks in specialist channels.
        Returns the message ID.
        """
        chat_id = str(channel_id)
        webhook = await self._ensure_webhook(webhook_name, chat_id)
        if not webhook:
            # Fallback to normal send
            channel = await self._resolve_channel(chat_id)
            result = await channel.send(text)
            return str(result.id)
        chunks = split_message(text, DISCORD_LIMIT)
        last_msg_id = ""
        for chunk in chunks:
            result = await webhook.send(chunk, wait=True)
            last_msg_id = str(result.id)
        return last_msg_id

    async def start(self) -> None:
        """Start Discord bot connection."""
        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)

        @self._client.event
        async def on_ready():
            logger.info("Discord channel ready as %s", self._client.user)

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

    async def _resolve_channel(self, chat_id: str):
        """Resolve a chat_id to a messageable channel."""
        cid = int(chat_id)
        channel = self._client.get_channel(cid)
        if channel is not None:
            return channel
        return await self._client.fetch_channel(cid)

    async def send_dm(self, user_id: str, text: str) -> str:
        """Send a direct message to a user by ID."""
        user = await self._client.fetch_user(int(user_id))
        dm = await user.create_dm()
        chunks = split_message(text, DISCORD_LIMIT)
        last_msg_id = ""
        for chunk in chunks:
            result = await dm.send(chunk)
            last_msg_id = str(result.id)
        return last_msg_id

    async def _consume_interaction(self, chat_id: str) -> discord.Interaction | None:
        """Pop and return a pending slash-command interaction for *chat_id*."""
        return self._pending_interactions.pop(chat_id, None)

    async def send_text(
        self, chat_id: str, text: str, parse_mode: str = "Markdown",
        webhook_name: str = "",
    ) -> str:
        """Send text message, splitting if too long.

        If there is a pending slash-command interaction for this chat, the
        first chunk is delivered via ``interaction.edit_original_response``
        so that Discord's "thinking..." placeholder is replaced inline.

        If *webhook_name* is set, sends via a bus webhook instead.
        """
        interaction = await self._consume_interaction(chat_id)
        text = md_to_discord(text)
        chunks = split_message(text, DISCORD_LIMIT)
        last_msg_id = ""

        webhook = None
        if webhook_name:
            webhook = await self._ensure_webhook(webhook_name, chat_id)

        for i, chunk in enumerate(chunks):
            if i == 0 and interaction:
                try:
                    msg = await interaction.edit_original_response(content=chunk)
                    last_msg_id = str(msg.id)
                    continue
                except Exception as e:
                    logger.warning("Failed to edit interaction response: %s", e)
                    # Fall through to normal send
            if webhook:
                result = await webhook.send(chunk, wait=True)
            else:
                channel = await self._resolve_channel(chat_id)
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
        channel = await self._resolve_channel(chat_id)

        view = discord.ui.View(timeout=None)
        for label, data in buttons:
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)

            async def _callback(interaction: discord.Interaction, d=data):
                await interaction.response.defer()
                chat_id = str(interaction.channel_id)
                self._pending_interactions[chat_id] = interaction
                callback = self._resolve_callback(interaction.channel)
                if callback:
                    incoming = IncomingMessage(
                        channel="discord",
                        chat_id=chat_id,
                        user_id=str(interaction.user.id),
                        text=d,
                    )
                    await callback(incoming)

            btn.callback = _callback
            view.add_item(btn)

        result = await channel.send(md_to_discord(text), view=view)
        return str(result.id)

    async def stream_start(self, chat_id: str, webhook_name: str = "") -> StreamHandle:
        """Send initial placeholder message for streaming.

        Reuses a pending slash-command interaction when available so the
        "thinking..." indicator becomes the streaming message.

        If *webhook_name* is set, sends the placeholder via a bus webhook.
        """
        interaction = await self._consume_interaction(chat_id)
        if interaction:
            try:
                msg = await interaction.edit_original_response(content="...")
                handle = StreamHandle(
                    message_id=str(msg.id), chat_id=chat_id,
                    webhook_name=webhook_name,
                )
                self._stream_buffers[handle.message_id] = ""
                self._stream_last_sent[handle.message_id] = ""
                return handle
            except Exception as e:
                logger.warning("Failed to edit interaction for stream: %s", e)

        if webhook_name:
            webhook = await self._ensure_webhook(webhook_name, chat_id)
            if webhook:
                result = await webhook.send("...", wait=True)
                handle = StreamHandle(
                    message_id=str(result.id), chat_id=chat_id,
                    webhook_name=webhook_name,
                )
                self._stream_buffers[handle.message_id] = ""
                self._stream_last_sent[handle.message_id] = ""
                return handle

        channel = await self._resolve_channel(chat_id)
        result = await channel.send("...")
        handle = StreamHandle(
            message_id=str(result.id), chat_id=chat_id,
            webhook_name=webhook_name,
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
        """Send typing indicator (skipped when a slash-command interaction
        is pending, since Discord already shows 'thinking...')."""
        if chat_id in self._pending_interactions:
            return
        channel = await self._resolve_channel(chat_id)
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
                send_text = md_to_discord(text) if final else text
                if handle.webhook_name:
                    cache_key = f"{handle.webhook_name.lower()}:{handle.chat_id}"
                    webhook = self._webhooks.get(cache_key)
                    if webhook:
                        await webhook.edit_message(int(mid), content=send_text)
                        self._stream_last_sent[mid] = text
                else:
                    msg = await self._fetch_message(handle.chat_id, int(mid))
                    if msg:
                        await msg.edit(content=send_text)
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

        # Edit old message with the 'keep' portion (converted for Discord)
        try:
            if handle.webhook_name:
                cache_key = f"{handle.webhook_name.lower()}:{handle.chat_id}"
                webhook = self._webhooks.get(cache_key)
                if webhook:
                    await webhook.edit_message(int(old_mid), content=md_to_discord(keep))
            else:
                msg = await self._fetch_message(handle.chat_id, int(old_mid))
                if msg:
                    await msg.edit(content=md_to_discord(keep))
        except Exception as e:
            logger.warning("Failed to edit stream message on split: %s", e)

        # Clean up old message state
        self._stream_buffers.pop(old_mid, None)
        self._stream_last_sent.pop(old_mid, None)
        self._cancel_stream_timer(old_mid)

        # Send new message and update handle in-place
        if handle.webhook_name:
            webhook = await self._ensure_webhook(handle.webhook_name, handle.chat_id)
            if webhook:
                result = await webhook.send(carry or "...", wait=True)
            else:
                channel = await self._resolve_channel(handle.chat_id)
                result = await channel.send(carry or "...")
        else:
            channel = await self._resolve_channel(handle.chat_id)
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
            channel = await self._resolve_channel(chat_id)
            return await channel.fetch_message(message_id)
        except Exception as e:
            logger.warning("Failed to fetch message %d: %s", message_id, e)
            return None

    async def _collect_attachments(self, message: discord.Message) -> list:
        """Download message attachments to temp files."""
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
        return attachments

    async def _handle_incoming(self, message: discord.Message) -> None:
        """Handle incoming Discord message — route by channel or category."""
        # Ignore messages from the bot itself
        is_self = message.author == self._client.user
        if is_self:
            return

        # Ignore webhook messages — except those in the allow list
        # (e.g. TaskNotification webhooks used for workgroup callbacks).
        is_allowed_webhook = False
        if isinstance(message.webhook_id, int):
            if message.webhook_id not in self._allowed_webhook_ids:
                return
            is_allowed_webhook = True

        # Ignore system messages (member joins, boosts, pins, etc.)
        if message.type not in (
            discord.MessageType.default,
            discord.MessageType.reply,
        ):
            return

        # Channel-level routing (workgroup channels)
        channel_id = message.channel.id
        if channel_id in self._channel_map:
            callback = self._channel_map[channel_id]
            attachments = await self._collect_attachments(message)
            incoming = IncomingMessage(
                channel="discord",
                chat_id=str(channel_id),
                user_id=str(message.author.id),
                text=message.content or "",
                attachments=attachments,
                trusted=is_allowed_webhook,
            )
            await callback(incoming)
            return

        # Normal routing by category
        callback = self._resolve_callback(message.channel)
        if callback is None:
            return

        attachments = await self._collect_attachments(message)
        incoming = IncomingMessage(
            channel="discord",
            chat_id=str(message.channel.id),
            user_id=str(message.author.id),
            text=message.content or "",
            attachments=attachments,
            trusted=is_allowed_webhook,
        )
        await callback(incoming)
