"""Shared cross-machine peer registry + send for the cluster syncers.

Both :class:`~boxagent.events.sync.EventSyncer` and
:class:`~boxagent.cluster.chat_sync.ChatSyncer` fan structured frames out to the
same set of connected peers over the same cluster WebSocket. The genuinely
shared part is the registry (``peer_key -> send_frame``) plus the send-and-swallow
loop. Each syncer composes one of these and keeps its own attach/detach
side-effects (EventSyncer resync-on-attach + sync detach; ChatSyncer plain
attach + async relay cleanup) and its own inbound ``handle_frame`` vocabulary.

Frame VOCABULARIES stay type-tagged per syncer, but every outbound frame is
stamped with the wire-protocol version ``v`` here (the single send chokepoint for
event + chat frames), so a peer can drop frames from an incompatible protocol
version gracefully instead of misparsing them (see bus_wiring's version gate).
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Iterator

logger = logging.getLogger(__name__)

SendFrame = Callable[[dict], Awaitable[None]]

# Cluster wire-protocol version. Bumped when the frame shape changes
# incompatibly; a node drops frames whose ``v`` it doesn't understand rather than
# misparse them. A missing ``v`` is treated as the current version (legacy peers
# that predate the field).
WIRE_VERSION = 2


class PeerTransport:
    """A registry of connected peers and the byte-identical outbound send.

    ``log_prefix`` distinguishes the two syncers' warning lines (``"syncer"`` /
    ``"chat"``) so migrating to this shared transport keeps log output identical.
    """

    def __init__(self, *, log_prefix: str) -> None:
        self._log_prefix = log_prefix
        self._peers: dict[str, SendFrame] = {}

    # ---------- registry ----------

    def attach_peer(self, peer_key: str, send_frame: SendFrame) -> None:
        """Register (or replace) the send callable for ``peer_key``."""
        self._peers[peer_key] = send_frame

    def detach_peer(self, peer_key: str) -> bool:
        """Remove ``peer_key``; return whether it was registered."""
        return self._peers.pop(peer_key, None) is not None

    def clear(self) -> None:
        self._peers.clear()

    # ---------- read access ----------

    def get(self, peer_key: str) -> SendFrame | None:
        return self._peers.get(peer_key)

    def peer_keys(self) -> list[str]:
        return list(self._peers)

    def __contains__(self, peer_key: str) -> bool:
        return peer_key in self._peers

    def __iter__(self) -> Iterator[str]:
        return iter(self._peers)

    # ---------- outbound ----------

    async def send_to(self, peer_key: str, frame: dict) -> None:
        """Send one frame to a peer, swallowing + logging any send failure.

        Stamps the wire-protocol version onto the frame first (idempotent — a
        broadcast reuses one frame dict across peers)."""
        send = self._peers.get(peer_key)
        if send is None:
            return
        frame.setdefault("v", WIRE_VERSION)
        try:
            await send(frame)
        except Exception as exception:
            logger.warning("%s: send to %s failed: %r",
                           self._log_prefix, peer_key, exception)
