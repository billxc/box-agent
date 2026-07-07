"""Tests for cross-machine fast-fail on incompatible / unreachable peers.

The病: mixed-version clusters made a request to an old / offline / incompatible
machine hang the full request timeout (~30s). The web UI fires several such
cross-machine requests per bot, each hanging a browser HTTP/1.1 connection slot
(~6), so the UI froze even though the backend was fine.

The fix negotiates a cluster-bus wire version at the hello/welcome handshake,
propagates it through machines_snapshot, and lets `dispatch_machine_request`
fast-fail (502 in <1ms) to any peer whose version isn't ours — never sending a
doomed request that would just time out.

These tests lock:
  1. hello with a `v` records the negotiated version on the GuestSession and the
     bus link; a hello WITHOUT `v` records 0 (old/incompatible).
  2. machines_snapshot descriptors carry each machine's version.
  3. dispatch_machine_request to an incompatible peer returns 502 without ever
     calling `request()` (no doomed send).
  4. a machine that "updated and reconnected" refreshes to the new version — it
     is no longer treated as the old one.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.web import WSMsgType

from boxagent.cluster.cluster_bus import WIRE_VERSION as CLUSTER_BUS_WIRE_VERSION
from boxagent.cluster.registry import GuestRegistry, GuestSession, RemoteBot
from boxagent.cluster.request_reply import RequestReply
from boxagent.cluster.topology_service import TopologyService


# ── a WebSocketResponse stub that replays a scripted hello, then ends ──────────


class _ScriptedServerWS:
    """Stand-in for the host-side WebSocketResponse `handle_ws` constructs.

    Replays the given inbound frames (as if a guest sent them), records every
    outbound send_json, then the async iteration ends so `handle_ws` returns."""

    def __init__(self, inbound_frames: list[dict]) -> None:
        self._inbound = [json.dumps(frame) for frame in inbound_frames]
        self.sent: list[dict] = []
        self.closed = False
        self.close_code = 0

    async def prepare(self, request):
        return None

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for raw in self._inbound:
            yield SimpleNamespace(type=WSMsgType.TEXT, data=raw)

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self, code=1000, message=b""):
        self.closed = True
        self.close_code = code


async def _drive_hello(registry: GuestRegistry, hello: dict) -> tuple[_ScriptedServerWS, GuestSession]:
    """Run handle_ws against a single scripted hello and return (ws, session).

    handle_ws pops the session on the (immediate) disconnect its scripted stream
    triggers, so we capture the live session via on_guest_attached — which fires
    at hello time with the negotiated version already set — rather than reading
    it back out of registry.sessions afterwards."""
    captured: dict[str, GuestSession] = {}
    prior_hook = registry.on_guest_attached

    def _capture(machine_id, session):
        captured["session"] = session
        if prior_hook is not None:
            prior_hook(machine_id, session)

    registry.on_guest_attached = _capture
    server_ws = _ScriptedServerWS([hello])
    try:
        with patch("boxagent.cluster.registry.web.WebSocketResponse", return_value=server_ws):
            await registry.handle_ws(request=SimpleNamespace())
    finally:
        registry.on_guest_attached = prior_hook
    return server_ws, captured["session"]


class _RecordingBus:
    """ClusterBus stand-in. `links` is the live link map (detach removes an
    entry); `attached_version` remembers the last version each link_key was
    attached with, so an assertion survives the disconnect-time detach that
    handle_ws performs when its scripted stream ends."""

    def __init__(self) -> None:
        self.links: dict[str, int] = {}
        self.attached_version: dict[str, int] = {}

    def attach_link(self, link_key, send_frame, *, version=CLUSTER_BUS_WIRE_VERSION):
        self.links[link_key] = version
        self.attached_version[link_key] = version

    def detach_link(self, link_key):
        self.links.pop(link_key, None)


# ── 1. hello version negotiation ──────────────────────────────────────────────


class TestHelloVersion:
    async def test_hello_with_version_records_it(self):
        bus = _RecordingBus()
        registry = GuestRegistry(cluster_bus=bus)
        ws, session = await _drive_hello(registry, {
            "type": "hello", "v": CLUSTER_BUS_WIRE_VERSION,
            "machine_id": "devbox", "bots": [],
        })
        # welcome carries our version
        welcome = next(frame for frame in ws.sent if frame.get("type") == "welcome")
        assert welcome["v"] == CLUSTER_BUS_WIRE_VERSION
        # session + bus link recorded the negotiated version
        assert session.version == CLUSTER_BUS_WIRE_VERSION
        assert bus.attached_version["devbox"] == CLUSTER_BUS_WIRE_VERSION

    async def test_hello_without_version_records_zero(self):
        # An old peer's hello carries no `v` → recorded as 0 (incompatible), so
        # requests to it fast-fail instead of hanging.
        bus = _RecordingBus()
        registry = GuestRegistry(cluster_bus=bus)
        _ws, session = await _drive_hello(registry, {
            "type": "hello", "machine_id": "oldbox", "bots": [],
        })
        assert session.version == 0
        assert bus.attached_version["oldbox"] == 0


# ── 2. snapshot carries version ───────────────────────────────────────────────


class TestSnapshotVersion:
    def _topology(self, sessions: dict[str, int]) -> TopologyService:
        config = SimpleNamespace(
            machine_id="host-machine", node_id="", cluster_tunnel=True,
            my_host_index=0, host_priority=["host-machine"],
        )
        registry = GuestRegistry()
        for machine_id, version in sessions.items():
            registry.sessions[machine_id] = GuestSession(
                machine_id=machine_id, ws=None, bots=[RemoteBot(name="b")], version=version,
            )
        host_election = SimpleNamespace(registry=registry, client=None, state="host")
        topology = TopologyService(config=config, web_channels={})
        topology.set_host_election(host_election)
        return topology

    def test_collect_machines_stamps_version(self):
        topology = self._topology({"devbox": CLUSTER_BUS_WIRE_VERSION, "oldbox": 0})
        machines = {m["machine_id"]: m for m in topology.collect_machines()}
        # host stamps itself as current
        assert machines["host-machine"]["version"] == CLUSTER_BUS_WIRE_VERSION
        assert machines["host-machine"]["self"] is True
        # guests carry their negotiated version
        assert machines["devbox"]["version"] == CLUSTER_BUS_WIRE_VERSION
        assert machines["oldbox"]["version"] == 0

    def test_version_for_reads_guest_session(self):
        topology = self._topology({"devbox": CLUSTER_BUS_WIRE_VERSION, "oldbox": 0})
        assert topology.version_for("host-machine") == CLUSTER_BUS_WIRE_VERSION  # self
        assert topology.version_for("devbox") == CLUSTER_BUS_WIRE_VERSION
        assert topology.version_for("oldbox") == 0
        assert topology.version_for("ghost") == 0  # unknown machine

    def test_version_for_reads_guest_client_cache(self):
        # A guest reads peer versions from the host-pushed snapshot cache.
        config = SimpleNamespace(machine_id="guest-machine", node_id="", cluster_tunnel=True)
        client = SimpleNamespace(remote_machines=[
            {"machine_id": "host-machine", "version": CLUSTER_BUS_WIRE_VERSION},
            {"machine_id": "oldbox", "version": 0},
            {"machine_id": "noversion"},  # snapshot from an old host → treated as 0
        ])
        host_election = SimpleNamespace(registry=None, client=client, state="guest")
        topology = TopologyService(config=config, web_channels={})
        topology.set_host_election(host_election)
        assert topology.version_for("host-machine") == CLUSTER_BUS_WIRE_VERSION
        assert topology.version_for("oldbox") == 0
        assert topology.version_for("noversion") == 0


# ── 3. dispatch fast-fails incompatible peers ─────────────────────────────────


class _CountingRequestReply(RequestReply):
    """RequestReply that fails loudly if `request()` is ever called — proving the
    version pre-check short-circuits before any doomed send."""

    def __init__(self, *, topology):
        # Minimal init: we only exercise dispatch_machine_request, which needs
        # topology + a bus for the base subscribe calls.
        bus = SimpleNamespace(
            subscribe=lambda *a, **k: SimpleNamespace(close=lambda: None),
            send=lambda **k: "mid",
        )
        super().__init__(bus=bus, topology=topology, local_web_port=0)
        self.request_calls = 0

    async def request(self, *args, **kwargs):  # type: ignore[override]
        self.request_calls += 1
        raise AssertionError("request() should not be called for an incompatible peer")


def _topology_with_versions(local: str, versions: dict[str, int]):
    return SimpleNamespace(
        local_machine_id=lambda: local,
        guest_registry=None,
        version_for=lambda machine: versions.get(machine, 0),
    )


class TestDispatchFastFail:
    async def test_incompatible_peer_returns_502_without_request(self):
        topology = _topology_with_versions("mbp", {"oldbox": 0})
        rr = _CountingRequestReply(topology=topology)
        request = SimpleNamespace(query={})
        response = await rr.dispatch_machine_request("oldbox", "GET", "/api/history", request)
        assert response is not None
        assert response.status == 502
        assert rr.request_calls == 0  # never sent the doomed request

    async def test_local_target_returns_none(self):
        topology = _topology_with_versions("mbp", {})
        rr = _CountingRequestReply(topology=topology)
        request = SimpleNamespace(query={})
        # local → None so the caller handles it locally; version_for not consulted
        assert await rr.dispatch_machine_request("mbp", "GET", "/x", request) is None

    async def test_compatible_peer_proceeds_to_request(self):
        # A same-version peer must NOT be fast-failed — dispatch calls request().
        topology = _topology_with_versions("mbp", {"devbox": CLUSTER_BUS_WIRE_VERSION})
        bus = SimpleNamespace(
            subscribe=lambda *a, **k: SimpleNamespace(close=lambda: None),
            send=lambda **k: "mid",
        )
        rr = RequestReply(bus=bus, topology=topology, local_web_port=0)
        called = {}

        async def fake_request(machine, method, path, *, query=None, body=None):
            called["machine"] = machine
            return {"status": 200, "body": {"ok": True}}

        rr.request = fake_request  # type: ignore[assignment]
        request = SimpleNamespace(query={})
        response = await rr.dispatch_machine_request("devbox", "GET", "/api/history", request)
        assert called["machine"] == "devbox"
        assert response.status == 200


# ── 4. updated-and-reconnected machine refreshes to the new version ───────────


class TestReconnectRefreshesVersion:
    async def test_reconnect_upgrades_version(self):
        # A machine first connects as an old peer (no `v`, recorded 0), then
        # updates and reconnects with the new version — topology must report the
        # new version, so it stops being fast-failed.
        bus = _RecordingBus()
        registry = GuestRegistry(cluster_bus=bus)

        config = SimpleNamespace(
            machine_id="host-machine", node_id="", cluster_tunnel=True,
            my_host_index=0, host_priority=["host-machine"],
        )
        host_election = SimpleNamespace(registry=registry, client=None, state="host")
        topology = TopologyService(config=config, web_channels={})
        topology.set_host_election(host_election)

        # First connection: old code, no version. handle_ws pops the session on
        # its scripted disconnect, so re-seat the captured session to model a
        # still-open link, then query topology.
        _ws, old_session = await _drive_hello(registry, {
            "type": "hello", "machine_id": "devbox", "bots": [],
        })
        assert old_session.version == 0
        registry.sessions["devbox"] = old_session
        assert topology.version_for("devbox") == 0  # incompatible → would fast-fail

        # devbox updates and reconnects with the current version.
        _ws2, new_session = await _drive_hello(registry, {
            "type": "hello", "v": CLUSTER_BUS_WIRE_VERSION,
            "machine_id": "devbox", "bots": [],
        })
        assert new_session.version == CLUSTER_BUS_WIRE_VERSION
        registry.sessions["devbox"] = new_session
        assert topology.version_for("devbox") == CLUSTER_BUS_WIRE_VERSION  # refreshed
        assert bus.attached_version["devbox"] == CLUSTER_BUS_WIRE_VERSION
