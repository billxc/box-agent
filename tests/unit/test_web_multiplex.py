"""/api/multiplex WebSocket 的测试——一个 socket，多个带 tag 的 chat 流。

页级 multiplex 端点用单个持有多个总线订阅的 WebSocket 取代 N 条 per-chat SSE 流。
这些测试用真的 MessageBus + 直接把 handler 作为 task 跑在测试自己的事件循环里
（用一个模拟 Starlette WebSocket 的 fake socket 驱动 receive/send），端到端覆盖整个
subscribe → publish → 带 tag 的帧 → unsubscribe → cleanup 回路。

之所以不起真 Starlette TestClient：TestClient 在独立 portal 线程里跑 app，而
MessageBus 的订阅者队列（asyncio.Queue）是绑定事件循环的——跨线程 put_nowait
不安全。让 handler 与 bus.publish 共用同一个循环，才如实覆盖同步 fan-out 语义。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from boxagent.bus.core import MessageBus
from boxagent.transports.web.server import WebHttpServer


class _FakeWebSocket:
    """模拟 handler 用到的那部分 Starlette WebSocket 接口。

    ``receive_text`` 从一个内部队列取客户端帧；``send_json`` 把服务端推送收进
    ``self.sent``；``close`` 后 ``receive_text`` 抛 ``WebSocketDisconnect``，等价于
    客户端断开。鉴权字段（client/headers/query_params）也一并提供。
    """

    def __init__(self) -> None:
        self.client = SimpleNamespace(host="127.0.0.1")
        self.headers: dict = {}
        self.query_params: dict = {}
        self.accepted = False
        self.close_code: int | None = None
        self.sent: list[dict] = []
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._closed = asyncio.Event()

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        from starlette.websockets import WebSocketDisconnect

        get_frame = asyncio.ensure_future(self._inbox.get())
        wait_closed = asyncio.ensure_future(self._closed.wait())
        done, pending = await asyncio.wait(
            {get_frame, wait_closed}, return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if get_frame in done:
            return get_frame.result()
        raise WebSocketDisconnect(code=1000)

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.close_code = code
        self._closed.set()

    # 测试驱动侧
    def client_send_json(self, frame: dict) -> None:
        self._inbox.put_nowait(json.dumps(frame))

    def client_disconnect(self) -> None:
        self._closed.set()


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


async def _run_handler(server: WebHttpServer, ws: _FakeWebSocket) -> asyncio.Task:
    task = asyncio.create_task(server._handle_web_multiplex(ws))
    # 让 handler accept + 起 pump。
    for _ in range(20):
        await asyncio.sleep(0)
        if ws.accepted:
            break
    return task


async def _next_sent(ws: _FakeWebSocket, timeout: float = 2.0) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if ws.sent:
            return ws.sent.pop(0)
        await asyncio.sleep(0.01)
    raise asyncio.TimeoutError()


@pytest.mark.asyncio
async def test_multiplex_delivers_tagged_events_for_subscribed_chat():
    bus = MessageBus(machine_id="local")
    server = _make_server(bus)
    ws = _FakeWebSocket()
    task = await _run_handler(server, ws)
    try:
        ws.client_send_json({"type": "subscribe", "machine": "local", "bot": "b", "chat_id": "c1"})
        await asyncio.sleep(0.05)
        bus.publish("chat.local.b.c1", {"type": "message", "text": "hi"}, 1.0)
        frame = await _next_sent(ws)
        assert frame == {
            "machine": "local", "bot": "b", "chat_id": "c1",
            "event": {"type": "message", "text": "hi"},
        }
    finally:
        ws.client_disconnect()
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_multiplex_demuxes_two_chats_over_one_socket():
    bus = MessageBus(machine_id="local")
    server = _make_server(bus)
    ws = _FakeWebSocket()
    task = await _run_handler(server, ws)
    try:
        ws.client_send_json({"type": "subscribe", "machine": "local", "bot": "b", "chat_id": "c1"})
        ws.client_send_json({"type": "subscribe", "machine": "local", "bot": "b", "chat_id": "c2"})
        await asyncio.sleep(0.05)
        bus.publish("chat.local.b.c2", {"type": "typing"}, 1.0)
        bus.publish("chat.local.b.c1", {"type": "message", "text": "x"}, 2.0)
        got = [await _next_sent(ws), await _next_sent(ws)]
        by_chat = {f["chat_id"]: f["event"] for f in got}
        assert by_chat["c2"] == {"type": "typing"}
        assert by_chat["c1"] == {"type": "message", "text": "x"}
    finally:
        ws.client_disconnect()
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_multiplex_unsubscribe_stops_delivery():
    bus = MessageBus(machine_id="local")
    server = _make_server(bus)
    ws = _FakeWebSocket()
    task = await _run_handler(server, ws)
    try:
        ws.client_send_json({"type": "subscribe", "machine": "local", "bot": "b", "chat_id": "c1"})
        await asyncio.sleep(0.05)
        ws.client_send_json({"type": "unsubscribe", "machine": "local", "bot": "b", "chat_id": "c1"})
        await asyncio.sleep(0.05)
        # unsubscribe 后总线对该 topic 没有订阅者了。
        assert not bus.has_subscribers("chat.local.b.c1")
        bus.publish("chat.local.b.c1", {"type": "message"}, 1.0)
        with pytest.raises(asyncio.TimeoutError):
            await _next_sent(ws, timeout=0.3)
    finally:
        ws.client_disconnect()
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_multiplex_closes_all_subscriptions_on_disconnect():
    bus = MessageBus(machine_id="local")
    server = _make_server(bus)
    ws = _FakeWebSocket()
    task = await _run_handler(server, ws)
    ws.client_send_json({"type": "subscribe", "machine": "local", "bot": "b", "chat_id": "c1"})
    ws.client_send_json({"type": "subscribe", "machine": "local", "bot": "b", "chat_id": "c2"})
    await asyncio.sleep(0.05)
    assert bus.has_subscribers("chat.local.b.c1")
    assert bus.has_subscribers("chat.local.b.c2")
    ws.client_disconnect()
    await asyncio.wait_for(task, timeout=2.0)
    # socket 断开时两个订阅都被拆除。
    assert not bus.has_subscribers("chat.local.b.c1")
    assert not bus.has_subscribers("chat.local.b.c2")


@pytest.mark.asyncio
async def test_multiplex_ignores_unknown_local_bot():
    bus = MessageBus(machine_id="local")
    server = _make_server(bus, bots=("b",))
    ws = _FakeWebSocket()
    task = await _run_handler(server, ws)
    try:
        # "nope" 不是本机启用 web 的 bot → 不创建订阅。
        ws.client_send_json({"type": "subscribe", "machine": "local", "bot": "nope", "chat_id": "c1"})
        await asyncio.sleep(0.05)
        assert not bus.has_subscribers("chat.local.nope.c1")
    finally:
        ws.client_disconnect()
        await asyncio.wait_for(task, timeout=2.0)
