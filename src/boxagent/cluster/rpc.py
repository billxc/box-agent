"""Cluster RPC dispatch — host↔guest proxying for HTTP and SSE.

Composition class. Held by Gateway as ``self._cluster_rpc``. Single-phase
DI: depends only on ``TopologyService`` (which exposes guest_registry +
guest_client lazily via host_election).

Public surface:
- ``dispatch_machine_request`` — caller-side helper used by WebHttpServer to
  forward an HTTP request to a remote machine. (Live chat SSE no longer proxies
  here — it rides ChatBus/ChatSyncer over the WS as structured frames.)
- ``handle_guest_ws`` — aiohttp handler registered by ClusterHttpRoutes.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from boxagent.cluster.topology_service import TopologyService

logger = logging.getLogger(__name__)


class ClusterRpc:
    def __init__(self, *, topology: "TopologyService") -> None:
        self.topology = topology

    async def dispatch_machine_request(
        self,
        machine: str,
        method: str,
        path: str,
        request: web.Request,
        body: dict | None = None,
    ) -> web.Response | None:
        """If `machine` is remote, forward and return the response.
        Returns None when the request targets the local node (caller should
        continue with its local handling).

        Host role: forward via GuestSession (existing host→guest RPC).
        Guest role: forward via GuestClient (guest→host RPC); the
        host then dispatches locally or proxies onward to the right guest.
        """
        if machine == self.topology.local_machine_id():
            return None
        guest_registry = self.topology.guest_registry
        if guest_registry is not None:
            session = guest_registry.get(machine)
            if session is None:
                return web.json_response({"ok": False, "error": "unknown machine"}, status=404)
            return await self._proxy_to_remote(session, method, path, request, body=body)
        guest_client = self.topology.guest_client
        if guest_client is not None:
            return await self._proxy_via_host(guest_client, method, path, request, body=body)
        return web.json_response({"ok": False, "error": "no cluster routing available"}, status=503)

    async def _proxy_via_host(
        self,
        guest_client,
        method: str,
        path: str,
        request: web.Request,
        body: dict | None = None,
    ) -> web.Response:
        """Guest-side: forward an HTTP request to the host over the existing WS."""
        try:
            result = await guest_client.call(
                method, path, query=dict(request.query), body=body,
            )
        except asyncio.TimeoutError:
            return web.json_response({"ok": False, "error": "host timeout"}, status=504)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"host error: {e}"}, status=502)
        return web.json_response(result.get("body") or {}, status=int(result.get("status") or 200))

    async def _proxy_to_remote(
        self,
        session,
        method: str,
        path: str,
        request: web.Request,
        body: dict | None = None,
    ) -> web.Response:
        """Forward an HTTP request to a guest over WS RPC and return its response."""
        try:
            result = await session.call(
                method, path,
                query=dict(request.query),
                body=body,
            )
        except asyncio.TimeoutError:
            return web.json_response({"ok": False, "error": "remote timeout"}, status=504)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"remote error: {e}"}, status=502)
        return web.json_response(result.get("body") or {}, status=int(result.get("status") or 200))

    async def handle_guest_ws(self, request: web.Request) -> web.StreamResponse:
        """Permanent route — delegates to the GuestRegistry only when this
        node is the active host; otherwise returns 503 so the dialing peer
        falls back / reconnects elsewhere."""
        registry = self.topology.guest_registry
        if registry is None:
            return web.json_response(
                {"ok": False, "error": "not host"}, status=503,
            )
        return await registry.handle_ws(request)
