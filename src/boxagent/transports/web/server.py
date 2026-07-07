"""Web transport — Web UI 的 Starlette server、鉴权与路由 handler。

装配类，由 Gateway 持有为 ``self._web_server``，一次性用
config + storage + 共享 dict + topology + cluster_rpc + cluster_routes
（都是 Phase-1 兄弟）装配。

底层用 Starlette + Hypercorn 跑，拿 HTTP/2（明文 h2c + ALPN 协商）——aiohttp
官方不支持 HTTP/2，而 web UI 每 chat 一条 SSE/WS 连接会占满浏览器 HTTP/1.1 的
~6 连接槽。见 ``docs/decisions.md``。

host 的 web 端口（默认 9292）同时承载 cluster guest WS 路由
（``/api/guest/ws``），由 ``ClusterHttpRoutes.register`` 挂到同一个
Starlette app 上——因为 cluster 其余代码本就指向这个端口。
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


def _project_to_dict(p) -> dict:
    """把 ``boxagent.history.ProjectInfo`` 序列化给 Web UI。

    前端期望 ``encoded``（历史字段名）——用它承载 project_id，
    resume 选择器再原样传回 ``/api/claude/sessions``。
    """
    return {
        "encoded": p.project_id,
        "label": p.label,
        "cwd": p.cwd,
        "session_count": p.session_count,
        "last_ts": p.last_ts,
    }


def _session_info_to_dict(s) -> dict:
    """把 ``boxagent.history.SessionInfo`` 序列化给 Web UI。"""
    return {
        "session_id": s.session_id,
        "first_user": s.first_user,
        "message_count": s.message_count,
        "last_ts": s.last_ts,
        "summary": s.summary,
        "custom_title": s.custom_title,
        "git_branch": s.git_branch,
        "tag": s.tag,
        "created_at": s.created_at,
        "recap": s.recap,
    }


def _message_to_dict(m) -> dict:
    """把 ``boxagent.history.Message`` 序列化成 web UI transcript 回放用的旧记录结构。"""
    base = {"role": m.role, "ts": m.ts}
    if m.cwd:
        base["cwd"] = m.cwd
    if m.git_branch:
        base["git_branch"] = m.git_branch
    if m.role in ("user", "assistant", "skill_output"):
        base["text"] = m.text
    elif m.role == "tool_call":
        base["tool_id"] = m.tool_id
        base["name"] = m.name
        base["args"] = m.args
    elif m.role == "tool_result":
        base["tool_id"] = m.tool_id
        base["ok"] = m.ok
        base["summary"] = m.summary
        base["error"] = m.error
    return base


class WebHttpServer:
    def __init__(
        self,
        *,
        config,
        local_dir: Path,
        config_dir: Path,
        storage,
        web_channels: dict,
        pools: dict,
        topology,
        cluster_rpc,
        cluster_routes,
        message_bus=None,
    ) -> None:
        self.config = config
        self.local_dir = local_dir
        self.config_dir = config_dir
        self.storage = storage
        self.web_channels = web_channels
        self.pools = pools
        self.topology = topology
        self.cluster_rpc = cluster_rpc
        self.cluster_routes = cluster_routes
        self.message_bus = message_bus
        self.event_bus = None
        # 进程内 preview 缓存（sid → {mtime, ...}），
        # /api/sessions 用它避免每次轮询都重读 transcript JSONL。
        self.session_meta_cache: dict = {}
        self._serve_task: asyncio.Task | None = None
        self._shutdown_event: asyncio.Event | None = None

    def set_event_bus(self, event_bus) -> None:
        self.event_bus = event_bus

    @property
    def web_port_file(self) -> Path:
        return self.local_dir / "web-port.txt"

    # ── 生命周期 ──

    def build_app(self) -> Starlette:
        """构建 Starlette app（路由 + cluster 路由 + static + error 中间件）。

        抽成独立方法，测试用它直接起 ``TestClient(server.build_app())``。
        """
        from boxagent.web_error_middleware import ErrorLoggingMiddleware

        routes = self._collect_routes()

        # 共用此端口的非 web 路由（cluster guest WS 等）
        if self.cluster_routes is not None:
            self.cluster_routes.register(routes)

        # 静态文件放最后，避免 catch-all 遮蔽 API 路由
        web_static = Path(__file__).parent / "static"
        if web_static.is_dir():
            routes.append(
                Mount("/", app=StaticFiles(directory=str(web_static), html=False))
            )

        return Starlette(
            routes=routes,
            middleware=[Middleware(ErrorLoggingMiddleware)],
        )

    async def start(self) -> None:
        """构建并启动 Web UI Starlette server（独立端口，HTTP/2 via Hypercorn）。"""
        from hypercorn.asyncio import serve
        from hypercorn.config import Config

        app = self.build_app()

        host = self.config.web_host or "127.0.0.1"
        port = self.config.web_port if self.config.web_port is not None else 9292

        config = Config()
        config.bind = [f"{host}:{port}"]
        # h2c（明文 HTTP/2，无 TLS）+ HTTP/1.1 兼容——浏览器直连 tunnel 时由
        # devtunnel 终结 TLS 并 ALPN 协商到 h2；本机 curl 用 --http2-prior-knowledge
        # 直接跑 h2c。
        config.alpn_protocols = ["h2", "http/1.1"]
        config.accesslog = None
        config.errorlog = None

        self._shutdown_event = asyncio.Event()
        shutdown_event = self._shutdown_event
        self._serve_task = asyncio.create_task(
            serve(app, config, shutdown_trigger=shutdown_event.wait)
        )
        # Hypercorn 编程式启动拿不到 bind 到 0 时的随机端口；web_port 恒为具体值
        # （默认 9292），直接写下即可。
        self.web_port_file.write_text(f"{port}\n", encoding="utf-8")
        logger.info("Web UI listening on %s:%d (HTTP/2 enabled)", host, port)

    async def stop(self) -> None:
        if self._shutdown_event is not None:
            self._shutdown_event.set()
        if self._serve_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._serve_task), timeout=5.0)
            except asyncio.TimeoutError:
                self._serve_task.cancel()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning("web server serve task ended with error: %r", e)
            self._serve_task = None
        self._shutdown_event = None
        self.web_port_file.unlink(missing_ok=True)

    def _collect_routes(self) -> list:
        return [
            Route("/", self._handle_web_index, methods=["GET"]),
            Route("/api/bots", self._handle_web_bots, methods=["GET"]),
            Route("/api/machines", self._handle_web_machines, methods=["GET"]),
            Route("/api/sessions", self._handle_web_sessions, methods=["GET"]),
            Route("/api/sessions/rename", self._handle_rename_session, methods=["POST"]),
            Route("/api/session_info", self._handle_web_session_info, methods=["GET"]),
            Route("/api/version", self._handle_version, methods=["GET"]),
            Route("/api/admin/restart", self._handle_admin_restart, methods=["POST"]),
            Route("/api/admin/cluster_restart", self._handle_admin_cluster_restart, methods=["POST"]),
            Route("/api/history", self._handle_web_history, methods=["GET"]),
            Route("/api/send", self._handle_web_send, methods=["POST"]),
            Route("/api/stream", self._handle_web_stream, methods=["GET"]),
            WebSocketRoute("/api/multiplex", self._handle_web_multiplex),
            Route("/api/claude/projects", self._handle_claude_projects, methods=["GET"]),
            Route("/api/claude/sessions", self._handle_claude_sessions, methods=["GET"]),
            Route("/api/claude/transcript", self._handle_claude_transcript, methods=["GET"]),
            Route("/api/claude/resume", self._handle_claude_resume, methods=["POST"]),
            # 事件日志路由
            Route("/api/events", self._handle_events_query, methods=["GET"]),
            Route("/api/events/stream", self._handle_events_stream, methods=["GET"]),
            Route("/api/events/categories", self._handle_events_categories, methods=["GET"]),
            Route("/api/events/machines", self._handle_events_machines, methods=["GET"]),
            Route("/api/events/{event_id}/read", self._handle_events_mark_read, methods=["POST"]),
            Route("/api/events/read_all", self._handle_events_read_all, methods=["POST"]),
            # 原始日志文件（boxagent.log）
            Route("/api/logs", self._handle_logs_query, methods=["GET"]),
            # schedule 运行记录路由
            Route("/api/schedules", self._handle_schedules_list, methods=["GET"]),
            Route("/api/schedules/runs", self._handle_schedules_runs, methods=["GET"]),
            Route(
                "/api/schedules/runs/{task_id}/{run_index}",
                self._handle_schedules_run_detail, methods=["GET"],
            ),
        ]

    # ── 鉴权 ──

    def _authorized(self, request) -> bool:
        """放行 localhost、可信 header（tunnel）或匹配的 bearer/query token。

        ``request`` 是 Starlette ``Request`` 或 ``WebSocket``——两者都有
        ``client`` / ``headers`` / ``query_params``，鉴权逻辑对二者通用。
        """
        token = (self.config.web_token or "").strip()
        host = (request.client.host if request.client else "") or ""
        if host in ("127.0.0.1", "::1", "localhost"):
            return True
        trust_header = (self.config.web_trust_header or "").strip()
        if trust_header and request.headers.get(trust_header):
            return True
        if not token:
            return False
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:].strip() == token:
            return True
        if request.query_params.get("token", "") == token:
            return True
        return False

    def _unauthorized(self) -> Response:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    # ── 请求处理 ──

    async def _handle_web_index(self, request: Request) -> Response:
        # 始终返回 index 页，便于用户粘贴 ?token=... 登录。
        index = Path(__file__).parent / "static" / "index.html"
        if not index.is_file():
            return Response("web UI not installed", status_code=404)
        return Response(index.read_bytes(), media_type="text/html")

    async def _handle_web_bots(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        bots = []
        local_machine_id = self.topology.local_machine_id()
        for name, channel in self.web_channels.items():
            config = self.config.bots.get(name)
            if config is not None:
                bots.append({
                    "name": name,
                    "display_name": config.display_name or name,
                    "backend": config.ai_backend,
                    "model": config.model,
                    "kind": "bot",
                    "machine": local_machine_id,
                })
        if self.topology.guest_registry is not None:
            for machine_id, bot in self.topology.guest_registry.list_bots():
                bots.append({
                    "name": bot.name,
                    "display_name": (bot.display_name or bot.name) + f"  @{machine_id}",
                    "backend": bot.backend,
                    "model": bot.model,
                    "kind": bot.kind,
                    "machine": machine_id,
                })
        elif self.topology.guest_client is not None:
            for m in self.topology.guest_client.remote_machines:
                machine_id = m.get("machine_id") or ""
                if not machine_id or machine_id == local_machine_id:
                    continue
                for bot in m.get("bots") or []:
                    bots.append({
                        "name": bot.get("name") or "",
                        "display_name": (bot.get("display_name") or bot.get("name") or "") + f"  @{machine_id}",
                        "backend": bot.get("backend") or "",
                        "model": bot.get("model") or "",
                        "kind": bot.get("kind") or "bot",
                        "machine": machine_id,
                    })
        return JSONResponse({"bots": bots})

    async def _handle_web_machines(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        if self.topology.guest_registry is not None:
            return JSONResponse({"machines": self.topology.collect_machines()})
        local_machine_id = self.topology.local_machine_id()
        local_role = self.topology.local_role()
        machines: list[dict] = [{
            "machine_id": local_machine_id,
            "online": True,
            "role": local_role,
            "self": True,
            "bots": self.topology.local_bot_descriptors(),
            "last_seen": time.time(),
        }]
        if self.topology.guest_client is not None:
            for m in self.topology.guest_client.remote_machines:
                if m.get("machine_id") == local_machine_id:
                    continue
                m = dict(m)
                m["self"] = False
                machines.append(m)
        return JSONResponse({"machines": machines})

    async def _handle_web_sessions(self, request: Request) -> Response:
        from boxagent.utils import infer_platform
        if not self._authorized(request):
            return self._unauthorized()
        bot = request.query_params.get("bot", "")
        machine = request.query_params.get("machine", "")
        if not bot or not machine:
            return JSONResponse({"ok": False, "error": "missing bot/machine"}, status_code=400)
        response = await self.cluster_rpc.dispatch_machine_request(machine, "GET", "/api/sessions", request)
        if response is not None:
            return response
        if bot not in self.web_channels:
            return JSONResponse({"ok": False, "error": "bot not web-enabled"}, status_code=404)
        if not self.storage:
            return JSONResponse({"ok": True, "sessions": []})

        sessions = self.storage.list_chat_sessions(bot)

        bot_config = self.config.bots.get(bot)
        backend = (bot_config.ai_backend if bot_config else None) or "claude-cli"
        if backend in ("claude-cli", "agent-sdk-claude"):
            try:
                from boxagent.history import get_history
                history = get_history(backend)
                # SDK 通过 get_session_info 直接按 sid 给出会话元数据，
                # 无需扫描 project 树。claude_session_info 在下面循环里懒填。
                _claude_history = history
            except Exception:
                _claude_history = None
        else:
            _claude_history = None

        for s in sessions:
            sid = s.get("session_id") or ""
            s["platform"] = infer_platform(s["chat_id"])
            s["preview"] = ""
            s["last_ts"] = 0
            s["message_count"] = 0
            s["summary"] = ""
            s["custom_title"] = None
            s["recap"] = ""
            if not sid:
                continue

            cached = self.session_meta_cache.get(sid)

            # 先试 claude-native 查找（覆盖 claude-cli + agent-sdk-claude）。
            if _claude_history is not None:
                info = await _claude_history.get_session_info(sid)
                if info is not None:
                    # 仅当 SDK 报告的 last_modified 一致时才复用缓存——
                    # 否则会话有新消息 / away_summary / 改名，必须重读。
                    if cached and cached.get("last_ts") == info.last_ts:
                        s["preview"] = cached.get("preview", "")
                        s["last_ts"] = cached.get("last_ts", 0)
                        s["message_count"] = cached.get("message_count", 0)
                        s["summary"] = cached.get("summary", "")
                        s["custom_title"] = cached.get("custom_title")
                        s["recap"] = cached.get("recap", "")
                        continue
                    preview = (info.first_user or "").strip().replace("\n", " ")
                    s["preview"] = preview[:90] + ("..." if len(preview) > 90 else "")
                    s["last_ts"] = info.last_ts
                    s["message_count"] = info.message_count
                    s["summary"] = info.summary or ""
                    s["custom_title"] = info.custom_title
                    s["recap"] = info.recap or ""
                    self.session_meta_cache[sid] = {
                        "mtime": info.last_ts,
                        "preview": s["preview"],
                        "last_ts": s["last_ts"],
                        "message_count": s["message_count"],
                        "summary": s["summary"],
                        "custom_title": s["custom_title"],
                        "recap": s["recap"],
                    }
                    continue

            tpath = self.storage.local_dir / "transcripts" / f"{sid}.jsonl"
            if not tpath.is_file():
                continue
            try:
                tstat = tpath.stat()
                if cached and cached.get("mtime") == tstat.st_mtime:
                    s["preview"] = cached.get("preview", "")
                    s["last_ts"] = cached.get("last_ts", 0)
                    s["message_count"] = cached.get("message_count", 0)
                    continue

                last_user = ""
                last_assist = ""
                last_ts = 0.0
                message_count = 0
                for line in tpath.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("chat_id") and rec.get("chat_id") != s["chat_id"]:
                        continue
                    event = rec.get("event")
                    text = rec.get("text", "") or ""
                    ts = float(rec.get("ts", 0) or 0)
                    if ts > last_ts:
                        last_ts = ts
                    if event == "user":
                        last_user = text
                        message_count += 1
                    elif event == "assistant":
                        last_assist = text
                        message_count += 1
                preview = (last_assist or last_user or "").strip().replace("\n", " ")
                s["preview"] = preview[:90] + ("..." if len(preview) > 90 else "")
                s["last_ts"] = last_ts
                s["message_count"] = message_count
                self.session_meta_cache[sid] = {
                    "mtime": tstat.st_mtime,
                    "preview": s["preview"],
                    "last_ts": s["last_ts"],
                    "message_count": message_count,
                }
            except Exception as e:
                logger.debug("session preview read failed for %s: %s", sid, e)

        sessions.sort(key=lambda x: x.get("last_ts") or 0, reverse=True)
        return JSONResponse({"ok": True, "sessions": sessions})

    async def _handle_rename_session(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
        bot = str(data.get("bot") or "").strip()
        machine = str(data.get("machine") or "").strip()
        session_id = str(data.get("session_id") or "").strip()
        title = str(data.get("title") or "").strip()
        if not bot or not machine or not session_id:
            return JSONResponse({"ok": False, "error": "missing bot/machine/session_id"}, status_code=400)
        if not title:
            return JSONResponse({"ok": False, "error": "title is empty"}, status_code=400)
        if machine != self.topology.local_machine_id():
            response = await self.cluster_rpc.dispatch_machine_request(
                machine, "POST", "/api/sessions/rename", request, body=data,
            )
            if response is not None:
                return response
        bot_config = self.config.bots.get(bot)
        backend = (bot_config.ai_backend if bot_config else None) or "claude-cli"
        if backend not in ("claude-cli", "agent-sdk-claude"):
            return JSONResponse(
                {"ok": False, "error": f"backend {backend!r} does not support rename"},
                status_code=400,
            )
        try:
            from boxagent.history import get_history
            history = get_history(backend)
            await history.rename_session(session_id, title)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        self.session_meta_cache.pop(session_id, None)
        return JSONResponse({"ok": True, "session_id": session_id, "title": title})

    async def _handle_version(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        from boxagent._version import __version__, _git_commit, version_string

        local = {
            "machine_id": self.topology.local_machine_id(),
            "version": __version__,
            "commit": _git_commit(),
            "version_string": version_string(),
        }
        if request.query_params.get("cluster") not in ("1", "true", "yes"):
            return JSONResponse({"ok": True, **local})
        if self.topology.guest_registry is not None:
            sats: dict[str, object] = {}
            for machine_id, session in list(self.topology.guest_registry.sessions.items()):
                try:
                    result = await self.cluster_rpc.request(machine_id, "GET", "/api/version", timeout=5.0)
                    sats[machine_id] = result.get("body") or {"error": "no body"}
                except Exception as e:
                    sats[machine_id] = {"error": str(e)}
            return JSONResponse({"ok": True, "self": local, "sats": sats})
        if self.topology.guest_client is not None:
            try:
                host_result = await self.topology.guest_client.fetch_host_json("/api/version", {"cluster": "1"})
            except Exception as e:
                host_result = {"error": str(e)}
            return JSONResponse({"ok": True, "self": local, "host": host_result})
        return JSONResponse({"ok": True, "self": local, "sats": {}})

    async def _handle_admin_restart(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        import os
        import signal as _signal
        loop = asyncio.get_event_loop()
        loop.call_later(0.2, lambda: os.kill(os.getpid(), _signal.SIGTERM))
        return JSONResponse({
            "ok": True, "restarting": self.topology.local_machine_id(),
            "note": "SIGTERM scheduled in 0.2s; supervisor must relaunch",
        })

    async def _handle_admin_cluster_restart(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        try:
            data = await request.json()
        except Exception:
            data = {}
        if self.topology.guest_registry is None:
            # Guest 模式：只有 host 持有 cluster registry，
            # 故把请求上抛并透传其响应。让每机的 Restart 按钮
            # 从任意节点 UI 都能用。
            guest_client = self.topology.guest_client
            if guest_client is None:
                return JSONResponse(
                    {"ok": False, "error": "no host connection"}, status_code=503,
                )
            try:
                result = await guest_client.fetch_host_json(
                    "/api/admin/cluster_restart",
                    query=dict(request.query_params),
                    method="POST",
                    body=data if isinstance(data, dict) else {},
                )
            except Exception as e:
                return JSONResponse(
                    {"ok": False, "error": f"host fwd failed: {e}"}, status_code=502,
                )
            return JSONResponse(result)
        include_self = request.query_params.get("include_self") in ("1", "true", "yes")
        target_filter: list[str] | None = None
        if isinstance(data, dict):
            if not include_self:
                include_self = bool(data.get("include_self"))
            raw = data.get("machines")
            if isinstance(raw, list) and raw:
                target_filter = [str(m) for m in raw]
        results: dict[str, object] = {}
        for machine_id, session in list(self.topology.guest_registry.sessions.items()):
            if target_filter is not None and machine_id not in target_filter:
                continue
            try:
                rpc = await self.cluster_rpc.request(machine_id, "POST", "/api/admin/restart", timeout=5.0)
                results[machine_id] = rpc.get("body") or {"status": rpc.get("status")}
            except Exception as e:
                results[machine_id] = {"error": str(e)}
        if include_self and (target_filter is None or self.topology.local_machine_id() in target_filter):
            import os
            import signal as _signal
            asyncio.get_event_loop().call_later(
                1.0, lambda: os.kill(os.getpid(), _signal.SIGTERM),
            )
            results[self.topology.local_machine_id()] = {
                "scheduled": True, "delay_seconds": 1.0,
            }
        return JSONResponse({"ok": True, "results": results})

    async def _handle_web_history(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        bot = request.query_params.get("bot", "")
        chat_id = request.query_params.get("chat_id", "")
        machine = request.query_params.get("machine", "")
        if not bot or not chat_id or not machine:
            return JSONResponse({"ok": False, "error": "missing bot/chat_id/machine"}, status_code=400)
        if machine != self.topology.local_machine_id():
            response = await self.cluster_rpc.dispatch_machine_request(machine, "GET", "/api/history", request)
            if response is not None:
                return response
        if bot not in self.web_channels:
            return JSONResponse({"ok": False, "error": "bot not web-enabled"}, status_code=404)

        history: list[dict] = []
        if self.storage:
            saved = self.storage.load_session(bot, chat_id)
            session_id = ""
            prev_chain: list[str] = []
            saved_backend = ""
            if isinstance(saved, dict):
                session_id = saved.get("session_id", "")
                raw_prev = saved.get("previous_session_ids") or []
                if isinstance(raw_prev, list):
                    prev_chain = [str(s) for s in raw_prev if isinstance(s, str) and s]
                saved_backend = str(saved.get("backend", "") or "")
            elif isinstance(saved, str):
                session_id = saved
            sids = ([session_id] if session_id else []) + prev_chain

            if saved_backend in ("claude-cli", "agent-sdk-claude") and sids:
                from boxagent.history import get_history
                history_impl = get_history(saved_backend)
                # 从已知最老的 session 开始沿 SDK 的 compact 链回溯——
                # 兜住 storage 的 previous_session_ids 漏跳的情况
                # （跨机恢复、手动 /resume native session、storage 不可用时
                # native 自动 compact）。
                walk_method = getattr(history_impl, "walk_compact_chain", None)
                if walk_method is not None and sids:
                    try:
                        ancestors = await walk_method(sids[-1])
                    except Exception:  # 回溯是尽力而为
                        ancestors = []
                    if ancestors:
                        existing = set(sids)
                        # ancestors 是老到新；我们渲染新到老 → 反转
                        for sid in reversed(ancestors):
                            if sid not in existing:
                                sids.append(sid)
                                existing.add(sid)
                # SDK 不需要我们知道 project 也能找到 session——
                # 传空 project_id 让它自己搜。
                for sid in sids:
                    messages = await history_impl.read_messages(sid)
                    history.extend(_message_to_dict(m) for m in messages)
                history.sort(key=lambda r: r.get("ts") or 0)
                total = len(history)
                limit = int(request.query_params.get("limit", 0) or 0)
                offset = int(request.query_params.get("offset", 0) or 0)
                if limit > 0:
                    history = history[-(offset + limit):len(history) - offset if offset else None]
                return JSONResponse({"ok": True, "total": total, "history": history})

            for sid in sids:
                tpath = self.storage.local_dir / "transcripts" / f"{sid}.jsonl"
                if not tpath.is_file():
                    continue
                try:
                    for line in tpath.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        event = rec.get("event")
                        if event not in ("user", "assistant"):
                            continue
                        if rec.get("chat_id") and rec.get("chat_id") != chat_id:
                            continue
                        history.append({
                            "role": event,
                            "text": rec.get("text", ""),
                            "ts": rec.get("ts", 0),
                        })
                except Exception as e:
                    logger.warning("history read failed for %s: %s", tpath, e)
            history.sort(key=lambda r: r.get("ts") or 0)
        total = len(history)
        limit = int(request.query_params.get("limit", 0) or 0)
        offset = int(request.query_params.get("offset", 0) or 0)
        if limit > 0:
            history = history[-(offset + limit):len(history) - offset if offset else None]
        return JSONResponse({"ok": True, "total": total, "history": history})

    async def _handle_web_session_info(self, request: Request) -> Response:
        """单个 ``session_id`` 的 SessionInfo（与 chat 解耦）。

        必填：``session_id``、``backend_kind``、``machine``（cluster_rpc 分发用）。
        可选：``model``（查 context_window）、``workspace``（帮助定位 Codex transcript）。
        """
        if not self._authorized(request):
            return self._unauthorized()
        session_id = request.query_params.get("session_id", "")
        backend_kind = request.query_params.get("backend_kind", "")
        machine = request.query_params.get("machine", "")
        model = request.query_params.get("model", "")
        workspace = request.query_params.get("workspace", "")
        if not session_id or not backend_kind or not machine:
            return JSONResponse(
                {"ok": False, "error": "missing session_id/backend_kind/machine"},
                status_code=400,
            )
        if machine != self.topology.local_machine_id():
            response = await self.cluster_rpc.dispatch_machine_request(
                machine, "GET", "/api/session_info", request,
            )
            if response is not None:
                return response
        from boxagent.sessions.info_builder import build_session_info
        try:
            info = await build_session_info(
                session_id=session_id,
                backend_kind=backend_kind,
                model=model,
                workspace=workspace,
            )
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        return JSONResponse({"ok": True, "info": {
            "session_id": info.session_id,
            "backend_kind": info.backend_kind,
            "model": info.model,
            "workspace": info.workspace,
            "last_turn_usage": info.last_turn_usage,
            "message_count": info.message_count,
            "last_ts": info.last_ts,
            "context_window": info.context_window,
            "context_used": info.context_used,
            "extra": info.extra,
        }})

    async def _handle_web_send(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        bot = body.get("bot", "")
        chat_id = body.get("chat_id", "")
        text = body.get("text", "")
        machine = body.get("machine", "")
        if not bot or not chat_id or not text or not machine:
            return JSONResponse({"ok": False, "error": "missing bot/chat_id/text/machine"}, status_code=400)
        if machine != self.topology.local_machine_id():
            response = await self.cluster_rpc.dispatch_machine_request(machine, "POST", "/api/send", request, body=body)
            if response is not None:
                return response
        channel = self.web_channels.get(bot)
        if channel is None:
            return JSONResponse({"ok": False, "error": "bot not web-enabled"}, status_code=404)
        try:
            await channel.inject(chat_id=chat_id, text=text, user_id="web")
        except Exception as e:
            logger.exception("web send failed")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        return JSONResponse({"ok": True})

    def _resolve_chat_topic(self, machine: str, bot: str, chat_id: str) -> str | None:
        """把 (machine, bot, chat_id) 选择器映射到其 bus topic；
        若 bot 在本机但未开启 web 则返回 None。

        位置透明：浏览器订阅 chat.<owner>.<bot>.<chat_id>，bus 负责投递——
        无论 owning bot 在本机（其 WebChannel 本地发布）还是远端
        （ClusterBus 把广播包转发过来）。
        """
        local_machine = self.topology.local_machine_id()
        owner = machine or local_machine
        if owner == local_machine and self.web_channels.get(bot) is None:
            return None
        return f"chat.{owner}.{bot}.{chat_id}"

    async def _handle_web_stream(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        bot = request.query_params.get("bot", "")
        chat_id = request.query_params.get("chat_id", "")
        machine = request.query_params.get("machine", "")
        if not bot or not chat_id or not machine:
            return JSONResponse({"ok": False, "error": "missing bot/chat_id/machine"}, status_code=400)
        if self.message_bus is None:
            return JSONResponse({"ok": False, "error": "bus unavailable"}, status_code=503)

        from boxagent.bus.subscriber import QueueSubscriber
        topic = self._resolve_chat_topic(machine, bot, chat_id)
        if topic is None:
            return JSONResponse({"ok": False, "error": "bot not web-enabled"}, status_code=404)
        queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
        subscription = self.message_bus.subscribe(topic, QueueSubscriber(queue, topic))

        async def event_generator():
            # StreamingResponse 会在客户端断开时取消这个 generator，
            # 触发 finally 里的 subscription.close()——等价于旧 aiohttp
            # 写失败时的清理。
            yield b": connected\n\n"
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=20.0)
                    except asyncio.TimeoutError:
                        yield b": ping\n\n"
                        continue
                    if event.get("type") == "_close":
                        break
                    payload = json.dumps(event, ensure_ascii=False)
                    yield f"data: {payload}\n\n".encode("utf-8")
            except asyncio.CancelledError:
                pass
            finally:
                subscription.close()

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def _handle_web_multiplex(self, websocket: WebSocket) -> None:
        """一个页面级 WebSocket，汇聚多个 chat 的事件。

        每 chat 一条 SSE 会各占浏览器约 6 个 HTTP/1.1 连接槽之一，
        开几个 chat 就把整个 UI 卡死。此端点改为在单个 socket 上持有多个 bus
        订阅：客户端开关 chat 时发送
        ``{"type":"subscribe","machine":..,"bot":..,"chat_id":..}`` /
        ``"unsubscribe"`` 帧，每个推送事件都带
        ``{machine,bot,chat_id,event:{...}}`` 标签，浏览器据此解复用到对应窗口。
        连接数从 N 降到 1。
        """
        # 鉴权用同一套规则（WebSocket 也有 client/headers/query_params）。
        if not self._authorized(websocket):
            await websocket.close(code=4401)
            return
        if self.message_bus is None:
            await websocket.close(code=1011)
            return

        from boxagent.bus.subscriber import TaggedQueueSubscriber

        await websocket.accept()

        queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
        # topic -> Subscription，使重复订阅幂等、取消订阅能找到它。
        subscriptions: dict[str, object] = {}

        def subscribe(machine: str, bot: str, chat_id: str) -> None:
            if not machine or not bot or not chat_id:
                return
            topic = self._resolve_chat_topic(machine, bot, chat_id)
            if topic is None or topic in subscriptions:
                return
            tag = {"machine": machine, "bot": bot, "chat_id": chat_id}
            subscriptions[topic] = self.message_bus.subscribe(
                topic, TaggedQueueSubscriber(queue, tag, topic)
            )

        def unsubscribe(machine: str, bot: str, chat_id: str) -> None:
            topic = self._resolve_chat_topic(machine, bot, chat_id)
            subscription = subscriptions.pop(topic, None) if topic else None
            if subscription is not None:
                subscription.close()

        async def pump() -> None:
            # 把合并队列排空到 socket。tagged 事件已带 {machine,bot,chat_id}，
            # 浏览器据此路由。
            while True:
                event = await queue.get()
                await websocket.send_json(event)

        pump_task = asyncio.create_task(pump())
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    frame = json.loads(data)
                except Exception:
                    logger.warning("multiplex ws: invalid JSON frame")
                    continue
                kind = frame.get("type")
                machine = str(frame.get("machine") or "")
                bot = str(frame.get("bot") or "")
                chat_id = str(frame.get("chat_id") or "")
                if kind == "subscribe":
                    subscribe(machine, bot, chat_id)
                elif kind == "unsubscribe":
                    unsubscribe(machine, bot, chat_id)
                # 未知帧忽略
        except WebSocketDisconnect:
            pass
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            pump_task.cancel()
            for subscription in subscriptions.values():
                subscription.close()
            subscriptions.clear()

    # ── Claude 原生 session 选择器 ──

    async def _handle_claude_projects(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        machine = request.query_params.get("machine", "")
        if not machine:
            return JSONResponse({"ok": False, "error": "missing machine"}, status_code=400)
        if machine != self.topology.local_machine_id():
            response = await self.cluster_rpc.dispatch_machine_request(machine, "GET", "/api/claude/projects", request)
            if response is not None:
                return response
        from boxagent.history import get_history
        history = get_history("claude-cli")
        projects = await history.list_projects()
        total = len(projects)
        try:
            limit = int(request.query_params.get("limit", "30"))
        except ValueError:
            limit = 30
        try:
            offset = int(request.query_params.get("offset", "0"))
        except ValueError:
            offset = 0
        offset = max(offset, 0)
        if limit > 0:
            page = projects[offset:offset + limit]
        else:
            page = projects[offset:]
        return JSONResponse({
            "ok": True,
            "projects": [_project_to_dict(p) for p in page],
            "total": total,
            "offset": offset,
            "has_more": offset + len(page) < total,
        })

    async def _handle_claude_sessions(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        encoded = request.query_params.get("project", "")
        machine = request.query_params.get("machine", "")
        if not encoded or not machine:
            return JSONResponse({"ok": False, "error": "missing project/machine"}, status_code=400)
        if machine != self.topology.local_machine_id():
            response = await self.cluster_rpc.dispatch_machine_request(machine, "GET", "/api/claude/sessions", request)
            if response is not None:
                return response
        from boxagent.history import get_history
        history = get_history("claude-cli")
        limit_raw = request.query_params.get("limit")
        if limit_raw is not None:
            try:
                limit = max(0, min(500, int(limit_raw)))
                offset = max(0, int(request.query_params.get("offset", "0")))
            except ValueError:
                return JSONResponse({"ok": False, "error": "invalid offset/limit"}, status_code=400)
            sessions, total = await history.list_sessions_paginated(encoded, offset, limit)
            return JSONResponse({
                "ok": True,
                "sessions": [_session_info_to_dict(s) for s in sessions],
                "total": total,
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(sessions) < total,
            })
        sessions = await history.list_sessions(encoded)
        return JSONResponse({
            "ok": True,
            "sessions": [_session_info_to_dict(s) for s in sessions],
        })

    async def _handle_claude_transcript(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        encoded = request.query_params.get("project", "")
        sid = request.query_params.get("session_id", "")
        machine = request.query_params.get("machine", "")
        backend_kind = request.query_params.get("backend", "") or "claude-cli"
        if not encoded or not sid or not machine:
            return JSONResponse({"ok": False, "error": "missing project/session_id/machine"}, status_code=400)
        if machine != self.topology.local_machine_id():
            response = await self.cluster_rpc.dispatch_machine_request(machine, "GET", "/api/claude/transcript", request)
            if response is not None:
                return response
        from boxagent.history import get_history
        try:
            history = get_history(backend_kind)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        messages = await history.read_messages(sid, encoded)
        return JSONResponse({
            "ok": True,
            "messages": [_message_to_dict(m) for m in messages],
        })

    async def _handle_claude_resume(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        bot = body.get("bot", "")
        sid = body.get("session_id", "")
        encoded = body.get("project", "")
        machine = body.get("machine", "")
        backend_override = body.get("backend", "")
        if not bot or not sid or not machine:
            return JSONResponse({"ok": False, "error": "missing bot/session_id/machine"}, status_code=400)
        if machine != self.topology.local_machine_id():
            response = await self.cluster_rpc.dispatch_machine_request(machine, "POST", "/api/claude/resume", request, body=body)
            if response is not None:
                return response
        if bot not in self.web_channels:
            return JSONResponse({"ok": False, "error": "bot not web-enabled"}, status_code=404)

        is_raw = bot == "raw"
        workspace = ""
        if self.storage:
            config = self.config.bots.get(bot)
            model = (config.model if config else None) or ""
            if is_raw:
                backend = backend_override or "claude-cli"
            else:
                backend = (config.ai_backend if config else None) or "claude-cli"
            # ``encoded``（project_id）现在就是 cwd 路径，直接用。
            # 老调用方不传 project 时仍走下面的 bot 默认值。
            workspace = encoded or (config.workspace if config else "")
            chat_id = f"{backend.split('-')[0]}-{sid}" if is_raw else f"claude-{sid}"
            self.storage.save_session(
                bot, sid,
                preview="(resumed via web)",
                backend=backend,
                chat_id=chat_id,
                model=model,
                workspace=workspace,
            )
            pool = self.pools.get(bot)
            if pool is not None:
                if workspace:
                    pool.set_workspace(chat_id, workspace)
                pool.set_session_id(chat_id, sid)
                if is_raw and hasattr(pool, "set_backend"):
                    pool.set_backend(chat_id, backend)
        else:
            chat_id = f"claude-{sid}"
        return JSONResponse({
            "ok": True,
            "chat_id": chat_id,
            "session_id": sid,
            "project": encoded,
            "backend": backend if self.storage else "",
            "workspace": workspace if self.storage else "",
        })

    # ── 事件日志 handler ──

    @staticmethod
    def _event_to_dict(event) -> dict:
        return {
            "id": event.id,
            "origin_machine": event.origin_machine,
            "origin_seq": event.origin_seq,
            "ts": event.ts,
            "level": event.level,
            "category": event.category,
            "message": event.message,
            "bot": event.bot,
            "meta": event.meta,
            "read_at": event.read_at,
        }

    def _events_query_args(self, request: Request) -> dict:
        q = request.query_params
        kwargs: dict = {}
        if q.get("bot"):
            kwargs["bot"] = q["bot"]
        if q.get("levels"):
            kwargs["levels"] = [s for s in q["levels"].split(",") if s]
        if q.get("machines"):
            kwargs["machines"] = [s for s in q["machines"].split(",") if s]
        if q.get("category_prefix"):
            kwargs["category_prefix"] = q["category_prefix"]
        if q.get("since"):
            try:
                kwargs["since"] = float(q["since"])
            except ValueError:
                pass
        if q.get("until"):
            try:
                kwargs["until"] = float(q["until"])
            except ValueError:
                pass
        if q.get("search"):
            kwargs["search"] = q["search"]
        if q.get("unread_only") in ("1", "true"):
            kwargs["unread_only"] = True
        if q.get("limit"):
            try:
                kwargs["limit"] = max(1, min(int(q["limit"]), 1000))
            except ValueError:
                pass
        if q.get("before_id"):
            try:
                kwargs["before_id"] = int(q["before_id"])
            except ValueError:
                pass
        return kwargs

    async def _handle_events_query(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        if self.event_bus is None:
            return JSONResponse({"ok": True, "events": []})
        kwargs = self._events_query_args(request)
        kwargs.setdefault("limit", 200)
        events = self.event_bus._store.query(**kwargs)
        next_cursor = events[-1].id if len(events) >= kwargs["limit"] else None
        return JSONResponse({
            "ok": True,
            "events": [self._event_to_dict(e) for e in events],
            "next_cursor": next_cursor,
        })

    async def _handle_events_categories(self, request: Request) -> Response:
        """给树状导航用的去重 category 列表。"""
        if not self._authorized(request):
            return self._unauthorized()
        if self.event_bus is None:
            return JSONResponse({"ok": True, "categories": []})
        cursor = self.event_bus._store._conn.execute(
            "SELECT category, COUNT(*) FROM events GROUP BY category ORDER BY category"
        )
        rows = [{"category": r[0], "count": r[1]} for r in cursor.fetchall()]
        return JSONResponse({"ok": True, "categories": rows})

    async def _handle_events_machines(self, request: Request) -> Response:
        """事件历史中出现过的去重 origin_machine（含离线节点）。"""
        if not self._authorized(request):
            return self._unauthorized()
        if self.event_bus is None:
            return JSONResponse({"ok": True, "machines": []})
        cursor = self.event_bus._store._conn.execute(
            "SELECT origin_machine, COUNT(*) FROM events GROUP BY origin_machine ORDER BY origin_machine"
        )
        rows = [{"machine_id": r[0], "count": r[1]} for r in cursor.fetchall() if r[0]]
        return JSONResponse({"ok": True, "machines": rows})

    async def _handle_events_mark_read(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        if self.event_bus is None:
            return JSONResponse({"ok": True, "updated": 0})
        try:
            event_id = int(request.path_params["event_id"])
        except (KeyError, ValueError):
            return JSONResponse({"ok": False, "error": "bad event_id"}, status_code=400)
        n = self.event_bus._store.mark_read([event_id])
        return JSONResponse({"ok": True, "updated": n})

    async def _handle_events_read_all(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        if self.event_bus is None:
            return JSONResponse({"ok": True, "updated": 0})
        try:
            body = await request.json()
        except Exception:
            body = {}
        kwargs = {}
        if isinstance(body, dict):
            for k in ("bot", "levels", "machines", "category_prefix", "since", "until"):
                if k in body:
                    kwargs[k] = body[k]
        kwargs["unread_only"] = True
        events = self.event_bus._store.query(**kwargs, limit=10000)
        ids = [e.id for e in events if e.id is not None]
        n = self.event_bus._store.mark_read(ids)
        return JSONResponse({"ok": True, "updated": n})

    async def _handle_logs_query(self, request: Request) -> Response:
        """Web UI Logs 页用的 <local-dir>/boxagent.log 尾部。

        query 参数：
          machine — 目标 machine_id（非本机则经 cluster RPC 转发）
          limit   — 最多返回条数（默认 200，上限 2000）
          offset  — 从末尾跳过的条数（分页用，默认 0）
          levels  — 逗号分隔的 level 过滤（大小写不敏感）
          grep    — 大小写不敏感的子串过滤
        """
        if not self._authorized(request):
            return self._unauthorized()
        machine = request.query_params.get("machine", "").strip()
        if machine:
            response = await self.cluster_rpc.dispatch_machine_request(machine, "GET", "/api/logs", request)
            if response is not None:
                return response
        from boxagent.transports.web.log_file import read_tail
        try:
            limit = max(1, min(2000, int(request.query_params.get("limit", "200"))))
        except ValueError:
            limit = 200
        try:
            offset = max(0, int(request.query_params.get("offset", "0")))
        except ValueError:
            offset = 0
        levels_raw = request.query_params.get("levels", "").strip()
        levels = [s.strip() for s in levels_raw.split(",") if s.strip()] if levels_raw else None
        grep_pattern = request.query_params.get("grep", "").strip() or None
        log_file = getattr(self.config, "log_file", None)
        if not log_file:
            return JSONResponse({"ok": True, "lines": [], "has_more": False, "log_file": None})
        result = read_tail(Path(log_file), limit=limit, offset=offset, levels=levels, grep=grep_pattern)
        return JSONResponse({
            "ok": True,
            "lines": result["lines"],
            "has_more": result["has_more"],
            "log_file": str(log_file),
        })

    async def _handle_events_stream(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        from boxagent.events.web_stream import EventStreamSubscriber

        event_bus = self.event_bus
        query_params = request.query_params

        async def event_generator():
            yield b"event: ready\ndata: {}\n\n"
            if event_bus is None:
                return
            sub = EventStreamSubscriber(
                bus=event_bus,
                levels=([s for s in query_params["levels"].split(",") if s] if query_params.get("levels") else None),
                machines=([s for s in query_params["machines"].split(",") if s] if query_params.get("machines") else None),
                bot=query_params.get("bot") or None,
                category_prefix=query_params.get("category_prefix") or None,
            )
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(sub.queue.get(), timeout=15.0)
                        payload = json.dumps(self._event_to_dict(event), ensure_ascii=False)
                        yield f"event: event\ndata: {payload}\n\n".encode("utf-8")
                    except asyncio.TimeoutError:
                        yield b": ping\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                sub.close()

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ── Schedules ──

    async def _handle_schedules_list(self, request: Request) -> Response:
        """GET /api/schedules?machine= — 列出 schedules.yaml 条目。"""
        if not self._authorized(request):
            return self._unauthorized()
        machine = request.query_params.get("machine", "")
        if machine:
            response = await self.cluster_rpc.dispatch_machine_request(
                machine, "GET", "/api/schedules", request,
            )
            if response is not None:
                return response
        from boxagent.scheduler.engine import load_schedule_entries
        from boxagent.config import node_matches
        path = self.config_dir / "schedules.yaml"
        try:
            entries = load_schedule_entries(path, node_id=self.config.node_id)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        items = []
        for task_id, entry in entries.items():
            if not node_matches(entry.get("enabled_on_nodes", ""), self.config.node_id):
                continue
            items.append({
                "id": task_id,
                "cron": entry.get("cron", ""),
                "mode": entry.get("mode", "isolate"),
                "enabled": bool(entry.get("enabled", True)),
                "ai_backend": entry.get("ai_backend", ""),
                "model": entry.get("model", ""),
                "bot": entry.get("bot", ""),
                "prompt": entry.get("prompt", ""),
                "enabled_on_nodes": entry.get("enabled_on_nodes", ""),
            })
        return JSONResponse({"ok": True, "schedules": items, "node_id": self.config.node_id})

    async def _handle_schedules_runs(self, request: Request) -> Response:
        """GET /api/schedules/runs?task=&machine=&limit= — 列出运行记录。"""
        if not self._authorized(request):
            return self._unauthorized()
        machine = request.query_params.get("machine", "")
        if machine:
            response = await self.cluster_rpc.dispatch_machine_request(
                machine, "GET", "/api/schedules/runs", request,
            )
            if response is not None:
                return response
        from boxagent.scheduler.cli import load_run_logs
        task_id = request.query_params.get("task", "")
        try:
            limit = max(1, int(request.query_params.get("limit", "50")))
        except ValueError:
            limit = 50
        try:
            offset = max(0, int(request.query_params.get("offset", "0")))
        except ValueError:
            offset = 0
        all_entries = load_run_logs(self.local_dir, task_id=task_id)
        page = all_entries[offset:offset + limit]
        return JSONResponse({
            "ok": True, "runs": page, "offset": offset,
            "total": len(all_entries), "node_id": self.config.node_id,
        })

    async def _handle_schedules_run_detail(self, request: Request) -> Response:
        """GET /api/schedules/runs/<task>/<index>?machine= — 单条运行记录。"""
        if not self._authorized(request):
            return self._unauthorized()
        machine = request.query_params.get("machine", "")
        if machine:
            response = await self.cluster_rpc.dispatch_machine_request(
                machine, "GET", request.url.path, request,
            )
            if response is not None:
                return response
        from boxagent.scheduler.cli import load_run_logs
        task_id = request.path_params["task_id"]
        try:
            run_index = int(request.path_params["run_index"])
        except ValueError:
            return JSONResponse({"ok": False, "error": "invalid run_index"}, status_code=400)
        entries = load_run_logs(self.local_dir, task_id=task_id)
        if run_index < 1 or run_index > len(entries):
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        return JSONResponse({"ok": True, "run": entries[run_index - 1]})
