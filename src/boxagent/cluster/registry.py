"""Host 侧：跟踪已连接的 guest 节点并向它们代理 RPC。

*host* 节点在 ``/api/guest/ws``（旧别名 ``/api/sat/ws``）接受 *guest* 节点的
WebSocket 连接。每个 guest 用 ``hello`` 帧注册自己的 bot。host 随后把发往远端
bot 的 web-UI HTTP 请求，通过 WS 用通用 RPC 信封转发给拥有它的 guest。

wire 协议（当前，与 ``handle_ws`` 一致）::

  # Guest → Host（open 后立即）
  {"type": "hello", "machine_id": "pc", "token": "...", "bots": [...], "v": 3}

  # Host → Guest（握手回执，带 host 的 cluster-bus wire 版本 + machine_id）
  {"type": "welcome", "v": 3, "machine_id": "<host>"}

  # 双向：ClusterBus packet —— chat 广播 + RPC request/reply 都骑它
  {"type": "packet", "v": 3, "packet": {...}}   → cluster_bus.on_inbound

  # Host → Guest：机器拓扑快照 / Guest → Host：bot 列表变更
  {"type": "machines_snapshot", "machines": [...]}
  {"type": "bots_update", "bots": [...]}

  # 双向心跳
  {"type": "ping"}  /  {"type": "pong"}

registry 不认识的帧（EventSyncer 的 ``event_batch`` / ``event_resync``，wire v2）
落到 ``on_unknown_frame``。历史上的 ``rpc`` / ``rpc_resp`` / ``rpc_stream`` 与
ChatSyncer 的 ``chat_*`` 帧已被 ClusterBus 的 packet 路径取代（rpc / chat_sync
模块已删）。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from starlette.websockets import WebSocket, WebSocketDisconnect

from boxagent.log import Category, log

from .cluster_bus import WIRE_VERSION as CLUSTER_BUS_WIRE_VERSION
from .peer_transport import WIRE_VERSION

logger = logging.getLogger(__name__)


@dataclass
class RemoteBot:
    """guest 节点拥有的一个 bot 的元数据。"""

    name: str
    display_name: str = ""
    backend: str = ""
    model: str = ""
    kind: str = "bot"


@dataclass
class GuestSession:
    """一个已连接的 guest 节点。"""

    machine_id: str
    ws: WebSocket
    bots: list[RemoteBot] = field(default_factory=list)
    # 该 guest 在 hello 时协商的 cluster-bus wire 版本。0 = hello 没带 `v`
    # （旧/不兼容 peer）——用于对它快速 fail 请求，而非挂满 timeout。
    version: int = 0
    _closed: bool = False


@dataclass
class GuestRegistry:
    """Host 侧当前已连接 guest 的注册表。"""

    expected_token: str = ""
    sessions: dict[str, GuestSession] = field(default_factory=dict)
    # 本进程生命周期内见过的机器；断连后仍保留，好让 web UI 显示 offline 图块
    # 而非整行消失。
    history: dict[str, dict] = field(default_factory=dict)  # machine_id → {bots: [...], last_seen}
    # 可选：guest hello/bots_update 后、以及任意 guest 断连后由 handle_ws 调。
    # 让 host 向全部（或仅变更的）guest 推 machines_snapshot，使每个 guest 获知
    # 当前 cluster 拓扑。None = 不推。
    on_topology_change: Callable[[str | None], Awaitable[None]] | None = None
    # 可选：guest 发来任意未知类型帧时调。返回 True 表示已消费。events syncer 用它
    # 处理 event_batch / event_resync 帧，不必往 registry 里塞。
    on_unknown_frame: Callable[[str, dict], Awaitable[bool]] | None = None
    # 可选：guest hello/welcome 握手完成时调，让 syncer 按 machine_id 挂上 peer。
    on_guest_attached: Callable[[str, "GuestSession"], None] | None = None
    on_guest_detached: Callable[[str], None] | None = None
    # 进程内的 ClusterBus（duck-typed，避免 import 环）。设置后，已连接 guest 成为
    # cluster-bus 链路，入站 `packet` 帧路由给它。由 gateway 在 on_registry_ready
    # 里注入。
    cluster_bus: object | None = None
    # host 自己的 machine_id，随 welcome 发给 guest，好让 guest 把"host 这条链路"
    # 对上一个具体 machine_id（从活连接握手读版本，而非等异步 snapshot）。
    local_machine_id: str = ""

    def get(self, machine_id: str) -> GuestSession | None:
        return self.sessions.get(machine_id)

    def list_machines(self) -> list[dict]:
        """所有已知机器：已连接 + 最近见过。"""
        out: list[dict] = []
        seen: set[str] = set()
        now = time.time()
        for machine_id, session in self.sessions.items():
            seen.add(machine_id)
            out.append({
                "machine_id": machine_id,
                "online": True,
                "version": session.version,
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
                "version": 0,
                "bots": info.get("bots") or [],
                "last_seen": info.get("last_seen") or 0,
            })
        return out

    def list_bots(self) -> list[tuple[str, RemoteBot]]:
        """产出每个已注册远端 bot 的 (machine_id, RemoteBot)。"""
        out: list[tuple[str, RemoteBot]] = []
        for machine_id, session in self.sessions.items():
            for bot in session.bots:
                out.append((machine_id, bot))
        return out

    def get_bot(self, machine_id: str, name: str) -> RemoteBot | None:
        """返回 guest `machine_id` 上名为 `name` 的 RemoteBot，无则 None。"""
        session = self.sessions.get(machine_id)
        if session is None:
            return None
        for bot in session.bots:
            if bot.name == name:
                return bot
        return None

    async def close_all_sessions(self) -> None:
        """强制关闭每个已连接 guest WS。HostElection 降级时用，让 guest 立即重连到
        新 active host，而非吊在即将停掉的 tunnel 上。"""
        for session in list(self.sessions.values()):
            session._closed = True
            try:
                await session.ws.close()
            except Exception:
                pass
        self.sessions.clear()

    async def handle_ws(self, websocket: WebSocket) -> None:
        """/api/guest/ws 的 Starlette WebSocket handler。

        读循环用 ``await websocket.receive_text()``；guest 断开时抛
        ``WebSocketDisconnect`` 退出循环，走 finally 清理。wire 全是 JSON 文本帧，
        非文本帧不会出现（旧 aiohttp 版跳过非 TEXT 帧的分支不再需要）。"""
        await websocket.accept()

        # 等 hello
        session: GuestSession | None = None
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    payload = json.loads(data)
                except Exception:
                    logger.warning("guest ws: invalid JSON frame")
                    log.warning(Category.CLUSTER_PROTOCOL_ERROR, "guest ws: invalid JSON frame")
                    continue
                t = payload.get("type")

                if session is None:
                    if t != "hello":
                        await websocket.close(code=4001, reason="expected hello")
                        return
                    if self.expected_token and payload.get("token") != self.expected_token:
                        await websocket.close(code=4003, reason="bad token")
                        return
                    machine_id = str(payload.get("machine_id") or "").strip()
                    if not machine_id:
                        await websocket.close(code=4002, reason="missing machine_id")
                        return
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
                    guest_version = int(payload.get("v") or 0)
                    session = GuestSession(machine_id=machine_id, ws=websocket, bots=bots, version=guest_version)
                    # 若同 machine_id 的旧 session 还在，逐出它（guest 重连）。
                    old_session = self.sessions.get(machine_id)
                    if old_session is not None:
                        old_session._closed = True
                        try:
                            await old_session.ws.close()
                        except Exception:
                            pass
                    self.sessions[machine_id] = session
                    logger.info("guest '%s' connected with %d bot(s) (wire v%d)",
                                machine_id, len(bots), guest_version)
                    log.info(
                        Category.CLUSTER_GUEST_JOINED,
                        f"guest '{machine_id}' joined with {len(bots)} bot(s)",
                        machine_id=machine_id, bot_count=len(bots), wire_version=guest_version,
                    )
                    await websocket.send_json({
                        "type": "welcome",
                        "v": CLUSTER_BUS_WIRE_VERSION,
                        "machine_id": self.local_machine_id,
                    })
                    if self.cluster_bus is not None:
                        self.cluster_bus.attach_link(machine_id, websocket.send_json, version=guest_version)
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

                if t == "packet":
                    # 统一 cluster bus：路由给 ClusterBus（它跑自己的 v3 版本门）。
                    # 在下面的旧 v2 门之前拦截，否则 v3 packet 帧会被丢掉。
                    if self.cluster_bus is not None:
                        self.cluster_bus.on_inbound(session.machine_id, payload)
                    continue

                if payload.get("v", WIRE_VERSION) != WIRE_VERSION:
                    logger.warning("dropping frame from %s: unsupported wire version %r",
                                   session.machine_id, payload.get("v"))
                    continue

                if t == "ping":
                    await websocket.send_json({"type": "pong"})
                elif t == "bots_update":
                    # guest 重新宣告 bot 列表（如动态创建后）
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
        except WebSocketDisconnect:
            # guest 正常/异常断开——退出读循环，走 finally 清理。
            pass
        finally:
            # 被顶掉的旧协程（session._closed=True）绝不能碰共享状态——它的 link 和
            # sessions 槽位已被新连接接管。若在此无条件 detach_link，旧协程的 finally
            # 会把新连接刚 attach 的 link 删掉，留下"拓扑在线（sessions 里有）但
            # ClusterBus 不可达"的幽灵态，令该 guest 的所有跨机 RPC 应答被丢弃。
            # 故 detach 与 sessions.pop 同守卫：只有真·断连（未被顶掉）才清理。
            if session is not None and not session._closed:
                if self.cluster_bus is not None:
                    self.cluster_bus.detach_link(session.machine_id)
                self.sessions.pop(session.machine_id, None)
                # 记住 bot，好让 UI 继续把该行显示为 "offline"
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
