"""One wiring bridging cluster registry / guest_client into BOTH syncers.

Replaces the pair events/sync_wiring.py + cluster/chat_sync_wiring.py and their
fragile install-order chain: the chat wiring used to CHAIN onto the event
wiring's ``on_unknown_frame`` (capture-previous, fall-through), so it had to be
installed *after* the event wiring or chat frames were swallowed. Here a single
installer owns the three registry/guest_client callbacks and dispatches by frame
type — event_batch/event_resync to the event syncer, chat_* to the chat syncer —
with no ordering constraint.

The sync attach/detach callbacks bridge to ChatSyncer's async methods
(detach_peer / resubscribe) via ``create_task`` (they run inside the WS coroutine,
so a loop is present). Both syncers send over the same peer WS, so they share one
``send_frame``.
"""
from __future__ import annotations

import asyncio


def install_registry_hooks(event_syncer, chat_syncer, registry) -> None:
    """Host-side: attach both syncers per connected guest; one on_unknown_frame
    dispatches event_* then chat_* (peer key = machine_id)."""

    def _on_attached(machine_id: str, session) -> None:
        async def send_frame(frame):
            await session.ws.send_json(frame)
        event_syncer.attach_peer(machine_id, send_frame)
        chat_syncer.attach_peer(machine_id, send_frame)
        asyncio.create_task(chat_syncer.resubscribe(machine_id))

    def _on_detached(machine_id: str) -> None:
        event_syncer.detach_peer(machine_id)
        asyncio.create_task(chat_syncer.detach_peer(machine_id))

    async def _on_unknown_frame(machine_id: str, payload: dict) -> bool:
        if await event_syncer.handle_frame(machine_id, payload):
            return True
        return await chat_syncer.handle_frame(machine_id, payload)

    registry.on_guest_attached = _on_attached
    registry.on_guest_detached = _on_detached
    registry.on_unknown_frame = _on_unknown_frame


def install_guest_client_hooks(event_syncer, chat_syncer, client) -> None:
    """Guest-side: same, with peer key = 'host'."""
    HOST_KEY = "host"

    def _on_connect(connected_client) -> None:
        async def send_frame(frame):
            ws = connected_client._ws
            if ws is None or ws.closed:
                return
            await ws.send_json(frame)
        event_syncer.attach_peer(HOST_KEY, send_frame)
        chat_syncer.attach_peer(HOST_KEY, send_frame)
        asyncio.create_task(chat_syncer.resubscribe(HOST_KEY))

    def _on_disconnect() -> None:
        event_syncer.detach_peer(HOST_KEY)
        asyncio.create_task(chat_syncer.detach_peer(HOST_KEY))

    async def _on_unknown_frame(payload: dict) -> bool:
        if await event_syncer.handle_frame(HOST_KEY, payload):
            return True
        return await chat_syncer.handle_frame(HOST_KEY, payload)

    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_unknown_frame = _on_unknown_frame
