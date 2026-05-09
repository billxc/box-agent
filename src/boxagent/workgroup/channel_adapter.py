"""WorkgroupChannelAdapter — pluggable channel for workgroup admin↔specialist.

Encapsulates the message-bus the workgroup uses internally so future
transports can be swapped in without touching orchestration logic.

Implementations in this module:
- WorkgroupChannelAdapter      — Protocol
- NullWorkgroupChannelAdapter  — no external channel (tests)
- WebWorkgroupAdapter          — host's WebChannel as the workgroup substrate
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from boxagent.config import SpecialistConfig, WorkgroupConfig
from boxagent.transports.base import Channel
from boxagent.transports.web import WebChannel

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

    def primary_channel(self) -> Channel | None:
        """The channel object passed to Router(channel=...). May be None for
        adapters that don't drive Router streaming directly."""
        ...

    def get_specialist_chat_id(self, specialist_name: str, specialist_config: SpecialistConfig) -> str:
        """The chat_id under which the specialist's pool/transcripts live."""
        ...

    async def setup_specialist(
        self, specialist_name: str, specialist_config: SpecialistConfig,
        workgroup_config: WorkgroupConfig, router,
    ) -> None:
        """Wire any inbound channel affordances on the specialist's router."""
        ...

    async def provision_specialist(
        self, specialist_name: str, specialist_config: SpecialistConfig, workgroup_config: WorkgroupConfig,
    ) -> SpecialistConfig:
        """Allocate any per-adapter resources and return the (possibly-mutated)
        specialist_config. Caller persists the result."""
        ...

    async def cleanup_specialist(
        self, specialist_name: str, specialist_config: SpecialistConfig,
    ) -> None:
        """Tear down whatever provision_specialist created."""
        ...

    async def post_task(
        self, specialist_name: str, specialist_config: SpecialistConfig,
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

    All operations are no-ops; chat_id falls back to ``wg:<specialist_name>``.
    """

    @property
    def channel_name(self) -> str:
        return "internal"

    def primary_channel(self) -> Channel | None:
        return None

    def get_specialist_chat_id(self, specialist_name: str, specialist_config: SpecialistConfig) -> str:
        return f"wg:{specialist_name}"

    async def setup_specialist(self, specialist_name, specialist_config, workgroup_config, router) -> None:
        return

    async def provision_specialist(self, specialist_name, specialist_config, workgroup_config) -> SpecialistConfig:
        return specialist_config

    async def cleanup_specialist(self, specialist_name, specialist_config) -> None:
        return

    async def post_task(self, specialist_name, specialist_config, text, admin_display) -> None:
        return

    async def notify_admin(self, chat_id, text) -> None:
        return


# ─── Web implementation ──────────────────────────────────────────────────────

@dataclass
class WebWorkgroupAdapter:
    """Publishes workgroup events into the host's WebChannel.

    Specialist visibility is achieved by using a virtual chat_id ``wg:<specialist_name>``
    on the SAME WebChannel as the admin — the admin web UI subscribes to that
    chat_id in addition to its own and renders the specialist's stream alongside.
    """

    web_channel: WebChannel

    @property
    def channel_name(self) -> str:
        return "web"

    def primary_channel(self) -> Channel | None:
        return self.web_channel

    def get_specialist_chat_id(self, specialist_name: str, specialist_config: SpecialistConfig) -> str:
        return f"wg:{specialist_name}"

    async def setup_specialist(self, specialist_name, specialist_config, workgroup_config, router) -> None:
        # Inbound affordance: if a web POST addresses the specialist via its
        # virtual chat_id, the router resolves the channel for replies.
        router._channels["web"] = self.web_channel

    async def provision_specialist(self, specialist_name, specialist_config, workgroup_config) -> SpecialistConfig:
        # Nothing to allocate — specialist chat_id is virtual.
        return specialist_config

    async def cleanup_specialist(self, specialist_name, specialist_config) -> None:
        return

    async def post_task(self, specialist_name, specialist_config, text, admin_display) -> None:
        # Render the admin's task as a user-role message in the specialist's
        # virtual chat so the web UI shows what was dispatched.
        self.web_channel._publish(
            f"wg:{specialist_name}",
            {
                "type": "message",
                "role": "user",
                "message_id": self.web_channel._allocate_id(),
                "text": f"[{admin_display}] {text}",
            },
        )

    async def notify_admin(self, chat_id, text) -> None:
        await self.web_channel.send_text(chat_id, text)
