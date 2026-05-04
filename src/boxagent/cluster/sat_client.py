"""Satellite-side: dial host WS, register bots, serve incoming RPCs."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from typing import Callable

import aiohttp
from aiohttp import ClientSession, WSMsgType

logger = logging.getLogger(__name__)


async def _devtunnel_resolve_url(tunnel_name: str, port: int = 9292) -> str:
    """Look up the public portUri of a tunnel by name. Same Microsoft account
    only — that's our auth model."""
    if not shutil.which("devtunnel"):
        raise RuntimeError("devtunnel CLI not found on PATH")
    proc = await asyncio.create_subprocess_exec(
        "devtunnel", "show", tunnel_name, "-j",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"devtunnel show '{tunnel_name}' failed: "
            + err.decode("utf-8", "replace").strip()
        )
    try:
        data = json.loads(out)
    except Exception as e:
        raise RuntimeError(f"devtunnel show: bad JSON: {e}")
    tunnel = data.get("tunnel") or {}
    for p in tunnel.get("ports") or []:
        if int(p.get("portNumber") or 0) == port:
            url = str(p.get("portUri") or "").rstrip("/")
            if url:
                return url
    raise RuntimeError(
        f"tunnel '{tunnel_name}' has no port {port} or hasn't been hosted yet"
    )


async def _devtunnel_connect_token(tunnel_name: str) -> str:
    """Use the locally-authenticated devtunnel CLI to mint a connect JWT.

    The satellite host machine is expected to be logged in via `devtunnel user
    login` against the same Microsoft account that owns the host's cluster
    tunnel.  This is what gates membership at the devtunnel layer — without
    this token the satellite cannot even reach the host's HTTP server.
    """
    if not shutil.which("devtunnel"):
        raise RuntimeError("devtunnel CLI not found on PATH")
    proc = await asyncio.create_subprocess_exec(
        "devtunnel", "token", tunnel_name, "--scopes", "connect",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"devtunnel token failed: {err.decode('utf-8', 'replace').strip()}"
        )
    text = out.decode("utf-8", "replace")
    m = re.search(r"^Token:\s*(\S+)\s*$", text, re.MULTILINE)
    if not m:
        raise RuntimeError("devtunnel token: no token in output")
    return m.group(1)


def _tunnel_name_from_url(url: str) -> str:
    """Extract tunnel id from a portUri like https://abc-9292.jpe1.devtunnels.ms/."""
    m = re.match(r"https?://([^.-]+)(?:-\d+)?\.([^.]+)\.devtunnels\.ms/?", url)
    if not m:
        return ""
    return f"{m.group(1)}.{m.group(2)}"


@dataclass
class SatelliteClient:
    """Maintains a long-lived WebSocket connection from this node to a host node.

    On each incoming RPC frame, the client re-issues the request against its
    own local web server (``http://127.0.0.1:<web_port>``) using the local
    web_token, and forwards the response (or SSE stream) back over the WS.
    """

    host_url: str           # e.g. https://abc-9292.jpe1.devtunnels.ms
    host_token: str         # cluster shared token (gates WS hello)
    machine_id: str
    local_web_port: int
    tunnel_name: str = ""   # devtunnel id, derived from host_url if empty
    local_web_port: int
    local_web_token: str = ""
    bot_provider: Callable[[], list[dict]] = field(default=lambda: [])
    reconnect_delay: float = 3.0

    _task: asyncio.Task | None = None
    _stop: bool = False
    _ws: aiohttp.ClientWebSocketResponse | None = None
    _session: ClientSession | None = None
    # Peers (host + other satellites' workgroup-kind bots) pushed by host
    # via `peers_snapshot` frames. Each entry: {name, machine, online,
    # kind, description}. Read by Gateway._build_peer_descriptors so the
    # local admin sees cross-machine peers.
    remote_peers: list[dict] = field(default_factory=list)

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop = False
        self._task = asyncio.create_task(self._run_forever(), name="sat-client")

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
        """Push an updated bot list to the host (after dynamic create/delete)."""
        ws = self._ws
        if ws is None or ws.closed:
            return
        try:
            await ws.send_json({"type": "bots_update", "bots": self.bot_provider()})
        except Exception as e:
            logger.debug("sat: bots_update failed: %s", e)

    async def _run_forever(self) -> None:
        effective_tunnel_name = self.tunnel_name or _tunnel_name_from_url(self.host_url)
        if not effective_tunnel_name:
            logger.error("sat: cannot derive tunnel name from %s", self.host_url)
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
                        resolved_url = await _devtunnel_resolve_url(
                            effective_tunnel_name, port=self.local_web_port,
                        )
                    except Exception as e:
                        logger.warning("sat: tunnel URL resolution failed: %s", e)
                        await asyncio.sleep(min(backoff, 60.0))
                        backoff = min(backoff * 1.5, 60.0)
                        continue
                ws_url = self._derive_ws_url(resolved_url)
                # Mint a fresh devtunnel connect token each attempt.
                try:
                    devtunnel_token = await _devtunnel_connect_token(effective_tunnel_name)
                except Exception as e:
                    logger.warning("sat: devtunnel token mint failed: %s", e)
                    await asyncio.sleep(min(backoff, 60.0))
                    backoff = min(backoff * 1.5, 60.0)
                    continue
                headers = {"X-Tunnel-Authorization": f"tunnel {devtunnel_token}"}
                logger.info("sat: connecting to host %s (tunnel %s)", ws_url, effective_tunnel_name)
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
                    logger.info("sat: hello sent (machine_id=%s)", self.machine_id)
                    await self._serve(ws)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("sat: connection failed: %s", e)
            finally:
                self._ws = None
            if self._stop:
                break
            await asyncio.sleep(min(backoff, 60.0))
            backoff = min(backoff * 1.5, 60.0)

    @staticmethod
    def _derive_ws_url(http_url: str) -> str:
        u = http_url.rstrip("/")
        if u.startswith("https://"):
            return "wss://" + u[len("https://"):] + "/api/sat/ws"
        if u.startswith("http://"):
            return "ws://" + u[len("http://"):] + "/api/sat/ws"
        return u + "/api/sat/ws"

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
                elif payload.get("type") == "peers_snapshot":
                    # Host pushes the full cross-cluster peer list (host's
                    # local workgroups + other sats' workgroups, minus this
                    # sat's own). Replace cache wholesale.
                    raw = payload.get("peers") or []
                    self.remote_peers = [p for p in raw if isinstance(p, dict) and p.get("name")]
                    logger.debug("sat: peers_snapshot received (%d peers)", len(self.remote_peers))
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
            async with self._session.request(method, url, **kwargs) as resp:
                if is_sse and resp.status == 200:
                    # Forward SSE frames as rpc_stream messages
                    buf = b""
                    async for chunk in resp.content.iter_any():
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
                    body_out = await resp.json(content_type=None)
                except Exception:
                    body_out = {"raw": (await resp.text())[:4096]}
                await ws.send_json({
                    "type": "rpc_resp", "id": rpc_id,
                    "status": resp.status, "body": body_out,
                })
        except Exception as e:
            logger.warning("sat: rpc %s %s failed: %s", method, path, e)
            try:
                await ws.send_json({
                    "type": "rpc_resp", "id": rpc_id, "status": 502,
                    "body": {"ok": False, "error": str(e)},
                })
            except Exception:
                pass
