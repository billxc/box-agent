"""Cluster HTTP/WS route registration on the Web UI aiohttp app.

Composition class. Held by Gateway as ``self._cluster_routes``.

These routes share the Web UI port (not the internal API port) because
``guest_client`` forwards cross-machine RPCs to the web port.
"""

from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from boxagent.cluster.request_reply import RequestReply


class ClusterHttpRoutes:
    def __init__(
        self,
        *,
        cluster_rpc: "RequestReply",
    ) -> None:
        self.cluster_rpc = cluster_rpc

    def register(self, web_app: web.Application) -> None:
        """Mount the guest WS route on the web UI app.

        ``/api/guest/ws`` is the WebSocket endpoint guests dial to attach
        to a host — always mounted (core cluster).
        """
        web_app.router.add_get("/api/guest/ws", self.cluster_rpc.handle_guest_ws)
