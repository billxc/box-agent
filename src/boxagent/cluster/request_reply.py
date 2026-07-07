"""基于 cluster bus 的 request/reply。

调用方指定目标机器和一个 HTTP 形状的请求，回复按 correlation_id 关联返回。
位置透明：调用方不区分 transport，由 bus 路由（含 guest→host→guest 中继）。
建在 `bus.send` + `bus.subscribe` 之上，不是独立 transport（"共用管道，不共用
模式"）：request/reply 只是 pub/sub 上的一层薄业务逻辑。

wire 形状（都是 bus 上普通的 `packet` 帧）：
  request  : receiver=<target>, topic="request.<target>",
             payload={method, path, query, body, correlation_id, reply_machine}
  reply    : receiver=<reply_machine>, topic="reply.<reply_machine>.<correlation_id>",
             payload={status, body, correlation_id}

responder 把请求打到自己的 web 端口、走真实 127.0.0.1 HTTP loopback（这样鉴权 +
真实的 _handle_web_* handler 都会跑），再发 reply 包。两跳中继由 bus 负责
（按 receiver 路由），loopback 从不中继。

drop-in 替代旧 ClusterRpc：`dispatch_machine_request` + `handle_guest_ws` 接口
不变，13 处 web-server 调用点和 ClusterHttpRoutes 无需改动。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Callable

# aiohttp 仅用于 responder 侧的 127.0.0.1 loopback HTTP 客户端（ClientSession）——
# 它是 HTTP 客户端，不需要 HTTP/2，保持原样。server 侧已迁到 Starlette。
from aiohttp import ClientSession
from starlette.responses import JSONResponse

from .cluster_bus import WIRE_VERSION as CLUSTER_BUS_WIRE_VERSION

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)


class _RequestSubscriber:
    """接收发给本机的入站请求的 bus 订阅者。用 create_task 调度异步 handler
    （请求低频且顺序无关，不像 chat 热路径）。"""

    def __init__(self, owner: "RequestReply") -> None:
        self._owner = owner

    def deliver(self, packet) -> None:
        self._owner._spawn_serve(packet.payload)


class _ReplySubscriber:
    """接收发给本机 pending 请求的入站回复的 bus 订阅者。"""

    def __init__(self, owner: "RequestReply") -> None:
        self._owner = owner

    def deliver(self, packet) -> None:
        self._owner._on_reply(packet.payload)


class RequestReply:
    def __init__(
        self,
        *,
        bus,
        topology,
        local_web_port: int,
        local_web_token: str = "",
        id_factory: "Callable[[], str] | None" = None,
    ) -> None:
        self._bus = bus
        self._topology = topology
        self._local_web_port = local_web_port
        self._local_web_token = local_web_token
        self._id_factory: "Callable[[], str]" = id_factory or (lambda: uuid.uuid4().hex)
        # correlation_id -> (future, target_machine)。保留 target，好在某个 peer
        # 变不可达时精确 fail 掉等它的那些请求。
        self._pending: dict[str, tuple[asyncio.Future, str]] = {}
        self._tasks: set = set()   # 在飞 responder task 的强引用
        self._http_session: ClientSession | None = None
        local = topology.local_machine_id()
        bus.subscribe(f"request.{local}", _RequestSubscriber(self))
        bus.subscribe(f"reply.{local}.", _ReplySubscriber(self))

    @property
    def _local(self) -> str:
        return self._topology.local_machine_id()

    # ── drop-in ClusterRpc 接口 ─────────────────────────────────────────

    async def dispatch_machine_request(
        self, machine: str, method: str, path: str,
        request: "Request", body: dict | None = None,
    ) -> "Response | None":
        """转发到远端机器并返回其响应。target 是本机时返回 None（调用方继续本地
        处理）。

        Fast-fail 门：发请求前查目标的 cluster-bus wire 版本。**只对确知不同版本**
        （正数且 != 本机）<1ms 回 502，不挂满 timeout，避免 web UI 把 ~6 个浏览器
        连接槽卡死。版本 0（未知/没学到/旧构建没报版本但可能同协议）**放行**——
        宁可让请求走一遭（兼容就成、真不通才走原 timeout），也不误杀。"""
        if machine == self._local:
            return None
        target_version = self._topology.version_for(machine)
        if target_version != 0 and target_version != CLUSTER_BUS_WIRE_VERSION:
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        f"{machine} is incompatible "
                        f"(wire version {target_version}, this node speaks {CLUSTER_BUS_WIRE_VERSION})"
                    ),
                },
                status_code=502,
            )
        reply = await self.request(
            machine, method, path, query=dict(request.query_params), body=body,
        )
        return JSONResponse(
            reply.get("body") or {}, status_code=int(reply.get("status") or 200),
        )

    async def handle_guest_ws(self, websocket: "WebSocket") -> None:
        """/api/guest/ws——本节点是 host 时委托给 GuestRegistry。

        Starlette WebSocket 路由 handler：非 host 时直接 close（WS 上无法回
        HTTP 状态码），host 时交给 registry 跑读循环。"""
        registry = self._topology.guest_registry
        if registry is None:
            await websocket.close(code=1011)
            return
        await registry.handle_ws(websocket)

    # ── caller 侧 ────────────────────────────────────────────────────────

    async def request(
        self, target_machine: str, method: str, path: str,
        *, query: dict | None = None, body: dict | None = None, timeout: float = 30.0,
    ) -> dict:
        """向 `target_machine` 发请求，等待关联的回复。返回
        ``{"status": int, "body": dict}``。若目标版本已知不兼容（dispatch 前置
        检查）或 bus 中途报它不可达，会 fast-fail（而非等满 timeout）。timeout 只是
        针对"兼容、在线但突然沉默的 peer"的兜底，不是主要 fast-fail 路径。
        （试过缩短统一 timeout 又撤回了：会误杀合法的慢请求；且 timeout 意味着
        "结果未知"而非"没发生"——真正的 RPC 框架会处理这个。）"""
        correlation_id = self._id_factory()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[correlation_id] = (future, target_machine)
        try:
            self._bus.send(
                receiver=target_machine,
                topic=f"request.{target_machine}",
                payload={
                    "method": method, "path": path,
                    "query": query or {}, "body": body,
                    "correlation_id": correlation_id,
                    "reply_machine": self._local,
                },
                ts=0.0,
            )
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return {"status": 504, "body": {"ok": False, "error": f"request to {target_machine} timed out"}}
        finally:
            self._pending.pop(correlation_id, None)

    def _on_reply(self, payload: dict) -> None:
        correlation_id = str(payload.get("correlation_id") or "")
        entry = self._pending.get(correlation_id)
        if entry is None:
            return
        future, _target = entry
        if not future.done():
            future.set_result({
                "status": int(payload.get("status") or 502),
                "body": payload.get("body") or {},
            })

    def fail_unreachable(self, machine: str) -> None:
        """快速 fail 掉所有指向 `machine` 的 pending 请求（bus 已告知该 peer 下线 /
        版本不兼容），不再挂到 timeout。"""
        for _correlation_id, (future, target) in list(self._pending.items()):
            if target == machine and not future.done():
                future.set_result({"status": 502, "body": {"ok": False, "error": f"{machine} unreachable"}})

    # ── responder 侧 ─────────────────────────────────────────────────────

    def _spawn_serve(self, payload: dict) -> None:
        task = asyncio.create_task(self._serve_request(payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve_request(self, payload: dict) -> None:
        result = await self._loopback(
            str(payload.get("method") or "GET"),
            str(payload.get("path") or ""),
            payload.get("query") or {},
            payload.get("body"),
        )
        reply_machine = str(payload.get("reply_machine") or "")
        correlation_id = str(payload.get("correlation_id") or "")
        if not reply_machine or not correlation_id:
            return
        self._bus.send(
            receiver=reply_machine,
            topic=f"reply.{reply_machine}.{correlation_id}",
            payload={"status": result["status"], "body": result["body"], "correlation_id": correlation_id},
            ts=0.0,
        )

    async def _loopback(self, method: str, path: str, query: dict, body) -> dict:
        """走真实 127.0.0.1 HTTP 重新打到本节点自己的 web 端口，让真实的
        _handle_web_* handler 带鉴权跑一遍。返回 {status, body}。"""
        if not self._local_web_port:
            return {"status": 503, "body": {"ok": False, "error": "loopback not configured"}}
        if self._http_session is None:
            self._http_session = ClientSession()
        url = f"http://127.0.0.1:{self._local_web_port}{path}"
        headers = {}
        if self._local_web_token:
            headers["Authorization"] = f"Bearer {self._local_web_token}"
        kwargs: dict = {"params": query, "headers": headers}
        if method.upper() != "GET" and body is not None:
            kwargs["json"] = body
        try:
            async with self._http_session.request(method, url, **kwargs) as response:
                try:
                    body_out = await response.json(content_type=None)
                except Exception:
                    body_out = {"raw": (await response.text())[:4096]}
                return {"status": response.status, "body": body_out}
        except Exception as exception:
            logger.warning("request_reply: loopback %s %s failed: %r", method, path, exception)
            return {"status": 502, "body": {"ok": False, "error": str(exception)}}

    async def aclose(self) -> None:
        if self._http_session is not None:
            try:
                await self._http_session.close()
            except Exception:
                pass
            self._http_session = None
