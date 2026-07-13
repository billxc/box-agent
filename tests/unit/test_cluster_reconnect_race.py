"""重连竞态回归：旧协程 finally 不得删掉新连接的 ClusterBus link。

复现 registry.py handle_ws 的 bug——guest 重连时新旧两个 handle_ws 协程短暂
并存。旧协程被顶掉（session._closed=True）后，它的 finally 曾无条件
detach_link，把新连接刚 attach 的 link 删掉。结果 host 拓扑仍显示该 guest
在线（sessions 里有它），但 ClusterBus 没有可用 link → 所有点对点 packet（含
RPC 应答）被判 unreachable 丢弃 → 该 guest 发起的所有跨机请求全部超时。
"""

import asyncio
import json

from starlette.websockets import WebSocketDisconnect

from boxagent.cluster.cluster_bus import ClusterBus, WIRE_VERSION
from boxagent.cluster.registry import GuestRegistry


class _FakeWebSocket:
    """驱动 handle_ws 的最小 Starlette WebSocket 替身。

    receive_text 先吐脚本化的帧；帧吐完后阻塞，直到 close() 被调用才抛
    WebSocketDisconnect——以此让"旧连接被 host 主动关闭"确定性地触发旧协程
    的 finally 清理路径。"""

    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)
        self.sent: list[dict] = []
        self.closed = False
        self._disconnected = asyncio.Event()

    async def accept(self) -> None:
        pass

    async def receive_text(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        await self._disconnected.wait()
        raise WebSocketDisconnect(code=1006)

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self._disconnected.set()


def _hello(machine_id: str) -> str:
    return json.dumps({
        "type": "hello",
        "machine_id": machine_id,
        "token": "",
        "bots": [{"name": "bot1"}],
        "v": WIRE_VERSION,
    })


class TestReconnectRace:
    async def test_stale_coroutine_finally_keeps_new_link(self):
        bus = ClusterBus(machine_id="host", route=lambda machine: machine)
        registry = GuestRegistry(cluster_bus=bus, local_machine_id="host")

        # 旧连接：吐一个 hello 后阻塞在 receive_text。
        old_ws = _FakeWebSocket([_hello("pc")])
        old_task = asyncio.create_task(registry.handle_ws(old_ws))
        await asyncio.sleep(0.02)
        assert "pc" in bus.link_keys()  # 旧连接的 link 已就位

        # 新连接：处理 hello 时逐出旧 session（close 旧 ws → 触发旧协程 finally），
        # 并 attach 自己的 link。
        new_ws = _FakeWebSocket([_hello("pc")])
        new_task = asyncio.create_task(registry.handle_ws(new_ws))

        # 等被顶掉的旧协程彻底跑完 finally。
        await asyncio.wait_for(old_task, timeout=1.0)

        # 核心断言：旧协程的 finally 不得删掉新连接刚 attach 的 link。
        assert "pc" in bus.link_keys(), \
            "旧协程 finally 误删了新连接的 ClusterBus link（重连竞态幽灵态）"
        # 拓扑侧始终是新连接——两者必须一致，不能"在线但不可达"。
        assert registry.sessions["pc"].ws is new_ws

        new_task.cancel()
        try:
            await new_task
        except (asyncio.CancelledError, Exception):
            pass

    async def test_clean_disconnect_still_detaches_link(self):
        # 反向保证：没有被顶掉的正常断连，仍要 detach_link + 清 session。
        bus = ClusterBus(machine_id="host", route=lambda machine: machine)
        registry = GuestRegistry(cluster_bus=bus, local_machine_id="host")

        ws = _FakeWebSocket([_hello("pc")])
        task = asyncio.create_task(registry.handle_ws(ws))
        await asyncio.sleep(0.02)
        assert "pc" in bus.link_keys()

        await ws.close()  # guest 正常断开
        await asyncio.wait_for(task, timeout=1.0)

        assert "pc" not in bus.link_keys()
        assert "pc" not in registry.sessions
