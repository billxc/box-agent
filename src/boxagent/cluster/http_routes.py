"""Cluster HTTP/WS route registration on the Web UI aiohttp app.

Composition class. Held by Gateway as ``self._cluster_routes``.

These routes share the Web UI port (not the internal API port) because
``guest_client`` forwards cross-machine RPCs to the web port.
"""

from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from boxagent.cluster.rpc import ClusterRpc
    from boxagent.cluster.peer_service import PeerService


class ClusterHttpRoutes:
    def __init__(
        self,
        *,
        peer: "PeerService",
        cluster_rpc: "ClusterRpc",
    ) -> None:
        self.peer = peer
        self.cluster_rpc = cluster_rpc

    def register(self, web_app: web.Application) -> None:
        """Mount cluster RPC + guest WS routes on the web UI app.

        - ``/api/peer/send`` and ``/api/wg/peer/recv`` are HTTP RPCs used
          by ``guest_client`` for cross-machine messaging.
        - ``/api/guest/ws`` is the WebSocket endpoint guests dial to
          attach to a host.
        """
        web_app.router.add_post("/api/wg/peer/recv", self.peer.handle_wg_peer_recv)
        web_app.router.add_post("/api/peer/send", self.peer.handle_peer_send)
        web_app.router.add_get("/api/guest/ws", self.cluster_rpc.handle_guest_ws)
