"""Hooks bridging cluster registry / guest_client into EventSyncer."""
from __future__ import annotations

from .sync import EventSyncer


def install_registry_hooks(syncer: EventSyncer, registry) -> None:
    """Wire host-side GuestRegistry into the syncer."""

    def _on_attached(machine_id: str, session) -> None:
        async def send_frame(frame):
            await session.ws.send_json(frame)
        syncer.attach_peer(machine_id, send_frame)

    def _on_detached(machine_id: str) -> None:
        syncer.detach_peer(machine_id)

    async def _on_unknown_frame(machine_id: str, payload: dict) -> bool:
        return await syncer.handle_frame(machine_id, payload)

    registry.on_guest_attached = _on_attached
    registry.on_guest_detached = _on_detached
    registry.on_unknown_frame = _on_unknown_frame


def install_guest_client_hooks(syncer: EventSyncer, client) -> None:
    """Wire guest-side GuestClient into the syncer (peer key = 'host')."""
    HOST_KEY = "host"

    def _on_connect(connected_client) -> None:
        async def send_frame(frame):
            ws = connected_client._ws
            if ws is None or ws.closed:
                return
            await ws.send_json(frame)
        syncer.attach_peer(HOST_KEY, send_frame)

    def _on_disconnect() -> None:
        syncer.detach_peer(HOST_KEY)

    async def _on_unknown_frame(payload: dict) -> bool:
        return await syncer.handle_frame(HOST_KEY, payload)

    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_unknown_frame = _on_unknown_frame
