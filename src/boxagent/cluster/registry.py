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

  # Guest → Host (response)
  {"type": "rpc_resp", "id": "<uuid>", "status": 200, "body": {...}}

  # Either direction
  {"type": "ping"}  /  {"type": "pong"}

Frames the registry doesn't recognize (``event_batch`` / ``event_resync`` from
the EventSyncer, ``chat_subscribe`` / ``chat_event`` from the ChatSyncer) fall
through to ``on_unknown_frame``. Live chat SSE used to ride ``rpc_stream`` /
``rpc_end`` here; it now rides the ChatSyncer's ``chat_*`` frames instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from aiohttp import ClientSession, web
from aiohttp.web import WebSocketResponse

from boxagent.cluster.rpc_over_bus import (
    InboundRequestExecutor,
    RpcChannel,
    _PendingResponse,
)
from boxagent.log import Category, log

from .peer_transport import WIRE_VERSION

logger = logging.getLogger(__name__)


@dataclass
class RemoteBot:
    """Metadata for a bot owned by a guest node."""

    name: str
    display_name: str = ""
    backend: str = ""
    model: str = ""
    kind: str = "bot"


@dataclass
class GuestSession:
    """One connected guest node."""

    machine_id: str
    ws: WebSocketResponse
    bots: list[RemoteBot] = field(default_factory=list)
    _channel: RpcChannel = field(default_factory=RpcChannel, repr=False)
    _closed: bool = False

    @property
    def _pending(self) -> dict[str, _PendingResponse]:
        return self._channel.pending

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
        return await self._channel.call(
            self.ws.send_json, method, path,
            query=query, body=body, timeout=timeout,
        )

    def _resolve(self, rpc_id: str, status: int, body: dict) -> None:
        self._channel.resolve(rpc_id, status, body)


