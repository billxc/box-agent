"""WorkgroupChannelAdapter — pluggable channel for workgroup admin↔specialist.

Encapsulates every Discord-specific call workgroup/manager.py previously made
inline, so future transports (web, devtunnel, local) can be swapped in without
touching orchestration logic.

Implementations in this module:
- WorkgroupChannelAdapter      — Protocol
- NullWorkgroupChannelAdapter  — no external channel (web UI only / tests)
- DiscordWorkgroupAdapter      — current Discord behavior, extracted verbatim

The Web-backed adapter lives in #3 (#3a).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from boxagent.config import SpecialistConfig, WorkgroupConfig

logger = logging.getLogger(__name__)


@runtime_checkable
class WorkgroupChannelAdapter(Protocol):
    """Channel surface used by WorkgroupManager.

    All async methods may be no-ops if the adapter has no external channel
    (e.g. NullWorkgroupChannelAdapter).
    """

    @property
    def channel_name(self) -> str:
        """Name written into IncomingMessage.channel for messages this adapter
        synthesizes (currently unused — manager only synthesizes channel="internal"
        callback messages — but reserved for future inbound paths)."""
        ...

    def primary_channel(self) -> object:
        """The channel object passed to Router(channel=...). May be None for
        adapters that don't drive Router streaming directly."""
        ...

    def get_specialist_chat_id(self, sp_name: str, sp_cfg: SpecialistConfig) -> str:
        """The chat_id under which the specialist's pool/transcripts live."""
        ...

    async def register_admin(self, router, wg_cfg: WorkgroupConfig) -> None:
        """Register inbound routes so admin receives external messages."""
        ...

    async def register_peer(self, router, wg_cfg: WorkgroupConfig) -> None:
        """Register cross-admin peer messaging (#8 will replace this with cluster RPC)."""
        ...

    async def setup_specialist(
        self, sp_name: str, sp_cfg: SpecialistConfig,
        wg_cfg: WorkgroupConfig, router,
    ) -> None:
        """Wire any inbound channel affordances on the specialist's router."""
        ...

    async def provision_specialist(
        self, sp_name: str, sp_cfg: SpecialistConfig, wg_cfg: WorkgroupConfig,
    ) -> SpecialistConfig:
        """Allocate any external resources (e.g. a Discord text channel) and
        return the (possibly-mutated) sp_cfg. Caller persists the result."""
        ...

    async def cleanup_specialist(
        self, sp_name: str, sp_cfg: SpecialistConfig,
    ) -> None:
        """Tear down whatever provision_specialist created."""
        ...

    async def post_task(
        self, sp_name: str, sp_cfg: SpecialistConfig,
        text: str, admin_display: str,
    ) -> None:
        """Publish a task into the specialist's visibility channel (e.g. webhook)."""
        ...

    async def notify_admin(self, chat_id: str, text: str) -> None:
        """Push a short notification to admin's chat_id (e.g. task-done summary)."""
        ...


# ─── Null implementation ─────────────────────────────────────────────────────

@dataclass
class NullWorkgroupChannelAdapter:
    """Adapter for workgroups with no external channel — pure in-process orchestration.

    All operations are no-ops; chat_id falls back to ``wg:<sp_name>``.
    """

    @property
    def channel_name(self) -> str:
        return "internal"

    def primary_channel(self) -> object:
        return None

    def get_specialist_chat_id(self, sp_name: str, sp_cfg: SpecialistConfig) -> str:
        return f"wg:{sp_name}"

    async def register_admin(self, router, wg_cfg: WorkgroupConfig) -> None:
        return

    async def register_peer(self, router, wg_cfg: WorkgroupConfig) -> None:
        return

    async def setup_specialist(self, sp_name, sp_cfg, wg_cfg, router) -> None:
        return

    async def provision_specialist(self, sp_name, sp_cfg, wg_cfg) -> SpecialistConfig:
        return sp_cfg

    async def cleanup_specialist(self, sp_name, sp_cfg) -> None:
        return

    async def post_task(self, sp_name, sp_cfg, text, admin_display) -> None:
        return

    async def notify_admin(self, chat_id, text) -> None:
        return


# ─── Discord implementation ──────────────────────────────────────────────────

