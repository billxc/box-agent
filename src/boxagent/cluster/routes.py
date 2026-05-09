"""Cluster HTTP/WS routes that share the Web UI aiohttp app.

Mounted as a mixin on Gateway. ``WebServerMixin._start_web_http`` calls
``_register_extra_web_routes`` after the web routes (and before static)
so cluster RPCs land on the same port that the web UI uses (necessary
because ``guest_client`` forwards cross-machine RPCs to the web port).
"""

from aiohttp import web


class ClusterRoutesMixin:
    def _register_extra_web_routes(self, web_app: web.Application) -> None:
        """Register cluster RPC + guest WS routes on the web app.

        - ``/api/peer/send`` and ``/api/wg/peer/recv`` are HTTP RPCs used
          by ``guest_client`` for cross-machine messaging. They must live
          on the web port (not the internal API port) because guest
          forwarding targets the web port.
        - ``/api/guest/ws`` is the WebSocket endpoint guests dial to
          attach to a host.
        """
        web_app.router.add_post("/api/wg/peer/recv", self._peer.handle_wg_peer_recv)
        web_app.router.add_post("/api/peer/send", self._peer.handle_peer_send)
        web_app.router.add_get("/api/guest/ws", self._handle_guest_ws)
