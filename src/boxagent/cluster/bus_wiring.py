"""Wiring bridging cluster registry / guest_client into the EventSyncer.

Owns the three registry/guest_client callbacks (attach / detach / on_unknown_frame)
and routes event_batch/event_resync frames to the EventSyncer. Chat now rides the
ClusterBus packet path; rpc is a first-class request/reply — this file is the last
resident of the old frame-typed cross-machine mechanism (events).
"""
from __future__ import annotations

import logging

from .peer_transport import WIRE_VERSION

logger = logging.getLogger(__name__)


def _wire_version_ok(machine_id: str, payload: dict) -> bool:
    """True if the frame's wire version is understood. A missing ``v`` is a
    legacy peer (predates the field) and is accepted; a present-but-different
    ``v`` is a newer/older protocol we drop gracefully rather than misparse."""
    version = payload.get("v", WIRE_VERSION)
    if version == WIRE_VERSION:
        return True
    logger.warning("dropping frame from %s: unsupported wire version %r (this node speaks %d)",
                   machine_id, version, WIRE_VERSION)
    return False


def install_registry_hooks(event_syncer, registry) -> None:
    """Host-side: attach the event syncer per connected guest; one
    on_unknown_frame handles event_* frames (peer key = machine_id)."""

    def _on_attached(machine_id: str, session) -> None:
        async def send_frame(frame):
            await session.ws.send_json(frame)
        event_syncer.attach_peer(machine_id, send_frame)

    def _on_detached(machine_id: str) -> None:
        event_syncer.detach_peer(machine_id)

    async def _on_unknown_frame(machine_id: str, payload: dict) -> bool:
        if not _wire_version_ok(machine_id, payload):
            return True  # consumed (dropped) — do not misparse or fall through
        return await event_syncer.handle_frame(machine_id, payload)

    registry.on_guest_attached = _on_attached
    registry.on_guest_detached = _on_detached
    registry.on_unknown_frame = _on_unknown_frame


def install_guest_client_hooks(event_syncer, client) -> None:
    """Guest-side: same, with peer key = 'host'."""
    HOST_KEY = "host"

    def _on_connect(connected_client) -> None:
        async def send_frame(frame):
            ws = connected_client._ws
            if ws is None or ws.closed:
                return
            await ws.send_json(frame)
        event_syncer.attach_peer(HOST_KEY, send_frame)

    def _on_disconnect() -> None:
        event_syncer.detach_peer(HOST_KEY)

    async def _on_unknown_frame(payload: dict) -> bool:
        if not _wire_version_ok("host", payload):
            return True
        return await event_syncer.handle_frame(HOST_KEY, payload)

    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_unknown_frame = _on_unknown_frame
