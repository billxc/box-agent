"""Guest-side: dial host WS, register bots, serve incoming RPCs."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import aiohttp
from aiohttp import ClientSession, WSMsgType

from . import devtunnel
from .cluster_bus import WIRE_VERSION as CLUSTER_BUS_WIRE_VERSION
from .peer_transport import WIRE_VERSION
from boxagent.log import Category, log

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
    # The process ClusterBus (duck-typed). When set, the host link is registered
    # with it and inbound `packet` frames are routed to it. Injected by gateway.
    cluster_bus: object | None = None

    _task: asyncio.Task | None = None
    _stop: bool = False
    _ws: aiohttp.ClientWebSocketResponse | None = None
    _session: ClientSession | None = None
    # Cluster machine list (host + all sats minus self), pushed by host via
    # `machines_snapshot` frames after every topology change. Read by guest-side
    # _handle_web_machines / _handle_web_bots so the local webui can render
    # the full cluster sidebar.
    remote_machines: list[dict] = field(default_factory=list)

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
                        log.warning(
                            Category.CLUSTER_TUNNEL_ERROR,
                            "guest: tunnel URL resolution failed",
                            tunnel=effective_tunnel_name, error=repr(e),
                        )
                        await asyncio.sleep(min(backoff, 60.0))
                        backoff = min(backoff * 1.5, 60.0)
                        continue
                ws_url = self._derive_ws_url(resolved_url)
                # Mint a fresh devtunnel connect token each attempt.
                try:
                    devtunnel_token = await devtunnel.connect_token(effective_tunnel_name)
                except Exception as e:
                    logger.warning("guest: devtunnel token mint failed: %s", e)
                    log.warning(
                        Category.CLUSTER_TUNNEL_ERROR,
                        "guest: devtunnel token mint failed",
                        tunnel=effective_tunnel_name, error=repr(e),
                    )
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
                        "v": CLUSTER_BUS_WIRE_VERSION,
                        "machine_id": self.machine_id,
                        "token": self.host_token,
                        "bots": self.bot_provider(),
                    })
                    # NB: the ClusterBus link is attached on the `welcome` frame
                    # (see _serve), not here — the host's wire version is only
                    # known once welcome arrives, so we negotiate it there.
                    logger.info("guest: hello sent (machine_id=%s)", self.machine_id)
                    log.info(
                        Category.CLUSTER_GUEST_CONNECTED,
                        f"guest connected to host (tunnel {effective_tunnel_name})",
                        machine_id=self.machine_id, tunnel=effective_tunnel_name, ws_url=ws_url,
                    )
                    if self.on_connect is not None:
                        try:
                            self.on_connect(self)
                        except Exception as e:
                            logger.warning("guest: on_connect failed: %s", e)
                            log.warning(
                                Category.CLUSTER_PROTOCOL_ERROR,
                                "guest: on_connect callback failed",
                                machine_id=self.machine_id, error=repr(e),
                            )
                    try:
                        await self._serve(ws)
                    finally:
                        if self.on_disconnect is not None:
                            try:
                                self.on_disconnect()
                            except Exception as e:
                                logger.warning("guest: on_disconnect failed: %s", e)
                                log.warning(
                                    Category.CLUSTER_PROTOCOL_ERROR,
                                    "guest: on_disconnect callback failed",
                                    machine_id=self.machine_id, error=repr(e),
                                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("guest: connection failed: %s", e)
                log.warning(
                    Category.CLUSTER_GUEST_DISCONNECTED,
                    "guest: connection failed",
                    machine_id=self.machine_id, tunnel=effective_tunnel_name, error=repr(e),
                )
            finally:
                self._ws = None
                if self.cluster_bus is not None:
                    self.cluster_bus.detach_link("host")
            if self._stop:
                break
            await asyncio.sleep(min(backoff, 60.0))
            backoff = min(backoff * 1.5, 60.0)

    @staticmethod
    def _derive_ws_url(http_url: str) -> str:
        url = http_url.rstrip("/")
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):] + "/api/guest/ws"
        if url.startswith("http://"):
            return "ws://" + url[len("http://"):] + "/api/guest/ws"
        return url + "/api/guest/ws"

    async def _serve(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except Exception:
                    continue
                if payload.get("type") == "packet":
                    # Unified cluster bus: route to ClusterBus (own v3 gate).
                    # Intercept before the legacy v2 gate below.
                    if self.cluster_bus is not None:
                        self.cluster_bus.on_inbound("host", payload)
                    continue
                if payload.get("type") == "welcome":
                    # Handled BEFORE the legacy v2 gate: welcome carries the
                    # cluster-bus wire version (v3), which the v2 gate would drop.
                    # Attach the host link now, with the host's negotiated version
                    # (missing v = old host = 0 = incompatible → fast-fail later).
                    host_version = int(payload.get("v") or 0)
                    if self.cluster_bus is not None:
                        self.cluster_bus.attach_link("host", ws.send_json, version=host_version)
                    logger.info("guest: welcome received (host wire v%d)", host_version)
                    continue
                if payload.get("type") == "machines_snapshot":
                    # Also handled before the v2 gate: the host stamps snapshots
                    # with type only (no `v`); each machine descriptor carries its
                    # own `version` field, consumed as-is below.
                    raw = payload.get("machines") or []
                    self.remote_machines = [
                        m for m in raw if isinstance(m, dict) and m.get("machine_id")
                    ]
                    logger.debug("guest: machines_snapshot received (%d machines)",
                                 len(self.remote_machines))
                    continue
                if payload.get("v", WIRE_VERSION) != WIRE_VERSION:
                    logger.warning("guest: dropping frame with unsupported wire version %r",
                                   payload.get("v"))
                    continue
                if payload.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
                elif self.on_unknown_frame is not None:
                    try:
                        await self.on_unknown_frame(payload)
                    except Exception as e:
                        logger.warning("guest: on_unknown_frame failed: %s", e)
                        log.warning(
                            Category.CLUSTER_PROTOCOL_ERROR,
                            "guest: on_unknown_frame failed",
                            machine_id=self.machine_id, error=repr(e),
                        )
            elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                break

