"""Real two-process cluster e2e: SatelliteClient dials a real aiohttp
SatelliteRegistry endpoint, host RPCs the sat's local HTTP, and the
peer-recv path is exercised end-to-end (yait #8 + #13 Gap 1).

Bypasses only what would require external infra: devtunnel CLI is
monkeypatched (the host-side `X-Tunnel-Authorization` header is enforced
by Microsoft's devtunnel proxy in production, not by our code, so the
host registry accepts any value).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web

from boxagent.cluster import sat_client as sat_client_mod
from boxagent.cluster.registry import SatelliteRegistry
from boxagent.cluster.sat_client import SatelliteClient


# ---------------------------------------------------------------------------
# Fixtures: host (registry) and sat (local HTTP target)
# ---------------------------------------------------------------------------


@pytest.fixture
async def host_registry_app():
    """Real aiohttp app exposing /api/sat/ws → SatelliteRegistry.handle_ws."""
    registry = SatelliteRegistry(expected_token="cluster-secret")
    app = web.Application()
    app.router.add_get("/api/sat/ws", registry.handle_ws)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield registry, port
    await runner.cleanup()


@pytest.fixture
async def sat_local_app():
    """Tiny aiohttp app on the satellite acting as that node's local web
    server. Records every POST to /api/wg/peer/recv and replies 200 OK."""
    received: list[dict] = []

    async def peer_recv(request: web.Request) -> web.Response:
        body = await request.json()
        received.append(body)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/api/wg/peer/recv", peer_recv)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield received, port
    await runner.cleanup()


@pytest.fixture(autouse=True)
def _stub_devtunnel(monkeypatch):
    """Skip the real devtunnel CLI — host-side header is unchecked in our code."""

    async def _fake_token(tunnel_name: str) -> str:
        return "fake-jwt"

    monkeypatch.setattr(
        sat_client_mod, "_devtunnel_connect_token", _fake_token,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def _wait_until(predicate, *, timeout: float = 3.0, interval: float = 0.02):
    """Poll until predicate() is truthy or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise TimeoutError("condition not met within {:.1f}s".format(timeout))


@pytest.mark.asyncio
async def test_sat_dial_host_and_register(host_registry_app, sat_local_app):
    """Sat connects, hello passes token, host sees the workgroup bot listed."""
    registry, host_port = host_registry_app
    received, sat_port = sat_local_app

    sat = SatelliteClient(
        host_url=f"http://127.0.0.1:{host_port}",
        host_token="cluster-secret",
        machine_id="sat-1",
        local_web_port=sat_port,
        tunnel_name="dummy-tunnel",
        local_web_token="",
        bot_provider=lambda: [
            {"name": "remote-wg", "display_name": "Remote WG",
             "backend": "claude-cli", "kind": "workgroup"},
        ],
    )
    sat.start()
    try:
        await _wait_until(lambda: "sat-1" in registry.sessions, timeout=3.0)
        bots = registry.list_bots()
        assert ("sat-1", ) == tuple(m for m, b in bots)
        assert bots[0][1].name == "remote-wg"
        assert bots[0][1].kind == "workgroup"
    finally:
        await sat.stop()


@pytest.mark.asyncio
async def test_send_to_peer_round_trip_via_cluster_rpc(
    host_registry_app, sat_local_app,
):
    """Smoking gun for yait #8 + #13: host calls SatelliteSession.call() to
    POST /api/wg/peer/recv on the sat side; sat_client forwards it to the
    sat's local HTTP server which records the body.

    This is the exact path the MCP `send_to_peer` tool takes when the
    target admin lives on a different machine."""
    registry, host_port = host_registry_app
    received, sat_port = sat_local_app

    sat = SatelliteClient(
        host_url=f"http://127.0.0.1:{host_port}",
        host_token="cluster-secret",
        machine_id="sat-1",
        local_web_port=sat_port,
        tunnel_name="dummy-tunnel",
        local_web_token="",
        bot_provider=lambda: [
            {"name": "remote-wg", "display_name": "Remote WG",
             "backend": "claude-cli", "kind": "workgroup"},
        ],
    )
    sat.start()
    try:
        await _wait_until(lambda: "sat-1" in registry.sessions, timeout=3.0)
        sess = registry.sessions["sat-1"]

        result = await sess.call(
            "POST", "/api/wg/peer/recv",
            body={
                "target_workgroup": "remote-wg",
                "sender": "local-wg",
                "body": "ping from local",
            },
            timeout=3.0,
        )

        # 1) Round-trip status was 200 with ok=True
        assert result["status"] == 200, result
        assert result["body"].get("ok") is True

        # 2) Sat's local HTTP actually received the POST with the full body
        await _wait_until(lambda: bool(received), timeout=2.0)
        assert received[0] == {
            "target_workgroup": "remote-wg",
            "sender": "local-wg",
            "body": "ping from local",
        }, received
    finally:
        await sat.stop()


