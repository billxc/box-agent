"""Cluster RPC dispatch — host↔guest proxying for HTTP and SSE."""

import asyncio
import logging

from aiohttp import web

logger = logging.getLogger(__name__)


class ClusterApiMixin:
    async def _dispatch_machine_request(
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
        Guest role: forward via GuestClient (new guest→host RPC); the
        host then dispatches locally or proxies onward to the right guest.
        """
        if machine == self._local_machine_id():
            return None
        if self._guest_registry is not None:
            sess = self._guest_registry.get(machine)
            if sess is None:
                return web.json_response({"ok": False, "error": "unknown machine"}, status=404)
            return await self._proxy_to_remote(sess, method, path, request, body=body)
        if self._guest_client is not None:
            return await self._proxy_via_host(method, path, request, body=body)
        return web.json_response({"ok": False, "error": "no cluster routing available"}, status=503)

    async def _dispatch_machine_stream(
        self,
        machine: str,
        path: str,
        request: web.Request,
    ) -> web.StreamResponse | None:
        """Streaming counterpart to `_dispatch_machine_request` for SSE."""
        if machine == self._local_machine_id():
            return None
        if self._guest_registry is not None:
            sess = self._guest_registry.get(machine)
            if sess is None:
                return web.json_response({"ok": False, "error": "unknown machine"}, status=404)
            return await self._proxy_stream_to_remote(sess, path, request)
        if self._guest_client is not None:
            return await self._proxy_via_host_stream(path, request)
        return web.json_response({"ok": False, "error": "no cluster routing available"}, status=503)

    async def _proxy_via_host(
        self,
        method: str,
        path: str,
        request: web.Request,
        body: dict | None = None,
    ) -> web.Response:
        """Guest-side: forward an HTTP request to the host over the existing WS."""
        try:
            result = await self._guest_client.call(
                method, path, query=dict(request.query), body=body,
            )
        except asyncio.TimeoutError:
            return web.json_response({"ok": False, "error": "host timeout"}, status=504)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"host error: {e}"}, status=502)
        return web.json_response(result.get("body") or {}, status=int(result.get("status") or 200))

    async def _proxy_via_host_stream(
        self,
        path: str,
        request: web.Request,
    ) -> web.StreamResponse:
        """Guest-side: forward an SSE GET to the host, relay frames to the browser."""
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)
        await resp.write(b": connected\n\n")
        try:
            async for data in self._guest_client.call_stream(
                "GET", path, query=dict(request.query),
            ):
                await resp.write(f"data: {data}\n\n".encode("utf-8"))
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def _proxy_to_remote(
        self,
        sess,
        method: str,
        path: str,
        request: web.Request,
        body: dict | None = None,
    ) -> web.Response:
        """Forward an HTTP request to a guest over WS RPC and return its response."""
        try:
            result = await sess.call(
                method, path,
                query=dict(request.query),
                body=body,
            )
        except asyncio.TimeoutError:
            return web.json_response({"ok": False, "error": "remote timeout"}, status=504)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"remote error: {e}"}, status=502)
        return web.json_response(result.get("body") or {}, status=int(result.get("status") or 200))

    async def _proxy_stream_to_remote(
        self,
        sess,
        path: str,
        request: web.Request,
    ) -> web.StreamResponse:
        """Forward an SSE GET to a guest, relay frames to the browser."""
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)
        await resp.write(b": connected\n\n")
        try:
            async for data in sess.call_stream("GET", path, query=dict(request.query)):
                await resp.write(f"data: {data}\n\n".encode("utf-8"))
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def _handle_guest_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Permanent route — delegates to the GuestRegistry only when this
        node is the active host; otherwise returns 503 so the dialing peer
        falls back / reconnects elsewhere."""
        registry = self._guest_registry
        if registry is None:
            return web.json_response(
                {"ok": False, "error": "not host"}, status=503,
            )
        return await registry.handle_ws(request)
