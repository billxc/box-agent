"""Wire-in test: cluster modules emit the expected `cluster.*` events
through the `boxagent.log` facade.

We don't simulate the full WS / devtunnel stack — we just exercise the
public methods that touch each log site with stubs/mocks and assert the
RecordingSink picked up the right category.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from boxagent.log import Category, log


class RecordingSink:
    def __init__(self):
        self.calls: list[tuple] = []

    def publish(self, level, category, message, **meta):
        self.calls.append((level, category, message, meta))

    def categories(self) -> list[str]:
        return [call[1] for call in self.calls]


@pytest.fixture
def sink():
    s = RecordingSink()
    log.bind(s)
    yield s
    log.unbind()


# ── tunnel.py ──

def test_tunnel_stop_emits_tunnel_down(sink):
    from boxagent.cluster.tunnel import ClusterTunnel
    tunnel = ClusterTunnel(name="test-tunnel", port=9999)
    asyncio.run(tunnel.stop())
    assert Category.CLUSTER_TUNNEL_DOWN in sink.categories()


# ── topology_service.py ──

def test_topology_push_failure_emits_topology_push_fail(sink):
    from boxagent.cluster.topology_service import TopologyService

    config = SimpleNamespace(
        machine_id="m1", node_id="m1", host_priority=["m1"],
        cluster_tunnel="t", web_port=9292, my_host_index=0,
    )
    service = TopologyService(config=config, web_channels={})

    failing_ws = MagicMock()
    failing_ws.send_json = AsyncMock(side_effect=RuntimeError("ws down"))
    session = SimpleNamespace(ws=failing_ws, machine_id="g1", bots=[])

    registry = SimpleNamespace(
        sessions={"g1": session},
        list_machines=lambda: [{"machine_id": "g1", "online": True, "bots": []}],
    )
    service.host_election = SimpleNamespace(registry=registry, client=None, state="host")

    asyncio.run(service.push_machines_snapshot_to_sats(None))
    cats = sink.categories()
    assert Category.CLUSTER_TOPOLOGY_PUSH_FAIL in cats


# ── registry.py: invalid JSON frame path ──

def test_registry_invalid_json_emits_protocol_error(sink):
    from boxagent.cluster.registry import GuestRegistry

    registry = GuestRegistry(expected_token="")

    # Build a fake aiohttp-style ws that yields one bad frame, then closes.
    bad_msg = SimpleNamespace(type=__import__("aiohttp").web.WSMsgType.TEXT, data="not-json{")

    class FakeWs:
        def __init__(self):
            self._sent = False

        async def prepare(self, _request):
            return None

        async def close(self, **kwargs):
            return None

        async def send_json(self, _data):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._sent:
                self._sent = True
                return bad_msg
            raise StopAsyncIteration

    fake_ws = FakeWs()

    # Patch web.WebSocketResponse to return our fake.
    import boxagent.cluster.registry as registry_mod
    original_ws = registry_mod.web.WebSocketResponse
    registry_mod.web.WebSocketResponse = lambda **kwargs: fake_ws
    try:
        request = SimpleNamespace()
        asyncio.run(registry.handle_ws(request))
    finally:
        registry_mod.web.WebSocketResponse = original_ws

    assert Category.CLUSTER_PROTOCOL_ERROR in sink.categories()
