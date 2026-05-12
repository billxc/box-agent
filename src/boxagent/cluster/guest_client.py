"""Guest-side: dial host WS, register bots, serve incoming RPCs."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

import aiohttp
from aiohttp import ClientSession, WSMsgType

from . import devtunnel
from .registry import _PendingResponse

logger = logging.getLogger(__name__)





@dataclass
class GuestClient:
    """Maintains a long-lived WebSocket connection from this node to a host node.

    On each incoming RPC frame, the client re-issues the request against its
    own local web server (``http://127.0.0.1:<web_port>``) using the local
    web_token, and forwards the response (or SSE stream) back over the WS.
    """

    host_url: str           # e.g. https://abc-9292.jpe1.devtunnels.ms
    host_token: str         # cluster shared token (gates WS hello)
    machine_id: str
    local_web_port: int
    local_web_token: str = ""
    tunnel_name: str = ""   # devtunnel id, derived from host_url if empty
    bot_provider: Callable[[], list[dict]] = field(default=lambda: [])
    reconnect_delay: float = 3.0
    # Optional hook for frames the client doesn't natively handle (e.g.
    # event_batch / event_resync from the events syncer). Called with the
    # raw payload; should return True if consumed.
    on_unknown_frame: Callable[[dict], Awaitable[bool]] | None = None
    # Optional hooks fired when the WS connection is established / lost so the
    # syncer can attach/detach its peer (key: "host").
    on_connect: Callable[["GuestClient"], None] | None = None
    on_disconnect: Callable[[], None] | None = None

    _task: asyncio.Task | None = None
    _stop: bool = False
    _ws: aiohttp.ClientWebSocketResponse | None = None
    _session: ClientSession | None = None
    # Peers (host + other guests' workgroup-kind bots) pushed by host
    # via `peers_snapshot` frames. Each entry: {name, machine, online,
    # kind, description}. Read by Gateway._build_peer_descriptors so the
    # local admin sees cross-machine peers.
    remote_peers: list[dict] = field(default_factory=list)
    # Cluster machine list (host + all sats minus self), pushed by host via
    # `machines_snapshot` frames after every topology change. Read by guest-side
    # _handle_web_machines / _handle_web_bots so the local webui can render
    # the full cluster sidebar.
    remote_machines: list[dict] = field(default_factory=list)
    # Pending reverse RPCs we (guest) initiated against host.
    _pending: dict[str, _PendingResponse] = field(default_factory=dict, repr=False)

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop = False
        self._task = asyncio.create_task(self._run_forever(), name="guest-client")

    async def stop(self) -> None:
        self._stop = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def call(
        self,
        method: str,
        path: str,
        *,
        query: dict | None = None,
        body: dict | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """Reverse RPC: guest → host. Returns ``{"status": int, "body": dict}``.

        Mirrors :meth:`GuestSession.call` on the host side. Used by the
        guest-side webui to forward "remote machine" requests through the host,
        which then dispatches locally or proxies to another guest.
        """
        ws = self._ws
        if ws is None or ws.closed:
            raise RuntimeError("guest: not connected to host")
        rpc_id = uuid.uuid4().hex
        pending = _PendingResponse()
        self._pending[rpc_id] = pending
        try:
            await ws.send_json({
                "type": "rpc", "id": rpc_id, "method": method,
                "path": path, "query": query or {}, "body": body,
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
        """Reverse RPC streaming variant for SSE endpoints."""
        ws = self._ws
        if ws is None or ws.closed:
            raise RuntimeError("guest: not connected to host")
        rpc_id = uuid.uuid4().hex
        pending = _PendingResponse()
        pending.is_stream = True
        self._pending[rpc_id] = pending
        try:
            await ws.send_json({
                "type": "rpc", "id": rpc_id, "method": method,
                "path": path, "query": query or {}, "body": body,
            })
            while True:
                frame = await pending.stream_queue.get()
                if frame is None:
                    return
                yield frame
        finally:
            self._pending.pop(rpc_id, None)

    async def announce_bots(self) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            return
        try:
            await ws.send_json({"type": "bots_update", "bots": self.bot_provider()})
        except Exception as e:
            logger.debug("guest: bots_update failed: %s", e)

    async def fetch_host_json(
        self, path: str, query: dict | None = None,
        method: str = "GET", body: dict | None = None,
    ) -> dict:
        """Issue a one-shot HTTPS request against the host's web app and return JSON.

        Uses the same devtunnel auth flow as the WS connection. Lets guest-side
        endpoints (e.g. /api/version?cluster=1, /api/peer/send) reach back
        through the host without inventing a reverse-RPC channel.
        """
        if self._session is None:
            self._session = ClientSession()
        effective_tunnel_name = self.tunnel_name or devtunnel.tunnel_name_from_url(self.host_url)
        if not effective_tunnel_name:
            raise RuntimeError("guest: cannot derive tunnel name for fetch_host_json")
        if self.host_url:
            base = self.host_url.rstrip("/")
        else:
            base = (await devtunnel.resolve_url(effective_tunnel_name, port=self.local_web_port)).rstrip("/")
        devtunnel_token = await devtunnel.connect_token(effective_tunnel_name)
        headers = {"X-Tunnel-Authorization": f"tunnel {devtunnel_token}"}
        if self.host_token:
            headers["Authorization"] = f"Bearer {self.host_token}"
        url = f"{base}{path}"
        kwargs: dict = {"params": query or {}, "headers": headers}
        if body is not None:
            kwargs["json"] = body
        async with self._session.request(method, url, **kwargs) as response:
            try:
                return await response.json(content_type=None)
            except Exception:
                return {"ok": False, "error": f"non-json response status={response.status}"}

    async def _run_forever(self) -> None:
        effective_tunnel_name = self.tunnel_name or devtunnel.tunnel_name_from_url(self.host_url)
        if not effective_tunnel_name:
            logger.error("guest: cannot derive tunnel name from %s", self.host_url)
            return
        backoff = self.reconnect_delay
        while not self._stop:
            try:
                if self._session is None:
                    self._session = ClientSession()
                # Resolve the host URL fresh each attempt — host might have
                # rebuilt the tunnel.
                if self.host_url:
                    resolved_url = self.host_url
                else:
                    try:
                        resolved_url = await devtunnel.resolve_url(
                            effective_tunnel_name, port=self.local_web_port,
                        )
                    except Exception as e:
                        logger.warning("guest: tunnel URL resolution failed: %s", e)
                        await asyncio.sleep(min(backoff, 60.0))
                        backoff = min(backoff * 1.5, 60.0)
                        continue
                ws_url = self._derive_ws_url(resolved_url)
                # Mint a fresh devtunnel connect token each attempt.
                try:
                    devtunnel_token = await devtunnel.connect_token(effective_tunnel_name)
                except Exception as e:
                    logger.warning("guest: devtunnel token mint failed: %s", e)
                    await asyncio.sleep(min(backoff, 60.0))
                    backoff = min(backoff * 1.5, 60.0)
                    continue
                headers = {"X-Tunnel-Authorization": f"tunnel {devtunnel_token}"}
                logger.info("guest: connecting to host %s (tunnel %s)", ws_url, effective_tunnel_name)
                async with self._session.ws_connect(
                    ws_url, heartbeat=30.0, autoping=True, headers=headers,
                ) as ws:
                    self._ws = ws
                    backoff = self.reconnect_delay
                    await ws.send_json({
                        "type": "hello",
                        "machine_id": self.machine_id,
                        "token": self.host_token,
                        "bots": self.bot_provider(),
                    })
                    logger.info("guest: hello sent (machine_id=%s)", self.machine_id)
                    if self.on_connect is not None:
                        try:
                            self.on_connect(self)
                        except Exception as e:
                            logger.warning("guest: on_connect failed: %s", e)
                    try:
                        await self._serve(ws)
                    finally:
                        if self.on_disconnect is not None:
                            try:
                                self.on_disconnect()
                            except Exception as e:
                                logger.warning("guest: on_disconnect failed: %s", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("guest: connection failed: %s", e)
            finally:
                self._ws = None
                # Reject any in-flight reverse RPCs so callers see a clean
                # error instead of hanging until timeout.
                for p in list(self._pending.values()):
                    if not p.result.done():
                        p.result.set_exception(RuntimeError("guest: ws disconnected"))
                    if p.is_stream:
                        p.stream_queue.put_nowait(None)
                self._pending.clear()
            if self._stop:
                break
            await asyncio.sleep(min(backoff, 60.0))
            backoff = min(backoff * 1.5, 60.0)

    @staticmethod
    def _derive_ws_url(http_url: str) -> str:
        u = http_url.rstrip("/")
        if u.startswith("https://"):
            return "wss://" + u[len("https://"):] + "/api/guest/ws"
        if u.startswith("http://"):
            return "ws://" + u[len("http://"):] + "/api/guest/ws"
        return u + "/api/guest/ws"

    async def _serve(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except Exception:
                    continue
                if payload.get("type") == "rpc":
                    asyncio.create_task(self._handle_rpc(ws, payload))
                elif payload.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
                elif payload.get("type") == "welcome":
                    pass
                elif payload.get("type") == "rpc_resp":
                    p = self._pending.get(str(payload.get("id") or ""))
                    if p and not p.result.done():
                        p.result.set_result({
                            "status": int(payload.get("status") or 0),
                            "body": payload.get("body") or {},
                        })
                elif payload.get("type") == "rpc_stream":
                    p = self._pending.get(str(payload.get("id") or ""))
                    if p:
                        p.stream_queue.put_nowait(str(payload.get("data") or ""))
                elif payload.get("type") == "rpc_end":
                    p = self._pending.get(str(payload.get("id") or ""))
                    if p:
                        p.stream_queue.put_nowait(None)
                elif payload.get("type") == "machines_snapshot":
                    raw = payload.get("machines") or []
                    self.remote_machines = [
                        m for m in raw if isinstance(m, dict) and m.get("machine_id")
                    ]
                    logger.debug("guest: machines_snapshot received (%d machines)",
                                 len(self.remote_machines))
                elif payload.get("type") == "peers_snapshot":
                    # Host pushes the full cross-cluster peer list (host's
                    # local workgroups + other sats' workgroups, minus this
                    # guest's own). Replace cache wholesale.
                    raw = payload.get("peers") or []
                    self.remote_peers = [p for p in raw if isinstance(p, dict) and p.get("name")]
                    logger.debug("guest: peers_snapshot received (%d peers)", len(self.remote_peers))
                elif self.on_unknown_frame is not None:
                    try:
                        await self.on_unknown_frame(payload)
                    except Exception as e:
                        logger.warning("guest: on_unknown_frame failed: %s", e)
            elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                break

    async def _handle_rpc(self, ws: aiohttp.ClientWebSocketResponse, req: dict) -> None:
        rpc_id = str(req.get("id") or "")
        method = str(req.get("method") or "GET").upper()
        path = str(req.get("path") or "")
        query: dict = req.get("query") or {}
        body = req.get("body")

        url = f"http://127.0.0.1:{self.local_web_port}{path}"
        headers = {}
        if self.local_web_token:
            headers["Authorization"] = f"Bearer {self.local_web_token}"

        # SSE endpoints stream chunks — forward as rpc_stream frames
        is_sse = path.endswith("/api/stream")
        try:
            assert self._session is not None
            kwargs = {"params": query, "headers": headers}
            if method != "GET" and body is not None:
                kwargs["json"] = body
            async with self._session.request(method, url, **kwargs) as response:
                if is_sse and response.status == 200:
                    # Forward SSE frames as rpc_stream messages
                    buf = b""
                    async for chunk in response.content.iter_any():
                        buf += chunk
                        # Split on \n\n SSE event boundaries
                        while b"\n\n" in buf:
                            event, buf = buf.split(b"\n\n", 1)
                            for line in event.splitlines():
                                if line.startswith(b"data: "):
                                    data = line[6:].decode("utf-8", errors="replace")
                                    await ws.send_json({
                                        "type": "rpc_stream", "id": rpc_id, "data": data,
                                    })
                    await ws.send_json({"type": "rpc_end", "id": rpc_id})
                    return

                # Non-streaming: parse JSON (or wrap raw)
                try:
                    body_out = await response.json(content_type=None)
                except Exception:
                    body_out = {"raw": (await response.text())[:4096]}
                await ws.send_json({
                    "type": "rpc_resp", "id": rpc_id,
                    "status": response.status, "body": body_out,
                })
        except Exception as e:
            logger.warning("guest: rpc %s %s failed: %s", method, path, e)
            try:
                await ws.send_json({
                    "type": "rpc_resp", "id": rpc_id, "status": 502,
                    "body": {"ok": False, "error": str(e)},
                })
            except Exception:
                pass