@dataclass
class GuestRegistry:
    """Host-side registry of currently-connected guests."""

    expected_token: str = ""
    sessions: dict[str, GuestSession] = field(default_factory=dict)
    # Machines we've seen this process lifetime; survives disconnect so the
    # web UI can show an offline tile instead of the row vanishing.
    history: dict[str, dict] = field(default_factory=dict)  # machine_id → {bots: [...], last_seen}
    # Optional: called by handle_ws after a guest's hello/bots_update, and after
    # any guest disconnects. Lets the host push a machines_snapshot to all (or
    # just the changed) guests so each guest learns the current cluster topology.
    # None = no push.
    on_topology_change: Callable[[str | None], Awaitable[None]] | None = None
    # Optional: called for any unknown frame type from a guest. Returns True
    # if the frame was consumed. Used by the events syncer to handle
    # event_batch / event_resync frames without bloating the registry.
    on_unknown_frame: Callable[[str, dict], Awaitable[bool]] | None = None
    # Optional: called when a guest hello/welcome handshake completes, so the
    # syncer can attach a peer keyed by machine_id.
    on_guest_attached: Callable[[str, "GuestSession"], None] | None = None
    on_guest_detached: Callable[[str], None] | None = None
    # Loopback config so the host can serve guest→host reverse RPCs by re-issuing
    # the request against its own web server (mirrors the guest-side pattern
    # in guest_client._handle_rpc). Injected by gateway after web app starts.
    local_web_port: int = 0
    local_web_token: str = ""
    _http_session: ClientSession | None = field(default=None, repr=False)
    _inbound_executor: InboundRequestExecutor | None = field(default=None, repr=False)

    def get(self, machine_id: str) -> GuestSession | None:
        return self.sessions.get(machine_id)

    def list_machines(self) -> list[dict]:
        """All known machines: connected + recently seen."""
        out: list[dict] = []
        seen: set[str] = set()
        now = time.time()
        for machine_id, session in self.sessions.items():
            seen.add(machine_id)
            out.append({
                "machine_id": machine_id,
                "online": True,
                "bots": [
                    {"name": bot.name, "display_name": bot.display_name,
                     "backend": bot.backend, "model": bot.model, "kind": bot.kind}
                    for bot in session.bots
                ],
                "last_seen": now,
            })
        for machine_id, info in self.history.items():
            if machine_id in seen:
                continue
            out.append({
                "machine_id": machine_id,
                "online": False,
                "bots": info.get("bots") or [],
                "last_seen": info.get("last_seen") or 0,
            })
        return out

    def list_bots(self) -> list[tuple[str, RemoteBot]]:
        """Yield (machine_id, RemoteBot) for every registered remote bot."""
        out: list[tuple[str, RemoteBot]] = []
        for machine_id, session in self.sessions.items():
            for bot in session.bots:
                out.append((machine_id, bot))
        return out

    def get_bot(self, machine_id: str, name: str) -> RemoteBot | None:
        """Return the RemoteBot named `name` on guest `machine_id`, or None."""
        session = self.sessions.get(machine_id)
        if session is None:
            return None
        for bot in session.bots:
            if bot.name == name:
                return bot
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
        for session in list(self.sessions.values()):
            session._closed = True
            try:
                await session.ws.close()
            except Exception:
                pass
        self.sessions.clear()

    async def _serve_inbound_rpc(self, session: GuestSession, request: dict) -> None:
        """Handle an `rpc` frame coming *from* a guest.

        Delegates to the shared :class:`InboundRequestExecutor`: re-issue the
        request against the host's own web port over loopback so the host's full
        `_handle_web_*` logic — which already includes host→guest proxying —
        handles routing (incl. onward guest→guest relay) for free.
        """
        if self._http_session is None:
            self._http_session = ClientSession()
        # Rebuild if the injected loopback config changed since last call
        # (gateway injects local_web_port after the web app starts).
        executor = self._inbound_executor
        if (executor is None
                or executor.local_web_port != self.local_web_port
                or executor._machine_id != session.machine_id):
            executor = InboundRequestExecutor(
                local_web_port=self.local_web_port,
                local_web_token=self.local_web_token,
                http_session_provider=lambda: self._http_session,
                logger_message="host: inbound rpc %s %s failed: %s",
                event_message_prefix="inbound rpc ",
                fail_category=Category.CLUSTER_HOST_RPC_FAIL,
                not_configured_error="host loopback not configured",
                machine_id=session.machine_id,
                require_web_port=True,
            )
            self._inbound_executor = executor
        await executor.serve(session.ws.send_json, request)

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Aiohttp handler for /api/guest/ws."""
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)

        # Expect hello
        session: GuestSession | None = None
        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(msg.data)
                except Exception:
                    logger.warning("guest ws: invalid JSON frame")
                    log.warning(Category.CLUSTER_PROTOCOL_ERROR, "guest ws: invalid JSON frame")
                    continue
                t = payload.get("type")

                if session is None:
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
                            name=str(bot.get("name") or ""),
                            display_name=str(bot.get("display_name") or ""),
                            backend=str(bot.get("backend") or ""),
                            model=str(bot.get("model") or ""),
                            kind=str(bot.get("kind") or "bot"),
                        )
                        for bot in bots_raw
                        if isinstance(bot, dict) and bot.get("name")
                    ]
                    session = GuestSession(machine_id=machine_id, ws=ws, bots=bots)
                    # If a previous session with this machine_id is still around,
                    # evict it (a guest reconnect).
                    old_session = self.sessions.get(machine_id)
                    if old_session is not None:
                        old_session._closed = True
                        try:
                            await old_session.ws.close()
                        except Exception:
                            pass
                    self.sessions[machine_id] = session
                    logger.info("guest '%s' connected with %d bot(s)", machine_id, len(bots))
                    log.info(
                        Category.CLUSTER_GUEST_JOINED,
                        f"guest '{machine_id}' joined with {len(bots)} bot(s)",
                        machine_id=machine_id, bot_count=len(bots),
                    )
                    await ws.send_json({"type": "welcome"})
                    if self.on_guest_attached is not None:
                        try:
                            self.on_guest_attached(machine_id, session)
                        except Exception as e:
                            logger.warning("on_guest_attached failed: %s", e)
                            log.warning(
                                Category.CLUSTER_PROTOCOL_ERROR,
                                "on_guest_attached failed",
                                machine_id=machine_id, error=repr(e),
                            )
                    if self.on_topology_change is not None:
                        try:
                            await self.on_topology_change(machine_id)
                        except Exception as e:
                            logger.warning("on_topology_change(hello) failed: %s", e)
                            log.warning(
                                Category.CLUSTER_PROTOCOL_ERROR,
                                "on_topology_change(hello) failed",
                                machine_id=machine_id, error=repr(e),
                            )
                    continue

                if payload.get("v", WIRE_VERSION) != WIRE_VERSION:
                    logger.warning("dropping frame from %s: unsupported wire version %r",
                                   session.machine_id, payload.get("v"))
                    continue

                if t == "ping":
                    await ws.send_json({"type": "pong"})
                elif t == "rpc":
                    # Guest → host reverse RPC: serve via localhost loopback so
                    # we reuse all of host's existing _handle_web_* logic
                    # (incl. host→guest proxy if the target is yet another guest).
                    asyncio.create_task(self._serve_inbound_rpc(session, payload))
                elif t == "rpc_resp":
                    session._resolve(
                        str(payload.get("id") or ""),
                        int(payload.get("status") or 0),
                        payload.get("body") or {},
                    )
                elif t == "bots_update":
                    # Guest re-announces its bot list (e.g. after dynamic create)
                    bots_raw = payload.get("bots") or []
                    session.bots = [
                        RemoteBot(
                            name=str(bot.get("name") or ""),
                            display_name=str(bot.get("display_name") or ""),
                            backend=str(bot.get("backend") or ""),
                            model=str(bot.get("model") or ""),
                            kind=str(bot.get("kind") or "bot"),
                        )
                        for bot in bots_raw
                        if isinstance(bot, dict) and bot.get("name")
                    ]
                    if self.on_topology_change is not None:
                        try:
                            await self.on_topology_change(session.machine_id)
                        except Exception as e:
                            logger.warning("on_topology_change(bots_update) failed: %s", e)
                            log.warning(
                                Category.CLUSTER_PROTOCOL_ERROR,
                                "on_topology_change(bots_update) failed",
                                machine_id=session.machine_id, error=repr(e),
                            )
                elif self.on_unknown_frame is not None:
                    try:
                        await self.on_unknown_frame(session.machine_id, payload)
                    except Exception as e:
                        logger.warning("on_unknown_frame(%s) failed: %s", t, e)
                        log.warning(
                            Category.CLUSTER_PROTOCOL_ERROR,
                            f"on_unknown_frame({t}) failed",
                            machine_id=session.machine_id, frame_type=str(t), error=repr(e),
                        )
        finally:
            if session is not None:
                # Fail any in-flight guest→host reverse RPCs so their callers
                # (web relays via dispatch_machine_request) fail fast instead of
                # hanging the full timeout on a dead session.
                session._channel.reject_all(RuntimeError("guest ws disconnected"))
            if session is not None and not session._closed:
                self.sessions.pop(session.machine_id, None)
                # Remember bots so the UI keeps showing the row as "offline"
                self.history[session.machine_id] = {
                    "bots": [
                        {"name": bot.name, "display_name": bot.display_name,
                         "backend": bot.backend, "model": bot.model, "kind": bot.kind}
                        for bot in session.bots
                    ],
                    "last_seen": time.time(),
                }
                logger.info("guest '%s' disconnected", session.machine_id)
                log.info(
                    Category.CLUSTER_GUEST_LEFT,
                    f"guest '{session.machine_id}' left",
                    machine_id=session.machine_id,
                )
                if self.on_guest_detached is not None:
                    try:
                        self.on_guest_detached(session.machine_id)
                    except Exception as e:
                        logger.warning("on_guest_detached failed: %s", e)
                        log.warning(
                            Category.CLUSTER_PROTOCOL_ERROR,
                            "on_guest_detached failed",
                            machine_id=session.machine_id, error=repr(e),
                        )
                if self.on_topology_change is not None:
                    try:
                        await self.on_topology_change(None)
                    except Exception as e:
                        logger.warning("on_topology_change(disconnect) failed: %s", e)
                        log.warning(
                            Category.CLUSTER_PROTOCOL_ERROR,
                            "on_topology_change(disconnect) failed",
                            machine_id=session.machine_id, error=repr(e),
                        )
        return ws
