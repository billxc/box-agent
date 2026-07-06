"""Role-agnostic RPC request/reply over the cluster WebSocket transport.

RPC = correlated request/reply. It rides the transport layer (the same WS the
syncers use), NOT ``MessageBus.publish``/topics — it needs id-correlation plus
concurrency, the opposite of the syncers' serial ordered fan-out.

Before this module the request/reply half of RPC existed TWICE, mirrored by role:
``GuestSession.call`` (host side, registry.py) vs ``GuestClient.call`` (guest
side, guest_client.py), and ``GuestRegistry._serve_inbound_rpc`` (host loopback)
vs ``GuestClient._handle_rpc`` (guest loopback). The two halves differed only in
which WS they send over, which HTTP session they loop back through, and a couple
of log strings. This module collapses each mirror into ONE implementation:

- :class:`RpcChannel` — the caller side. Mint an ``rpc_id``, park a future in a
  per-link ``_pending`` map, send the ``{"type": "rpc", ...}`` frame via an
  injected ``send_frame`` callable, await the correlated reply, clean up. Both
  ``GuestSession`` (per-guest on the host) and ``GuestClient`` (the single guest
  dialer) compose one of these.
- :class:`InboundRequestExecutor` — the ONE loopback re-issuer. An inbound
  ``rpc`` frame is replayed against the node's OWN web port over REAL HTTP so the
  node's real ``_handle_web_*`` handlers run. That loopback is load-bearing: for
  a guest→host→guest request the host's re-issued request itself hits
  ``dispatch_machine_request`` and forwards onward to the second guest, giving
  two-hop relay for free. It must stay a genuine ``127.0.0.1`` HTTP loopback —
  an in-process shortcut would skip auth, machine-resolution, and onward
  dispatch, silently breaking two-hop.

The wire frames (``{"type": "rpc", ...}`` / ``{"type": "rpc_resp", "v": WIRE_VERSION, ...}``) are
unchanged; frame unification is a later phase.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Awaitable, Callable

from aiohttp import ClientSession

from boxagent.log import log

from .peer_transport import WIRE_VERSION

SendFrame = Callable[[dict], Awaitable[None]]


class _PendingResponse:
    """Future-like holder for a single RPC awaiting its reply.

    Resolves ``result`` once with a JSON dict (``{"status": int, "body": dict}``).
    """

    __slots__ = ("result",)

    def __init__(self) -> None:
        self.result: asyncio.Future[dict] = asyncio.get_event_loop().create_future()


class RpcChannel:
    """Caller side of RPC: correlated request/reply over one WS link.

    Owns the per-link ``_pending`` map (kept per-link, NOT bus-global, so
    reject-on-disconnect is a plain clear — no peer scan). Host and guest are
    identical here; the role-split ``GuestSession.call`` / ``GuestClient.call``
    existed only because this code lived in two files.
    """

    __slots__ = ("_pending",)

    def __init__(self) -> None:
        self._pending: dict[str, _PendingResponse] = {}

    @property
    def pending(self) -> dict[str, _PendingResponse]:
        return self._pending

    async def call(
        self,
        send_frame: SendFrame,
        method: str,
        path: str,
        *,
        query: dict | None = None,
        body: dict | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """Send an RPC request, await the single correlated reply.

        ``send_frame`` is the WS-send callable for this link. Returns
        ``{"status": int, "body": dict}``.
        """
        rpc_id = uuid.uuid4().hex
        pending = _PendingResponse()
        self._pending[rpc_id] = pending
        try:
            await send_frame({
                "type": "rpc", "v": WIRE_VERSION,
                "id": rpc_id,
                "method": method,
                "path": path,
                "query": query or {},
                "body": body,
            })
            return await asyncio.wait_for(pending.result, timeout=timeout)
        finally:
            self._pending.pop(rpc_id, None)

    def resolve(self, rpc_id: str, status: int, body: dict) -> None:
        """Correlate an inbound ``rpc_resp`` frame back to its waiter."""
        pending = self._pending.get(rpc_id)
        if pending is not None and not pending.result.done():
            pending.result.set_result({"status": status, "body": body})

    def reject_all(self, exception: BaseException) -> None:
        """Fail every in-flight caller on link drop so nobody hangs to timeout."""
        for pending in list(self._pending.values()):
            if not pending.result.done():
                pending.result.set_exception(exception)
        self._pending.clear()


class InboundRequestExecutor:
    """The ONE loopback re-issuer, shared by host and guest.

    Reads a parsed ``rpc`` frame, replays it against ``127.0.0.1:<local_web_port>``
    over REAL HTTP so the node's real ``_handle_web_*`` handlers run, then sends
    the response back as an ``rpc_resp`` frame via ``send_reply``.

    The three former host/guest differences are parameters, not forks:
    ``http_session_provider`` (which client session to loop through),
    ``logger_message`` / ``event_message`` / ``fail_category`` (log identity),
    and ``require_web_port`` (the host's "loopback not configured"→503 guard,
    universally safe — a no-op on the guest whose port is always set). The two
    role log strings are kept byte-identical by passing them in verbatim.
    """

    def __init__(
        self,
        *,
        local_web_port: int,
        local_web_token: str,
        http_session_provider: Callable[[], ClientSession],
        logger_message: str,
        event_message_prefix: str,
        fail_category: str,
        not_configured_error: str,
        machine_id: str,
        require_web_port: bool = True,
    ) -> None:
        self._local_web_port = local_web_port
        self._local_web_token = local_web_token
        self._http_session_provider = http_session_provider
        self._logger_message = logger_message
        self._event_message_prefix = event_message_prefix
        self._fail_category = fail_category
        self._not_configured_error = not_configured_error
        self._machine_id = machine_id
        self._require_web_port = require_web_port

    @property
    def local_web_port(self) -> int:
        return self._local_web_port

    async def serve(self, send_reply: SendFrame, request: dict) -> None:
        """Re-issue one inbound ``rpc`` frame over loopback, reply ``rpc_resp``."""
        import logging

        logger = logging.getLogger("boxagent.cluster.rpc_over_bus")

        rpc_id = str(request.get("id") or "")
        method = str(request.get("method") or "GET").upper()
        path = str(request.get("path") or "")
        query: dict = request.get("query") or {}
        body = request.get("body")

        if self._require_web_port and not self._local_web_port:
            try:
                await send_reply({
                    "type": "rpc_resp", "v": WIRE_VERSION, "id": rpc_id, "status": 503,
                    "body": {"ok": False, "error": self._not_configured_error},
                })
            except Exception:
                pass
            return

        http_session = self._http_session_provider()
        url = f"http://127.0.0.1:{self._local_web_port}{path}"
        headers = {}
        if self._local_web_token:
            headers["Authorization"] = f"Bearer {self._local_web_token}"

        try:
            kwargs: dict = {"params": query, "headers": headers}
            if method != "GET" and body is not None:
                kwargs["json"] = body
            async with http_session.request(method, url, **kwargs) as response:
                try:
                    body_out = await response.json(content_type=None)
                except Exception:
                    body_out = {"raw": (await response.text())[:4096]}
                await send_reply({
                    "type": "rpc_resp", "v": WIRE_VERSION, "id": rpc_id,
                    "status": response.status, "body": body_out,
                })
        except Exception as exception:
            logger.warning(self._logger_message, method, path, exception)
            log.warning(
                self._fail_category,
                f"{self._event_message_prefix}{method} {path} failed",
                machine_id=self._machine_id, method=method, path=path,
                error=repr(exception),
            )
            try:
                await send_reply({
                    "type": "rpc_resp", "v": WIRE_VERSION, "id": rpc_id, "status": 502,
                    "body": {"ok": False, "error": str(exception)},
                })
            except Exception:
                pass
