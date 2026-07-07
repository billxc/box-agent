"""Tests for the /api/multiplex WebSocket — one socket, many tagged chat streams.

The page-level multiplex endpoint replaces N per-chat SSE streams with a single
WebSocket that holds many bus subscriptions. These drive a real aiohttp server +
real ws client + real MessageBus, so they cover the whole subscribe → publish →
tagged-frame → unsubscribe → cleanup loop end to end.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from boxagent.bus.core import MessageBus
from boxagent.transports.web.server import WebHttpServer


def _make_server(bus: MessageBus, machine_id: str = "local", bots=("b",)) -> WebHttpServer:
    config = SimpleNamespace(
        web_token="", web_trust_header="", web_host="127.0.0.1", web_port=0, bots={},
    )
    topology = MagicMock()
    topology.local_machine_id.return_value = machine_id
    cluster_rpc = MagicMock()
    cluster_rpc.dispatch_machine_request = AsyncMock(return_value=None)
    return WebHttpServer(
        config=config,
        local_dir=Path("/tmp"),
        config_dir=Path("/tmp"),
        storage=None,
        web_channels={name: MagicMock() for name in bots},
        pools={},
        topology=topology,
        cluster_rpc=cluster_rpc,
        cluster_routes=None,
        message_bus=bus,
    )


async def _client(server: WebHttpServer) -> TestClient:
    app = web.Application()
    app.router.add_get("/api/multiplex", server._handle_web_multiplex)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_multiplex_delivers_tagged_events_for_subscribed_chat():
    bus = MessageBus(machine_id="local")
    server = _make_server(bus)
    client = await _client(server)
    try:
        ws = await client.ws_connect("/api/multiplex")
        await ws.send_json({"type": "subscribe", "machine": "local", "bot": "b", "chat_id": "c1"})
        # Let the handler register the subscription before we publish.
        await asyncio.sleep(0.05)
        bus.publish("chat.local.b.c1", {"type": "message", "text": "hi"}, 1.0)
        frame = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
        assert frame == {
            "machine": "local", "bot": "b", "chat_id": "c1",
            "event": {"type": "message", "text": "hi"},
        }
        await ws.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_multiplex_demuxes_two_chats_over_one_socket():
    bus = MessageBus(machine_id="local")
    server = _make_server(bus)
    client = await _client(server)
    try:
        ws = await client.ws_connect("/api/multiplex")
        await ws.send_json({"type": "subscribe", "machine": "local", "bot": "b", "chat_id": "c1"})
        await ws.send_json({"type": "subscribe", "machine": "local", "bot": "b", "chat_id": "c2"})
        await asyncio.sleep(0.05)
        bus.publish("chat.local.b.c2", {"type": "typing"}, 1.0)
        bus.publish("chat.local.b.c1", {"type": "message", "text": "x"}, 2.0)
        got = [await asyncio.wait_for(ws.receive_json(), timeout=2.0) for _ in range(2)]
        by_chat = {f["chat_id"]: f["event"] for f in got}
        assert by_chat["c2"] == {"type": "typing"}
        assert by_chat["c1"] == {"type": "message", "text": "x"}
        await ws.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_multiplex_unsubscribe_stops_delivery():
    bus = MessageBus(machine_id="local")
    server = _make_server(bus)
    client = await _client(server)
    try:
        ws = await client.ws_connect("/api/multiplex")
        await ws.send_json({"type": "subscribe", "machine": "local", "bot": "b", "chat_id": "c1"})
        await asyncio.sleep(0.05)
        await ws.send_json({"type": "unsubscribe", "machine": "local", "bot": "b", "chat_id": "c1"})
        await asyncio.sleep(0.05)
        # After unsubscribe the bus has no subscriber for this topic.
        assert not bus.has_subscribers("chat.local.b.c1")
        bus.publish("chat.local.b.c1", {"type": "message"}, 1.0)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws.receive_json(), timeout=0.3)
        await ws.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_multiplex_closes_all_subscriptions_on_disconnect():
    bus = MessageBus(machine_id="local")
    server = _make_server(bus)
    client = await _client(server)
    try:
        ws = await client.ws_connect("/api/multiplex")
        await ws.send_json({"type": "subscribe", "machine": "local", "bot": "b", "chat_id": "c1"})
        await ws.send_json({"type": "subscribe", "machine": "local", "bot": "b", "chat_id": "c2"})
        await asyncio.sleep(0.05)
        assert bus.has_subscribers("chat.local.b.c1")
        assert bus.has_subscribers("chat.local.b.c2")
        await ws.close()
        await asyncio.sleep(0.05)
        # Both subscriptions torn down when the socket dropped.
        assert not bus.has_subscribers("chat.local.b.c1")
        assert not bus.has_subscribers("chat.local.b.c2")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_multiplex_ignores_unknown_local_bot():
    bus = MessageBus(machine_id="local")
    server = _make_server(bus, bots=("b",))
    client = await _client(server)
    try:
        ws = await client.ws_connect("/api/multiplex")
        # "nope" is not a local web-enabled bot → no subscription created.
        await ws.send_json({"type": "subscribe", "machine": "local", "bot": "nope", "chat_id": "c1"})
        await asyncio.sleep(0.05)
        assert not bus.has_subscribers("chat.local.nope.c1")
        await ws.close()
    finally:
        await client.close()
