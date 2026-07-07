"""Request/reply over the cluster bus.

A caller names a target machine and an HTTP-shaped request; the reply comes back
correlated. Location-unified: the caller does not branch on a transport — the bus
routes (guest→host→guest relay included). Built on `bus.send` + `bus.subscribe`,
NOT a separate transport ("share the pipe, not the pattern"): request/reply is a
thin business layer over pub/sub.

Wire shape (all ordinary `packet` frames on the bus):
  request  : receiver=<target>, topic="request.<target>",
             payload={method, path, query, body, correlation_id, reply_machine}
  reply    : receiver=<reply_machine>, topic="reply.<reply_machine>.<correlation_id>",
             payload={status, body, correlation_id}

The responder runs the request against its OWN web port over a real 127.0.0.1 HTTP
loopback (so auth + the real _handle_web_* handlers run), then sends a reply packet.
Two-hop relay is the bus's job (receiver-based routing) — the loopback never relays.

Drop-in for the old ClusterRpc: same `dispatch_machine_request` + `handle_guest_ws`
surface, so the 13 web-server call sites and ClusterHttpRoutes are unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Callable

from aiohttp import ClientSession, web

from .cluster_bus import WIRE_VERSION as CLUSTER_BUS_WIRE_VERSION

logger = logging.getLogger(__name__)


class _RequestSubscriber:
    """Bus subscriber for inbound requests addressed to this machine. Schedules
    the async handler (create_task is fine here — requests are low-frequency and
    order-independent, unlike the chat hot path)."""

    def __init__(self, owner: "RequestReply") -> None:
        self._owner = owner

    def deliver(self, packet) -> None:
        self._owner._spawn_serve(packet.payload)


class _ReplySubscriber:
    """Bus subscriber for inbound replies to this machine's pending requests."""

    def __init__(self, owner: "RequestReply") -> None:
        self._owner = owner

    def deliver(self, packet) -> None:
        self._owner._on_reply(packet.payload)


