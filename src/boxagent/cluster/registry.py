"""Host-side: track connected guest nodes and proxy RPC to them.

A *host* node accepts WebSocket connections from *guest* nodes at
``/api/guest/ws`` (legacy alias: ``/api/sat/ws``). Each guest
registers its bots with a ``hello`` frame.
The host then forwards web-UI HTTP requests bound for a remote bot over
the WS to the guest that owns it, using a generic RPC envelope.

Wire protocol::

  # Guest → Host (immediately after open)
  {"type": "hello", "machine_id": "pc", "token": "...", "bots": [...]}

  # Host → Guest (a request)
  {"type": "rpc", "id": "<uuid>", "method": "GET",
   "path": "/api/history", "query": {"bot": "x", "chat_id": "y"}, "body": null}

  # Guest → Host (non-streaming response)
  {"type": "rpc_resp", "id": "<uuid>", "status": 200, "body": {...}}

  # Guest → Host (streaming response, e.g. SSE)
  {"type": "rpc_stream", "id": "<uuid>", "data": "<sse data line>"}
  ...
  {"type": "rpc_end",    "id": "<uuid>"}

  # Either direction
  {"type": "ping"}  /  {"type": "pong"}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import AsyncIterator

from aiohttp import ClientSession, web
from aiohttp.web import WebSocketResponse

logger = logging.getLogger(__name__)


@dataclass
class RemoteBot:
    """Metadata for a bot owned by a guest node."""

    name: str
    display_name: str = ""
    backend: str = ""
    model: str = ""
    kind: str = "bot"  # "bot" | "workgroup"


class _PendingResponse:
    """Future-like aggregator for a single RPC awaiting reply.

    Non-streaming RPCs resolve `result` once with a JSON dict.
    Streaming RPCs (used for SSE) push frames into `stream_queue` instead;
    callers iterate via :meth:`iter_stream` until ``rpc_end`` arrives.
    """

    __slots__ = ("result", "stream_queue", "is_stream")

    def __init__(self) -> None:
        self.result: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self.stream_queue: asyncio.Queue = asyncio.Queue()
        self.is_stream: bool = False


@dataclass
class GuestSession:
    """One connected guest node."""

    machine_id: str
    ws: WebSocketResponse
    bots: list[RemoteBot] = field(default_factory=list)
    _pending: dict[str, _PendingResponse] = field(default_factory=dict, repr=False)
    _closed: bool = False

    async def call(
        self,
        method: str,
        path: str,
        *,
        query: dict | None = None,
        body: dict | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """RPC: send request, await single response."""
        rpc_id = uuid.uuid4().hex
        pending = _PendingResponse()
        self._pending[rpc_id] = pending
        try:
            await self.ws.send_json({
                "type": "rpc",
                "id": rpc_id,
                "method": method,
                "path": path,
                "query": query or {},
                "body": body,
            })
            return await asyncio.wait_for(pending.result, timeout=timeout)
        finally:
            self._pending.pop(rpc_id, None)

    async def call_stream(
        self,
        method: str,
        path: str,
        *,
        query: dict | None = None,
        body: dict | None = None,
    ) -> AsyncIterator[str]:
        """RPC: send request, async-iterate stream frames until rpc_end."""
        rpc_id = uuid.uuid4().hex
        pending = _PendingResponse()
        pending.is_stream = True
        self._pending[rpc_id] = pending
        try:
            await self.ws.send_json({
                "type": "rpc",
                "id": rpc_id,
                "method": method,
                "path": path,
                "query": query or {},
                "body": body,
            })
            while True:
                frame = await pending.stream_queue.get()
                if frame is None:  # sentinel = rpc_end
                    return
                yield frame
        finally:
            self._pending.pop(rpc_id, None)

    def _resolve(self, rpc_id: str, status: int, body: dict) -> None:
        p = self._pending.get(rpc_id)
        if p and not p.result.done():
            p.result.set_result({"status": status, "body": body})

    def _push_stream(self, rpc_id: str, data: str) -> None:
        p = self._pending.get(rpc_id)
        if p:
            p.stream_queue.put_nowait(data)

    def _end_stream(self, rpc_id: str) -> None:
        p = self._pending.get(rpc_id)
        if p:
            p.stream_queue.put_nowait(None)


@dataclass
class GuestRegistry:
    """Host-side registry of currently-connected guests."""

    expected_token: str = ""
    sessions: dict[str, GuestSession] = field(default_factory=dict)
    # Machines we've seen this process lifetime; survives disconnect so the
    # web UI can show an offline tile instead of the row vanishing.
    history: dict[str, dict] = field(default_factory=dict)  # machine_id → {bots: [...], last_seen}
    # Optional: called by handle_ws after a guest's hello/bots_update, and after
    # any guest disconnects. Lets the host push a peers_snapshot to all (or just the
    # the changed) guests so each guest learns about the other workgroups in the
    # cluster. None = no push.
    on_topology_change: Callable[[str | None], Awaitable[None]] | None = None
    # Loopback config so the host can serve guest→host reverse RPCs by re-issuing
    # the request against its own web server (mirrors the guest-side pattern
    # in guest_client._handle_rpc). Injected by gateway after web app starts.
    local_web_port: int = 0
    local_web_token: str = ""
    _http_session: ClientSession | None = field(default=None, repr=False)

    def get(self, machine_id: str) -> GuestSession | None:
        return self.sessions.get(machine_id)

    def list_machines(self) -> list[dict]:
        """All known machines: connected + recently seen."""
        out: list[dict] = []
        seen: set[str] = set()
        now = time.time()
        for mid, sess in self.sessions.items():
            seen.add(mid)
            out.append({
                "machine_id": mid,
                "online": True,
                "bots": [
                    {"name": b.name, "display_name": b.display_name,
                     "backend": b.backend, "model": b.model, "kind": b.kind}
                    for b in sess.bots
                ],
                "last_seen": now,
            })
        for mid, info in self.history.items():
            if mid in seen:
                continue
            out.append({
                "machine_id": mid,
                "online": False,
                "bots": info.get("bots") or [],
                "last_seen": info.get("last_seen") or 0,
            })
        return out

    def list_bots(self) -> list[tuple[str, RemoteBot]]:
        """Yield (machine_id, RemoteBot) for every registered remote bot."""
        out: list[tuple[str, RemoteBot]] = []
        for mid, sess in self.sessions.items():
            for b in sess.bots:
                out.append((mid, b))
        return out

    def get_bot(self, machine_id: str, name: str) -> RemoteBot | None:
        """Return the RemoteBot named `name` on guest `machine_id`, or None."""
        sess = self.sessions.get(machine_id)
        if sess is None:
            return None
        for b in sess.bots:
            if b.name == name:
                return b
        return None

    async def aclose(self) -> None:
        if self._http_session is not None:
            try:
                await self._http_session.close()
            except Exception:
                pass
            self._http_session = None

    async def close_all_sessions(self) -> None:
        """Force-close every connected guest WS. Used by HostElection
        during demote so guests immediately reconnect to the new active host
        instead of dangling on a soon-to-be-stopped tunnel."""
        for sess in list(self.sessions.values()):
            sess._closed = True
            try:
                await sess.ws.close()
            except Exception:
                pass
        self.sessions.clear()

    async def _serve_inbound_rpc(self, sess: GuestSession, req: dict) -> None:
        """Handle an `rpc` frame coming *from* a guest.

        Mirrors `GuestClient._handle_rpc`: re-issue the request against the
        host's own web port over loopback, then stream the response back to the
        guest as `rpc_resp` (or `rpc_stream` + `rpc_end` for SSE). Reusing the
        loopback HTTP path means the host's full `_handle_web_*` logic — which
        already includes host→guest proxying — handles routing for free.
        """
        rpc_id = str(req.get("id") or "")
        method = str(req.get("method") or "GET").upper()
        path = str(req.get("path") or "")
        query: dict = req.get("query") or {}
        body = req.get("body")

        if not self.local_web_port:
            try:
                await sess.ws.send_json({
                    "type": "rpc_resp", "id": rpc_id, "status": 503,
                    "body": {"ok": False, "error": "host loopback not configured"},
                })
            except Exception:
                pass
            return

        if self._http_session is None:
            self._http_session = ClientSession()
        url = f"http://127.0.0.1:{self.local_web_port}{path}"
        headers = {}
        if self.local_web_token:
            headers["Authorization"] = f"Bearer {self.local_web_token}"

        is_sse = path.endswith("/api/stream")
        try:
            kwargs: dict = {"params": query, "headers": headers}
            if method != "GET" and body is not None:
                kwargs["json"] = body
            async with self._http_session.request(method, url, **kwargs) as resp:
                if is_sse and resp.status == 200:
                    buf = b""
                    async for chunk in resp.content.iter_any():
                        buf += chunk
                        while b"\n\n" in buf:
                            event, buf = buf.split(b"\n\n", 1)
                            for line in event.splitlines():
                                if line.startswith(b"data: "):
                                    data = line[6:].decode("utf-8", errors="replace")
                                    await sess.ws.send_json({
                                        "type": "rpc_stream", "id": rpc_id, "data": data,
                                    })
                    await sess.ws.send_json({"type": "rpc_end", "id": rpc_id})
                    return
                try:
                    body_out = await resp.json(content_type=None)
                except Exception:
                    body_out = {"raw": (await resp.text())[:4096]}
                await sess.ws.send_json({
                    "type": "rpc_resp", "id": rpc_id,
                    "status": resp.status, "body": body_out,
                })
        except Exception as e:
            logger.warning("host: inbound rpc %s %s failed: %s", method, path, e)
            try:
                await sess.ws.send_json({
                    "type": "rpc_resp", "id": rpc_id, "status": 502,
                    "body": {"ok": False, "error": str(e)},
                })
            except Exception:
                pass

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Aiohttp handler for /api/guest/ws."""
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)

        # Expect hello
        sess: GuestSession | None = None
        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(msg.data)
                except Exception:
                    logger.warning("guest ws: invalid JSON frame")
                    continue
                t = payload.get("type")

                if sess is None:
                    if t != "hello":
                        await ws.close(code=4001, message=b"expected hello")
                        return ws
                    if self.expected_token and payload.get("token") != self.expected_token:
                        await ws.close(code=4003, message=b"bad token")
                        return ws
                    machine_id = str(payload.get("machine_id") or "").strip()
                    if not machine_id:
                        await ws.close(code=4002, message=b"missing machine_id")
                        return ws
                    bots_raw = payload.get("bots") or []
                    bots = [
                        RemoteBot(
                            name=str(b.get("name") or ""),
                            display_name=str(b.get("display_name") or ""),
                            backend=str(b.get("backend") or ""),
                            model=str(b.get("model") or ""),
                            kind=str(b.get("kind") or "bot"),
                        )
                        for b in bots_raw
                        if isinstance(b, dict) and b.get("name")
                    ]
                    sess = GuestSession(machine_id=machine_id, ws=ws, bots=bots)
                    # If a previous session with this machine_id is still around,
                    # evict it (a guest reconnect).
                    old = self.sessions.get(machine_id)
                    if old is not None:
                        old._closed = True
                        try:
                            await old.ws.close()
                        except Exception:
                            pass
                    self.sessions[machine_id] = sess
                    logger.info("guest '%s' connected with %d bot(s)", machine_id, len(bots))
                    await ws.send_json({"type": "welcome"})
                    if self.on_topology_change is not None:
                        try:
                            await self.on_topology_change(machine_id)
                        except Exception as e:
                            logger.warning("on_topology_change(hello) failed: %s", e)
                    continue

                if t == "ping":
                    await ws.send_json({"type": "pong"})
                elif t == "rpc":
                    # Guest → host reverse RPC: serve via localhost loopback so
                    # we reuse all of host's existing _handle_web_* logic
                    # (incl. host→guest proxy if the target is yet another guest).
                    asyncio.create_task(self._serve_inbound_rpc(sess, payload))
                elif t == "rpc_resp":
                    sess._resolve(
                        str(payload.get("id") or ""),
                        int(payload.get("status") or 0),
                        payload.get("body") or {},
                    )
                elif t == "rpc_stream":
                    sess._push_stream(
                        str(payload.get("id") or ""),
                        str(payload.get("data") or ""),
                    )
                elif t == "rpc_end":
                    sess._end_stream(str(payload.get("id") or ""))
                elif t == "bots_update":
                    # Guest re-announces its bot list (e.g. after dynamic create)
                    bots_raw = payload.get("bots") or []
                    sess.bots = [
                        RemoteBot(
                            name=str(b.get("name") or ""),
                            display_name=str(b.get("display_name") or ""),
                            backend=str(b.get("backend") or ""),
                            model=str(b.get("model") or ""),
                            kind=str(b.get("kind") or "bot"),
                        )
                        for b in bots_raw
                        if isinstance(b, dict) and b.get("name")
                    ]
                    if self.on_topology_change is not None:
                        try:
                            await self.on_topology_change(sess.machine_id)
                        except Exception as e:
                            logger.warning("on_topology_change(bots_update) failed: %s", e)
        finally:
            if sess is not None and not sess._closed:
                self.sessions.pop(sess.machine_id, None)
                # Remember bots so the UI keeps showing the row as "offline"
                self.history[sess.machine_id] = {
                    "bots": [
                        {"name": b.name, "display_name": b.display_name,
                         "backend": b.backend, "model": b.model, "kind": b.kind}
                        for b in sess.bots
                    ],
                    "last_seen": time.time(),
                }
                logger.info("guest '%s' disconnected", sess.machine_id)
                if self.on_topology_change is not None:
                    try:
                        await self.on_topology_change(None)
                    except Exception as e:
                        logger.warning("on_topology_change(disconnect) failed: %s", e)
        return ws
