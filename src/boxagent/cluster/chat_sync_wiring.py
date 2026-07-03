"""把 cluster registry / guest_client 桥接进 ChatSyncer 的 hook。

类似 ``events/sync_wiring.py``，但**链式**接上既有 hook：EventSyncer 已经占了
``on_guest_attached`` / ``on_guest_detached`` / ``on_unknown_frame``，所以这些
installer 会捕获当前 callback 并 fall through 过去 —— 因此 chat hook 必须在
event hook **之后**安装（event 那套是直接赋值、不链式的）。gateway 保证这个顺序。
同步的 attach/detach callback 经 ``create_task`` 桥到 ChatSyncer 的 async 方法
（它们跑在 WS coroutine 里，一定有 running loop）。
"""
from __future__ import annotations

import asyncio

from .chat_sync import ChatSyncer


def install_registry_hooks(syncer: ChatSyncer, registry) -> None:
    """host 侧 GuestRegistry 接入 chat syncer（peer key = machine_id）。"""
    previous_attached = registry.on_guest_attached
    previous_detached = registry.on_guest_detached
    previous_unknown = registry.on_unknown_frame

    def _on_attached(machine_id: str, session) -> None:
        if previous_attached is not None:
            previous_attached(machine_id, session)

        async def send_frame(frame):
            await session.ws.send_json(frame)
        syncer.attach_peer(machine_id, send_frame)
        asyncio.create_task(syncer.resubscribe(machine_id))

    def _on_detached(machine_id: str) -> None:
        if previous_detached is not None:
            previous_detached(machine_id)
        asyncio.create_task(syncer.detach_peer(machine_id))

    async def _on_unknown_frame(machine_id: str, payload: dict) -> bool:
        if await syncer.handle_frame(machine_id, payload):
            return True
        if previous_unknown is not None:
            return await previous_unknown(machine_id, payload)
        return False

    registry.on_guest_attached = _on_attached
    registry.on_guest_detached = _on_detached
    registry.on_unknown_frame = _on_unknown_frame


def install_guest_client_hooks(syncer: ChatSyncer, client) -> None:
    """guest 侧 GuestClient 接入 chat syncer（peer key = 'host'）。"""
    HOST_KEY = "host"
    previous_connect = client.on_connect
    previous_disconnect = client.on_disconnect
    previous_unknown = client.on_unknown_frame

    def _on_connect(connected_client) -> None:
        if previous_connect is not None:
            previous_connect(connected_client)

        async def send_frame(frame):
            ws = connected_client._ws
            if ws is None or ws.closed:
                return
            await ws.send_json(frame)
        syncer.attach_peer(HOST_KEY, send_frame)
        asyncio.create_task(syncer.resubscribe(HOST_KEY))

    def _on_disconnect() -> None:
        if previous_disconnect is not None:
            previous_disconnect()
        asyncio.create_task(syncer.detach_peer(HOST_KEY))

    async def _on_unknown_frame(payload: dict) -> bool:
        if await syncer.handle_frame(HOST_KEY, payload):
            return True
        if previous_unknown is not None:
            return await previous_unknown(payload)
        return False

    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_unknown_frame = _on_unknown_frame
