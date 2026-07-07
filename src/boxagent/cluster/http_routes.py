"""Cluster HTTP/WS route registration on the Web UI Starlette app.

Composition class. Held by Gateway as ``self._cluster_routes``.

These routes share the Web UI port (not the internal API port) because
``guest_client`` forwards cross-machine RPCs to the web port.
"""

from typing import TYPE_CHECKING

from starlette.routing import WebSocketRoute

if TYPE_CHECKING:
    from boxagent.cluster.request_reply import RequestReply


class ClusterHttpRoutes:
    def __init__(
        self,
        *,
        cluster_rpc: "RequestReply",
    ) -> None:
        self.cluster_rpc = cluster_rpc

    def register(self, routes: list) -> None:
        """把 guest WS 路由追加到 web UI 的 Starlette 路由列表。

        ``/api/guest/ws`` 是 guest 拨向 host 接入的 WebSocket 端点——
        始终挂上（核心 cluster）。
        """
        routes.append(
            WebSocketRoute("/api/guest/ws", self.cluster_rpc.handle_guest_ws)
        )