@dataclass
class DiscordWorkgroupAdapter:
    """Wraps the existing DiscordChannel object. Behavior identical to the
    pre-refactor inline Discord calls in workgroup/manager.py."""

    dc_channel: object  # boxagent.channels.discord.DiscordChannel

    @property
    def channel_name(self) -> str:
        return "discord"

    def primary_channel(self) -> object:
        return self.dc_channel

    def get_specialist_chat_id(self, sp_name: str, sp_cfg: SpecialistConfig) -> str:
        return str(sp_cfg.discord_channel) if sp_cfg.discord_channel else f"wg:{sp_name}"

    async def register_admin(self, router, wg_cfg: WorkgroupConfig) -> None:
        # (a) Category route — admin sees every channel under the category.
        if wg_cfg.admin_discord_category:
            self.dc_channel.register_route(
                router.handle_message,
                [wg_cfg.admin_discord_category],
            )
            router._channels["discord"] = self.dc_channel
            logger.info(
                "Workgroup '%s': admin registered on Discord category %d",
                wg_cfg.name, wg_cfg.admin_discord_category,
            )
        # (b) Admin channel route (e.g. DM) — independent of category.
        if wg_cfg.admin_discord_channel:
            try:
                self.dc_channel.register_channel_route(
                    router.handle_message,
                    wg_cfg.admin_discord_channel,
                )
                router._channels["discord"] = self.dc_channel
                logger.info(
                    "Workgroup '%s': admin registered on Discord channel %d",
                    wg_cfg.name, wg_cfg.admin_discord_channel,
                )
            except ValueError:
                logger.debug(
                    "Workgroup '%s': Discord channel %d already registered, skipping",
                    wg_cfg.name, wg_cfg.admin_discord_channel,
                )

    async def register_peer(self, router, wg_cfg: WorkgroupConfig) -> None:
        if not wg_cfg.discord_peer_channel:
            return
        from boxagent.channels.base import IncomingMessage
        from boxagent.gateway import _parse_peer_message

        peer_ch_id = wg_cfg.discord_peer_channel
        comm_ch_id = str(wg_cfg.admin_discord_channel)
        wg_name = wg_cfg.name

        async def _peer_handler(msg, _name=wg_name, _comm=comm_ch_id, _router=router):
            target, sender, body = _parse_peer_message(msg.text)
            if target != _name:
                return
            wrapped = IncomingMessage(
                channel=msg.channel,
                chat_id=_comm,
                user_id=msg.user_id,
                text=(
                    f"[Peer message from {sender}]\n"
                    f"{body}\n\n"
                    f"---\n"
                    f'Reply with: send_to_peer("{sender}", "your reply")'
                ),
                attachments=msg.attachments,
                trusted=True,
                channel_info=msg.channel_info,
            )
            await _router.handle_message(wrapped)

        try:
            self.dc_channel.register_channel_route(_peer_handler, peer_ch_id)
            router.has_peer_channel = True
            logger.info(
                "Workgroup '%s': peer channel %d registered (comm → %s)",
                wg_name, peer_ch_id, comm_ch_id,
            )
        except ValueError:
            logger.debug(
                "Workgroup '%s': peer channel %d already registered, skipping",
                wg_name, peer_ch_id,
            )

    async def setup_specialist(self, sp_name, sp_cfg, wg_cfg, router) -> None:
        # Specialists hear inbound discord messages via the same channel object.
        router._channels["discord"] = self.dc_channel

    async def provision_specialist(self, sp_name, sp_cfg, wg_cfg) -> SpecialistConfig:
        if not wg_cfg.admin_discord_category:
            return sp_cfg
        try:
            ch_id = await self.dc_channel.create_text_channel(
                wg_cfg.admin_discord_category, sp_name,
            )
            sp_cfg.discord_channel = ch_id
        except Exception as e:
            logger.warning("Failed to create Discord channel for '%s': %s", sp_name, e)
        return sp_cfg

    async def cleanup_specialist(self, sp_name, sp_cfg) -> None:
        if not sp_cfg.discord_channel:
            return
        try:
            await self.dc_channel.delete_text_channel(sp_cfg.discord_channel)
        except Exception as e:
            logger.warning("Failed to delete Discord channel for '%s': %s", sp_name, e)

    async def post_task(self, sp_name, sp_cfg, text, admin_display) -> None:
        if not sp_cfg.discord_channel:
            return
        try:
            await self.dc_channel.send_via_webhook(
                sp_cfg.discord_channel, admin_display, text,
            )
        except Exception as e:
            logger.warning("Failed to post task to specialist channel: %s", e)

    async def notify_admin(self, chat_id, text) -> None:
        # Use _ensure_webhook (NOT ensure_allowed_webhook) so the notification
        # is filtered out by _handle_incoming and doesn't trigger admin reply.
        try:
            wh = await self.dc_channel._ensure_webhook("TaskNotification", chat_id)
            if wh:
                await wh.send(text, wait=True)
            else:
                await self.dc_channel.send_text(chat_id, text)
        except Exception as e:
            logger.warning("Failed to send task notification: %s", e)