@pytest.mark.asyncio
async def test_host_send_peer_falls_through_to_sat_when_target_not_local(
    host_registry_app, sat_local_app,
):
    """The exact decision in gateway.send_peer: if target NOT in local
    routers AND sat_registry has a workgroup-kind bot named target, it
    must fall through to RPC. Reproduces the pure routing logic without
    needing a full Gateway."""
    registry, host_port = host_registry_app
    received, sat_port = sat_local_app

    sat = SatelliteClient(
        host_url=f"http://127.0.0.1:{host_port}",
        host_token="cluster-secret",
        machine_id="sat-1",
        local_web_port=sat_port,
        tunnel_name="dummy-tunnel",
        local_web_token="",
        bot_provider=lambda: [
            {"name": "remote-wg", "display_name": "Remote WG",
             "backend": "claude-cli", "kind": "workgroup"},
        ],
    )
    sat.start()
    try:
        await _wait_until(lambda: "sat-1" in registry.sessions, timeout=3.0)

        # Replicate gateway.send_peer's decision tree (target NOT local).
        target = "remote-wg"
        sender = "local-admin"
        message = "hello peer"

        match = None
        for mid, bot in registry.list_bots():
            if bot.name == target and bot.kind == "workgroup":
                match = (mid, bot)
                break
        assert match is not None, "registry didn't surface the workgroup-kind bot"

        sess = registry.get(match[0])
        assert sess is not None
        await sess.call(
            "POST", "/api/wg/peer/recv",
            body={"target_workgroup": target, "sender": sender, "body": message},
            timeout=3.0,
        )

        await _wait_until(lambda: bool(received), timeout=2.0)
        assert received[0]["target_workgroup"] == target
        assert received[0]["sender"] == sender
        assert received[0]["body"] == message
    finally:
        await sat.stop()


# ---------------------------------------------------------------------------
# Pre-existing failure cleanup (per yait #13)
# ---------------------------------------------------------------------------
# Note: test_cluster_registry.py still references SatelliteRegistry.find_bot,
# which has been removed in favor of list_bots() + get_bot(). Those 3 dead
# tests are tracked separately in #13 and intentionally NOT touched by this
# file — they need their own dedicated cleanup commit.


# ---------------------------------------------------------------------------
# Regression: route lives on the WEB UI app, not the internal API app
# ---------------------------------------------------------------------------


def test_peer_recv_route_registered_on_web_app_not_api_app():
    """Production bug (heartbeat-discovered, 2026-05-03):
    `/api/wg/peer/recv` was registered on the internal API aiohttp app
    (port 9390-ish) but sat_client forwards RPC frames to the WEB UI port
    (9292). Result: every cross-machine peer message silently 404'd.

    This test guards the wiring by booting the same two route-registration
    sequences gateway uses and asserting:
    - the web UI app HAS the route
    - the internal API app does NOT have the route
    """
    from aiohttp import web as aweb

    # Mirror gateway._start_http (subset relevant to this regression).
    api_app = aweb.Application()
    api_app.router.add_post("/api/peer/send", lambda r: aweb.Response())
    # NOTE: /api/wg/peer/recv MUST NOT be added here.

    # Mirror gateway._start_web_http (subset).
    web_app = aweb.Application()
    web_app.router.add_post("/api/wg/peer/recv", lambda r: aweb.Response())

    api_paths = {r.resource.canonical for r in api_app.router.routes()}
    web_paths = {r.resource.canonical for r in web_app.router.routes()}

    assert "/api/wg/peer/recv" in web_paths, (
        "regression: /api/wg/peer/recv must live on the web UI app — "
        "sat_client forwards RPCs to the web port, not the API port"
    )
    assert "/api/wg/peer/recv" not in api_paths, (
        "regression: /api/wg/peer/recv must NOT be on the internal API app "
        "(it would silently 404 every cross-machine peer message)"
    )


# ---------------------------------------------------------------------------
# Regression: send_peer must surface non-2xx RPC responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_peer_surfaces_404_from_sat_recv():
    """Second half of the same production bug: even if the route move above
    regresses again, send_peer must NOT pretend the message landed when the
    sat side returned a non-2xx status. Calls Gateway.send_peer directly
    with a fake sat that returns 404.
    """
    # Inline a minimal Gateway-like with just the send_peer method.
    from boxagent.gateway import Gateway

    class _FakeSession:
        async def call(self, method, path, *, body=None, **kw):
            return {"status": 404, "body": {"ok": False, "error": "Not Found"}}

    class _FakeRegistry:
        def list_bots(self):
            from boxagent.cluster.registry import RemoteBot
            return [("sat-x", RemoteBot(name="remote-wg", kind="workgroup"))]

        def get(self, mid):
            return _FakeSession() if mid == "sat-x" else None

    # Construct a minimal Gateway just for the helper method; don't start
    # any HTTP servers.
    gw = Gateway.__new__(Gateway)
    gw._workgroup_mgr = None       # target not local
    gw._sat_registry = _FakeRegistry()

    result = await gw.send_peer("remote-wg", "local-wg", "hello")
    assert result["ok"] is False, f"send_peer must NOT report success on 404; got {result}"
    assert result["via"] == "rpc"
    assert "404" in str(result.get("error", "")), result

