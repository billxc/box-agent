"""Hooks bridging cluster registry / guest_client into ChatSyncer.

Mirrors ``events/sync_wiring.py`` but **chains** onto the existing hooks instead
of overwriting them: the EventSyncer already owns
``on_guest_attached`` / ``on_guest_detached`` / ``on_unknown_frame``, so these
installers capture the current callbacks and fall through to them. This means
chat hooks MUST be installed *after* the event hooks (the event wiring assigns,
it does not chain). Gateway guarantees that order.

``attach_peer`` / ``resubscribe`` / ``detach_peer`` bridge the sync
attach/detach callbacks to ChatSyncer's async methods via ``create_task`` (the
callbacks run inside the WS-serving coroutine, so a loop is always present).
"""
from __future__ import annotations

import asyncio

from .chat_sync import ChatSyncer


def install_registry_hooks(syncer: ChatSyncer, registry) -> None:
    """Wire host-side GuestRegistry into the chat syncer (peer key = machine_id)."""
    previous_attached = registry.on_guest_attached
    previous_detached = registry.on_guest_detached
    previous_unknown = registry.on_unknown_frame

    def _on_attached(machine_id: str, session) -> None:
        if previous_attached is not None:
            previous_attached(machine_id, session)

        async def send_frame(frame):
            await session.ws.send_json(frame)
        syncer.attach_peer(machine_id, send_frame)
        asyncio.create_task(syncer.resubscribe(machine_id))

    def _on_detached(machine_id: str) -> None:
        if previous_detached is not None:
            previous_detached(machine_id)
        asyncio.create_task(syncer.detach_peer(machine_id))

    async def _on_unknown_frame(machine_id: str, payload: dict) -> bool:
        if await syncer.handle_frame(machine_id, payload):
            return True
        if previous_unknown is not None:
            return await previous_unknown(machine_id, payload)
        return False

    registry.on_guest_attached = _on_attached
    registry.on_guest_detached = _on_detached
    registry.on_unknown_frame = _on_unknown_frame


def install_guest_client_hooks(syncer: ChatSyncer, client) -> None:
    """Wire guest-side GuestClient into the chat syncer (peer key = 'host')."""
    HOST_KEY = "host"
    previous_connect = client.on_connect
    previous_disconnect = client.on_disconnect
    previous_unknown = client.on_unknown_frame

    def _on_connect(connected_client) -> None:
        if previous_connect is not None:
            previous_connect(connected_client)

        async def send_frame(frame):
            ws = connected_client._ws
            if ws is None or ws.closed:
                return
            await ws.send_json(frame)
        syncer.attach_peer(HOST_KEY, send_frame)
        asyncio.create_task(syncer.resubscribe(HOST_KEY))

    def _on_disconnect() -> None:
        if previous_disconnect is not None:
            previous_disconnect()
        asyncio.create_task(syncer.detach_peer(HOST_KEY))

    async def _on_unknown_frame(payload: dict) -> bool:
        if await syncer.handle_frame(HOST_KEY, payload):
            return True
        if previous_unknown is not None:
            return await previous_unknown(payload)
        return False

    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_unknown_frame = _on_unknown_frame
