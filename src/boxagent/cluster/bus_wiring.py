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
import logging

from .peer_transport import WIRE_VERSION

logger = logging.getLogger(__name__)

# Strong refs to fire-and-forget chat resubscribe/detach tasks: the event loop
# keeps only a WEAK ref to a bare create_task result, so without this the task
# can be garbage-collected mid-flight (a reconnect's chat_subscribe replay would
# silently never run). The done-callback drops the ref and logs any exception
# that would otherwise vanish into an unretrieved task.
_background_tasks: set = set()


def _spawn(coroutine) -> None:
    task = asyncio.create_task(coroutine)
    _background_tasks.add(task)

    def _done(finished: asyncio.Task) -> None:
        _background_tasks.discard(finished)
        if not finished.cancelled() and finished.exception() is not None:
            logger.warning("chat wiring task failed: %r", finished.exception())

    task.add_done_callback(_done)


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


def install_registry_hooks(event_syncer, chat_syncer, registry) -> None:
    """Host-side: attach both syncers per connected guest; one on_unknown_frame
    dispatches event_* then chat_* (peer key = machine_id)."""

    def _on_attached(machine_id: str, session) -> None:
        async def send_frame(frame):
            await session.ws.send_json(frame)
        event_syncer.attach_peer(machine_id, send_frame)
        chat_syncer.attach_peer(machine_id, send_frame)
        _spawn(chat_syncer.resubscribe(machine_id))

    def _on_detached(machine_id: str) -> None:
        event_syncer.detach_peer(machine_id)
        _spawn(chat_syncer.detach_peer(machine_id))

    async def _on_unknown_frame(machine_id: str, payload: dict) -> bool:
        if not _wire_version_ok(machine_id, payload):
            return True  # consumed (dropped) — do not misparse or fall through
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
        _spawn(chat_syncer.resubscribe(HOST_KEY))

    def _on_disconnect() -> None:
        event_syncer.detach_peer(HOST_KEY)
        _spawn(chat_syncer.detach_peer(HOST_KEY))

    async def _on_unknown_frame(payload: dict) -> bool:
        if not _wire_version_ok("host", payload):
            return True
        if await event_syncer.handle_frame(HOST_KEY, payload):
            return True
        return await chat_syncer.handle_frame(HOST_KEY, payload)

    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_unknown_frame = _on_unknown_frame