class RequestReply:
    def __init__(
        self,
        *,
        bus,
        topology,
        local_web_port: int,
        local_web_token: str = "",
        id_factory: "Callable[[], str] | None" = None,
    ) -> None:
        self._bus = bus
        self._topology = topology
        self._local_web_port = local_web_port
        self._local_web_token = local_web_token
        self._id_factory: "Callable[[], str]" = id_factory or (lambda: uuid.uuid4().hex)
        # correlation_id -> (future, target_machine). target is kept so a peer going
        # unreachable can fail exactly the requests waiting on it.
        self._pending: dict[str, tuple[asyncio.Future, str]] = {}
        self._tasks: set = set()   # strong refs to in-flight responder tasks
        self._http_session: ClientSession | None = None
        local = topology.local_machine_id()
        bus.subscribe(f"request.{local}", _RequestSubscriber(self))
        bus.subscribe(f"reply.{local}.", _ReplySubscriber(self))

    @property
    def _local(self) -> str:
        return self._topology.local_machine_id()

    # ── drop-in ClusterRpc surface ─────────────────────────────────────────

    async def dispatch_machine_request(
        self, machine: str, method: str, path: str,
        request: web.Request, body: dict | None = None,
    ) -> "web.Response | None":
        """Forward to a remote machine and return its response. None when the
        target is local (caller continues with local handling).

        Fast-fail gate: before sending a doomed request, check the target's
        negotiated cluster-bus wire version. An incompatible peer (old / offline /
        version-mismatch) returns 502 in <1ms instead of hanging the full timeout,
        so the web UI never wedges its ~6 browser connection slots on it."""
        if machine == self._local:
            return None
        target_version = self._topology.version_for(machine)
        if target_version != CLUSTER_BUS_WIRE_VERSION:
            return web.json_response(
                {
                    "ok": False,
                    "error": (
                        f"{machine} is incompatible "
                        f"(wire version {target_version}, this node speaks {CLUSTER_BUS_WIRE_VERSION})"
                    ),
                },
                status=502,
            )
        reply = await self.request(
            machine, method, path, query=dict(request.query), body=body,
        )
        return web.json_response(
            reply.get("body") or {}, status=int(reply.get("status") or 200),
        )

    async def handle_guest_ws(self, request: web.Request) -> web.StreamResponse:
        """/api/guest/ws — delegate to the GuestRegistry when this node is host."""
        registry = self._topology.guest_registry
        if registry is None:
            return web.json_response({"ok": False, "error": "not host"}, status=503)
        return await registry.handle_ws(request)

    # ── caller side ────────────────────────────────────────────────────────

    async def request(
        self, target_machine: str, method: str, path: str,
        *, query: dict | None = None, body: dict | None = None, timeout: float = 8.0,
    ) -> dict:
        """Send a request to `target_machine`, await the correlated reply.
        Returns ``{"status": int, "body": dict}``. Fails fast (not a full timeout)
        if the bus reports the target unreachable mid-flight. The 8s default is a
        backstop only — the version pre-check and on_unreachable signal normally
        fail an unreachable peer far sooner."""
        correlation_id = self._id_factory()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[correlation_id] = (future, target_machine)
        try:
            self._bus.send(
                receiver=target_machine,
                topic=f"request.{target_machine}",
                payload={
                    "method": method, "path": path,
                    "query": query or {}, "body": body,
                    "correlation_id": correlation_id,
                    "reply_machine": self._local,
                },
                ts=0.0,
            )
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return {"status": 504, "body": {"ok": False, "error": f"request to {target_machine} timed out"}}
        finally:
            self._pending.pop(correlation_id, None)

    def _on_reply(self, payload: dict) -> None:
        correlation_id = str(payload.get("correlation_id") or "")
        entry = self._pending.get(correlation_id)
        if entry is None:
            return
        future, _target = entry
        if not future.done():
            future.set_result({
                "status": int(payload.get("status") or 502),
                "body": payload.get("body") or {},
            })

    def fail_unreachable(self, machine: str) -> None:
        """Fail every pending request targeting `machine` fast (bus signalled the
        peer is down / version-incompatible) instead of hanging to timeout."""
        for _correlation_id, (future, target) in list(self._pending.items()):
            if target == machine and not future.done():
                future.set_result({"status": 502, "body": {"ok": False, "error": f"{machine} unreachable"}})

    # ── responder side ─────────────────────────────────────────────────────

    def _spawn_serve(self, payload: dict) -> None:
        task = asyncio.create_task(self._serve_request(payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve_request(self, payload: dict) -> None:
        result = await self._loopback(
            str(payload.get("method") or "GET"),
            str(payload.get("path") or ""),
            payload.get("query") or {},
            payload.get("body"),
        )
        reply_machine = str(payload.get("reply_machine") or "")
        correlation_id = str(payload.get("correlation_id") or "")
        if not reply_machine or not correlation_id:
            return
        self._bus.send(
            receiver=reply_machine,
            topic=f"reply.{reply_machine}.{correlation_id}",
            payload={"status": result["status"], "body": result["body"], "correlation_id": correlation_id},
            ts=0.0,
        )

    async def _loopback(self, method: str, path: str, query: dict, body) -> dict:
        """Re-issue against this node's own web port over real 127.0.0.1 HTTP, so
        the real _handle_web_* handlers run with auth. Returns {status, body}."""
        if not self._local_web_port:
            return {"status": 503, "body": {"ok": False, "error": "loopback not configured"}}
        if self._http_session is None:
            self._http_session = ClientSession()
        url = f"http://127.0.0.1:{self._local_web_port}{path}"
        headers = {}
        if self._local_web_token:
            headers["Authorization"] = f"Bearer {self._local_web_token}"
        kwargs: dict = {"params": query, "headers": headers}
        if method.upper() != "GET" and body is not None:
            kwargs["json"] = body
        try:
            async with self._http_session.request(method, url, **kwargs) as response:
                try:
                    body_out = await response.json(content_type=None)
                except Exception:
                    body_out = {"raw": (await response.text())[:4096]}
                return {"status": response.status, "body": body_out}
        except Exception as exception:
            logger.warning("request_reply: loopback %s %s failed: %r", method, path, exception)
            return {"status": 502, "body": {"ok": False, "error": str(exception)}}

    async def aclose(self) -> None:
        if self._http_session is not None:
            try:
                await self._http_session.close()
            except Exception:
                pass
            self._http_session = None
