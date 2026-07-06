"""Real-aiohttp N-node cluster harness for the RPC-round-trip invariants.

The event/chat harness (`_bus_harness.py`) is a fan-out shuttle — broadcast /
subscribe semantics. RPC is the OTHER delivery semantic: correlated
request/reply. Its load-bearing detail is the *loopback re-issue*: an inbound
RPC frame is replayed against the node's OWN web port and re-runs the REAL
`_handle_web_*` handler (`GuestRegistry._serve_inbound_rpc` on the host,
`GuestClient._handle_rpc` on the guest). A pure in-process fake would defeat the
whole point ("the loopback hits the REAL handler"), so this harness stands up a
genuine `aiohttp` server per node via `aiohttp.test_utils.TestServer` and links
the nodes' real `GuestRegistry`(host) / `GuestClient`(guest) over a real
WebSocket — exactly as production wires them, minus the devtunnel dial.

Topology: hub-and-spoke, one `host` + N guests. This mirrors the real cluster
(`registry.py` host, `guest_client.py` guest) and is enough for:
- single hop (guest→host, host→guest),
- two-hop (gA→host→gB),
- concurrency / correlation (many in-flight, out-of-order replies),
- timeout + pending-future cleanup.

Each node's app carries THREE real routes, each wrapped in a spy that records
(method, path, query, body) so a test can prove the REAL handler ran:
  GET  /api/echo?n=k        → {"machine": <id>, "n": k}
  GET  /api/history         → {"machine": <id>, "rows": [...controllable...]}
  GET  /api/session_info    → {"machine": <id>, "info": {...controllable...}}
Every handler is machine-aware: if `?machine=` names a DIFFERENT node, the
handler forwards to it (host: via GuestSession.call; this is what makes the
two-hop gA→host→gB round-trip re-enter the host's real routing on loopback).

Public surface used by the invariants:
- `await cluster.rpc(from_node, to_machine, method, path, query, body)` → dict
  ({"status": int, "body": dict}), the SAME shape GuestSession.call returns.
- `cluster.pending_rpc_count(node)` — in-flight correlated futures on that node.
- `cluster.hold_replies(node, predicate)` / `release()` — pause a node's outbound
  `rpc_resp` frames matching `predicate` (used to force out-of-order replies and
  overlap).
- `cluster.spy(node)` — the recorded (method, path, query, body) tuples.
- `cluster.set_history(node, rows)` / `set_session_info(node, info)` — control a
  node's real handler output.

This is TEST INFRASTRUCTURE, not product code.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from boxagent.cluster.guest_client import GuestClient
from boxagent.cluster.registry import GuestRegistry


# --------------------------------------------------------------------------
# Spy record for a single real-handler invocation.
# --------------------------------------------------------------------------
@dataclass
class SpyCall:
    method: str
    path: str
    query: dict
    body: dict | None


# --------------------------------------------------------------------------
# One node: a real aiohttp app (3 real routes + spy) behind a TestServer, plus
# either a host GuestRegistry or a guest GuestClient (assigned by the cluster).
# --------------------------------------------------------------------------
class _RpcNode:
    def __init__(self, machine_id: str) -> None:
        self.machine_id = machine_id
        self.spy_calls: list[SpyCall] = []
        # Controllable real-handler outputs.
        self.history_rows: list[dict] = [{"role": "user", "text": "hi"}]
        self.session_info: dict = {"session_id": "s1", "message_count": 1}
        # Slow-handler injection: path -> seconds to sleep before responding.
        self.handler_delays: dict[str, float] = {}

        # Host role: a real GuestRegistry (set for the host node).
        self.registry: GuestRegistry | None = None
        # Guest role: a real GuestClient (set for guest nodes).
        self.guest_client: GuestClient | None = None
        # Outbound-reply gate (for out-of-order / overlap tests).
        self._reply_gate: _ReplyGate | None = None

        self.app = self._build_app()
        self.server = TestServer(self.app)

    # -- app / routes --------------------------------------------------------

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/echo", self._handle_echo)
        app.router.add_get("/api/history", self._handle_history)
        app.router.add_get("/api/session_info", self._handle_session_info)
        app.router.add_post("/api/echo", self._handle_echo)
        return app

    async def _record_and_maybe_forward(
        self, request: web.Request, path: str,
    ) -> web.Response | None:
        """Record the spy call, apply any slow-handler delay, and — if the
        request names a DIFFERENT machine — forward it there (host role) so the
        two-hop path re-enters real routing. Returns a Response if forwarded /
        delayed-and-forwarded, else None to continue local handling."""
        body: dict | None = None
        if request.method != "GET":
            try:
                body = await request.json()
            except Exception:
                body = None
        self.spy_calls.append(
            SpyCall(request.method, path, dict(request.query), body),
        )
        delay = self.handler_delays.get(path)
        if delay:
            await asyncio.sleep(delay)

        target = request.query.get("machine")
        if target and target != self.machine_id:
            # Host forwards to the owning guest and returns its body verbatim.
            forwarded = await self._forward_to_machine(
                target, request.method, path, dict(request.query), body,
            )
            return web.json_response(
                forwarded.get("body") or {},
                status=int(forwarded.get("status") or 200),
            )
        return None

    async def _forward_to_machine(
        self, machine: str, method: str, path: str, query: dict,
        body: dict | None,
    ) -> dict:
        if self.registry is None:
            return {"status": 503, "body": {"ok": False, "error": "not host"}}
        session = self.registry.get(machine)
        if session is None:
            return {"status": 404, "body": {"ok": False, "error": "unknown machine"}}
        return await session.call(method, path, query=query, body=body)

    async def _handle_echo(self, request: web.Request) -> web.Response:
        forwarded = await self._record_and_maybe_forward(request, "/api/echo")
        if forwarded is not None:
            return forwarded
        return web.json_response(
            {"machine": self.machine_id, "n": request.query.get("n")},
        )

    async def _handle_history(self, request: web.Request) -> web.Response:
        forwarded = await self._record_and_maybe_forward(request, "/api/history")
        if forwarded is not None:
            return forwarded
        return web.json_response(
            {"machine": self.machine_id, "rows": self.history_rows},
        )

    async def _handle_session_info(self, request: web.Request) -> web.Response:
        forwarded = await self._record_and_maybe_forward(
            request, "/api/session_info",
        )
        if forwarded is not None:
            return forwarded
        return web.json_response(
            {"machine": self.machine_id, "info": self.session_info},
        )

    @property
    def port(self) -> int:
        return self.server.port

    async def start(self) -> None:
        await self.server.start_server()


# --------------------------------------------------------------------------
# Outbound rpc_resp gate: lets a test hold replies matching a predicate so it
# can force out-of-order delivery / prove overlap, then release them.
# --------------------------------------------------------------------------
class _ReplyGate:
    def __init__(self) -> None:
        self._predicate: Callable[[dict], bool] | None = None
        self._held: list[tuple[asyncio.Future, dict]] = []

    def hold(self, predicate: Callable[[dict], bool]) -> None:
        self._predicate = predicate

    async def gate(self, frame: dict) -> None:
        """Called before an rpc_resp frame is actually sent. If the gate is
        armed and matches, block until released."""
        if self._predicate is not None and self._predicate(frame):
            waiter: asyncio.Future = asyncio.get_event_loop().create_future()
            self._held.append((waiter, frame))
            await waiter

    def release(self, *, reverse: bool = False) -> None:
        self._predicate = None
        held, self._held = self._held, []
        if reverse:
            held = list(reversed(held))
        for waiter, _frame in held:
            if not waiter.done():
                waiter.set_result(None)

    @property
    def held_count(self) -> int:
        return len(self._held)


# --------------------------------------------------------------------------
# The cluster: one host + N guests, linked over real WebSockets.
# --------------------------------------------------------------------------
class RpcCluster:
    HELLO_TIMEOUT = 5.0

    def __init__(self) -> None:
        self.nodes: dict[str, _RpcNode] = {}
        self._host_id: str = ""
        self._guest_serve_tasks: list[asyncio.Task] = []
        self._guest_sessions: list[aiohttp.ClientSession] = []
        self._reply_gates: dict[str, _ReplyGate] = {}

    # -- construction --------------------------------------------------------

    async def add_host(self, machine_id: str, *, token: str = "tok") -> _RpcNode:
        node = _RpcNode(machine_id)
        node.registry = GuestRegistry(expected_token=token)
        # Guest-ws route on the host app so guests can dial it — MUST be added
        # before the TestServer starts (aiohttp freezes the router on start).
        node.app.router.add_get("/api/guest/ws", node.registry.handle_ws)
        await node.start()
        # Loopback: inbound guest→host RPC re-issues against the host's own web
        # port and re-runs the REAL /api/* handlers (incl. host→guest forward).
        node.registry.local_web_port = node.port
        self.nodes[machine_id] = node
        self._host_id = machine_id
        return node

    async def add_guest(
        self, machine_id: str, *, host: _RpcNode, token: str = "tok",
        bots: list[dict] | None = None,
    ) -> _RpcNode:
        node = _RpcNode(machine_id)
        await node.start()
        client = GuestClient(
            host_url=f"http://127.0.0.1:{host.port}",
            host_token=token,
            machine_id=machine_id,
            local_web_port=node.port,
            local_web_token="",
            bot_provider=lambda: list(bots or []),
        )
        node.guest_client = client
        await self._dial_guest(node, host)
        self.nodes[machine_id] = node
        return node

    async def _dial_guest(self, node: _RpcNode, host: _RpcNode) -> None:
        """Open a REAL client WS to the host, send hello, and drive the guest's
        real `_serve` loop (which uses the real `_handle_rpc` loopback). This is
        production's connection minus the devtunnel token dance."""
        client = node.guest_client
        assert client is not None
        session = aiohttp.ClientSession()
        self._guest_sessions.append(session)
        client._session = session
        ws_url = f"ws://127.0.0.1:{host.port}/api/guest/ws"
        ws = await session.ws_connect(ws_url, heartbeat=30.0, autoping=True)
        # Gate the guest's outbound rpc_resp frames too (out-of-order tests).
        self._install_guest_reply_gate(node, ws)
        client._ws = ws
        await ws.send_json({
            "type": "hello",
            "machine_id": client.machine_id,
            "token": client.host_token,
            "bots": client.bot_provider(),
        })
        # Wait for the host to register this guest (welcome + registry entry).
        await self._wait_registered(host, node.machine_id)
        # Drive the guest's REAL serve loop in the background.
        task = asyncio.create_task(client._serve(ws), name=f"guest-serve-{node.machine_id}")
        self._guest_serve_tasks.append(task)

    async def _wait_registered(self, host: _RpcNode, machine_id: str,
                               *, timeout: float = None) -> None:
        timeout = timeout or self.HELLO_TIMEOUT
        deadline = asyncio.get_event_loop().time() + timeout
        assert host.registry is not None
        while asyncio.get_event_loop().time() < deadline:
            if host.registry.get(machine_id) is not None:
                return
            await asyncio.sleep(0.01)
        raise TimeoutError(f"guest {machine_id} never registered on host")

    # -- reply gate: wrap the guest WS's send_json to intercept rpc_resp ------
    #
    # A guest's replies (to host→guest RPCs) go out over its client WS. Wrapping
    # that WS's send_json lets a test hold / release / reorder those replies —
    # which is exactly the seam R4 (out-of-order) and R5 (held-forever) need.
    # (Host-side replies to guest→host RPCs travel over per-connection server WS
    # objects created inside handle_ws; no test needs to gate those, so we don't
    # wrap them.)

    def _install_guest_reply_gate(self, node: _RpcNode, ws) -> None:
        gate = node._reply_gate or _ReplyGate()
        node._reply_gate = gate
        self._reply_gates[node.machine_id] = gate
        original_send_json = ws.send_json

        async def gated_send_json(frame, *args, **kwargs):
            if isinstance(frame, dict) and frame.get("type") == "rpc_resp":
                await gate.gate(frame)
            return await original_send_json(frame, *args, **kwargs)

        ws.send_json = gated_send_json  # type: ignore[assignment]

    def hold_replies(self, node: _RpcNode,
                     predicate: Callable[[dict], bool]) -> None:
        gate = self._reply_gates.get(node.machine_id)
        if gate is not None:
            gate.hold(predicate)

    def release_replies(self, node: _RpcNode, *, reverse: bool = False) -> None:
        gate = self._reply_gates.get(node.machine_id)
        if gate is not None:
            gate.release(reverse=reverse)

    def held_reply_count(self, node: _RpcNode) -> int:
        gate = self._reply_gates.get(node.machine_id)
        return gate.held_count if gate is not None else 0

    # -- the RPC entry point -------------------------------------------------

    async def rpc(
        self, from_node: _RpcNode, to_machine: str, method: str, path: str,
        *, query: dict | None = None, body: dict | None = None,
        timeout: float = 5.0,
    ) -> dict:
        """Issue an RPC from `from_node` to `to_machine`. Returns
        {"status": int, "body": dict} — the GuestSession.call shape.

        Routing (production-faithful):
        - host → guest:  host's GuestSession(to_machine).call(...)
        - guest → host:  the guest's GuestClient.call(...) (reverse RPC)
        - guest → guest: the guest calls the host with ?machine=to_machine; the
          host's loopback handler forwards to the owning guest (two-hop).
        """
        query = dict(query or {})
        if from_node.registry is not None:
            # from host: direct host→guest.
            session = from_node.registry.get(to_machine)
            if session is None:
                raise RuntimeError(f"host has no guest {to_machine!r}")
            return await session.call(method, path, query=query, body=body,
                                      timeout=timeout)
        # from a guest.
        client = from_node.guest_client
        assert client is not None
        if to_machine == self._host_id:
            return await client.call(method, path, query=query, body=body,
                                     timeout=timeout)
        # guest → guest: tag the target machine; host loopback forwards it.
        query = {**query, "machine": to_machine}
        return await client.call(method, path, query=query, body=body,
                                 timeout=timeout)

    # -- observation ---------------------------------------------------------

    def pending_rpc_count(self, node: _RpcNode) -> int:
        """Number of in-flight correlated RPC futures originated by `node`."""
        if node.registry is not None:
            total = 0
            for session in node.registry.sessions.values():
                total += len(session._pending)
            return total
        client = node.guest_client
        assert client is not None
        return len(client._pending)

    def spy(self, node: _RpcNode) -> list[SpyCall]:
        return node.spy_calls

    def set_history(self, node: _RpcNode, rows: list[dict]) -> None:
        node.history_rows = rows

    def set_session_info(self, node: _RpcNode, info: dict) -> None:
        node.session_info = info

    def set_handler_delay(self, node: _RpcNode, path: str, seconds: float) -> None:
        node.handler_delays[path] = seconds

    # -- teardown ------------------------------------------------------------

    async def aclose(self) -> None:
        for task in self._guest_serve_tasks:
            task.cancel()
        for task in self._guest_serve_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for node in self.nodes.values():
            if node.guest_client is not None:
                ws = node.guest_client._ws
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass
            if node.registry is not None:
                try:
                    await node.registry.aclose()
                except Exception:
                    pass
        for session in self._guest_sessions:
            try:
                await session.close()
            except Exception:
                pass
        for node in self.nodes.values():
            try:
                await node.server.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Convenience builders.
# --------------------------------------------------------------------------
async def build_two_node(token: str = "tok") -> RpcCluster:
    """host + one guest gB."""
    cluster = RpcCluster()
    host = await cluster.add_host("host", token=token)
    await cluster.add_guest("gB", host=host, token=token)
    return cluster


async def build_three_node(token: str = "tok") -> RpcCluster:
    """host + two guests gA, gB (for two-hop gA→host→gB)."""
    cluster = RpcCluster()
    host = await cluster.add_host("host", token=token)
    await cluster.add_guest("gA", host=host, token=token)
    await cluster.add_guest("gB", host=host, token=token)
    return cluster
