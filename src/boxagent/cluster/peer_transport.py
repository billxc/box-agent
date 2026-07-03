"""Shared cross-machine peer registry + send for the cluster syncers.

Both :class:`~boxagent.events.sync.EventSyncer` and
:class:`~boxagent.cluster.chat_sync.ChatSyncer` fan structured frames out to the
same set of connected peers over the same cluster WebSocket. The genuinely
shared part is the registry (``peer_key -> send_frame``) plus the send-and-swallow
loop. Each syncer composes one of these and keeps its own attach/detach
side-effects (EventSyncer resync-on-attach + sync detach; ChatSyncer plain
attach + async relay cleanup) and its own inbound ``handle_frame`` vocabulary.

Frame VOCABULARIES are NOT unified here (that is a later phase) — this only owns
the peer set and the outbound send.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Iterator

logger = logging.getLogger(__name__)

SendFrame = Callable[[dict], Awaitable[None]]


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
        """Send one frame to a peer, swallowing + logging any send failure."""
        send = self._peers.get(peer_key)
        if send is None:
            return
        try:
            await send(frame)
        except Exception as exception:
            logger.warning("%s: send to %s failed: %r",
                           self._log_prefix, peer_key, exception)
