"""HTTP API mixin — Web UI endpoints, MCP, and the internal API server."""

import asyncio
import logging
import time
from pathlib import Path

from aiohttp import web

from .core import _infer_platform

logger = logging.getLogger(__name__)


class HttpApiMixin:
    async def _start_http(self) -> None:
        """Start the internal HTTP API server (TCP only)."""
        app = web.Application()
        app.router.add_post("/api/schedule/run", self._handle_schedule_run)
        app.router.add_get("/api/workgroup/specialists", self._handle_list_specialists)
        app.router.add_get("/api/workgroup/specialist_status", self._handle_specialist_status)
        app.router.add_post("/api/workgroup/send", self._handle_workgroup_send)
        app.router.add_post("/api/workgroup/create_specialist", self._handle_create_specialist)
        app.router.add_post("/api/workgroup/reset_specialist", self._handle_reset_specialist)
        app.router.add_post("/api/workgroup/delete_specialist", self._handle_delete_specialist)
        app.router.add_post("/api/workgroup/cancel_task", self._handle_cancel_task)
        app.router.add_post("/api/peer/send", self._handle_peer_send)
        # NOTE: /api/wg/peer/recv lives on `web_app` (the web UI port) instead of
        # `app` (internal API port) because guest_client forwards RPC frames to
        # `127.0.0.1:<local_web_port>` — the web UI port. Registering it here
        # would silently 404 every cross-machine peer message.

        runner = web.AppRunner(app)
        await runner.setup()
        self._http_runner = runner

        self.local_dir.mkdir(parents=True, exist_ok=True)
        self._clear_http_artifacts()

        # Always use TCP (api_port=0 lets the OS pick a free port)
        port = self.config.api_port or 0
        tcp_site = web.TCPSite(runner, "127.0.0.1", port)
        await tcp_site.start()
        sockets = getattr(getattr(tcp_site, "_server", None), "sockets", None) or []
        actual_port = sockets[0].getsockname()[1] if sockets else port
        self._api_port_file.write_text(f"{actual_port}\n", encoding="utf-8")
        logger.info("HTTP API listening on 127.0.0.1:%d", actual_port)

        # Start MCP HTTP server (streamable-http)
        await self._start_mcp_http()

    async def _start_web_http(self) -> None:
        """Start a separate aiohttp server for the /web/* UI on its own port."""
        from pathlib import Path as _Path

        web_app = web.Application()
        web_app.router.add_get("/", self._handle_web_index)
        web_app.router.add_get("/api/bots", self._handle_web_bots)
        web_app.router.add_get("/api/machines", self._handle_web_machines)
        web_app.router.add_get("/api/sessions", self._handle_web_sessions)
        web_app.router.add_post("/api/sessions/set_main", self._handle_set_main_session)
        web_app.router.add_get("/api/version", self._handle_version)
        web_app.router.add_post("/api/admin/restart", self._handle_admin_restart)
        web_app.router.add_post("/api/admin/cluster_restart", self._handle_admin_cluster_restart)
        web_app.router.add_get("/api/history", self._handle_web_history)
        web_app.router.add_post("/api/send", self._handle_web_send)
        web_app.router.add_get("/api/stream", self._handle_web_stream)
        web_app.router.add_get("/api/claude/projects", self._handle_claude_projects)
        web_app.router.add_get("/api/claude/sessions", self._handle_claude_sessions)
        web_app.router.add_get("/api/claude/transcript", self._handle_claude_transcript)
        web_app.router.add_post("/api/claude/resume", self._handle_claude_resume)
        # Cluster RPC inbound: guest_client forwards peer-recv RPCs to the web
        # UI port (see _start_http for why this lives here, not on `app`).
        web_app.router.add_post("/api/wg/peer/recv", self._handle_wg_peer_recv)
        # /api/peer/send also exposed on web_app so sats can forward
        # cross-node send_to_peer calls back to host via devtunnel
        # (guest_client.fetch_host_json hits web_app, not app).
        web_app.router.add_post("/api/peer/send", self._handle_peer_send)
        # Hub-and-spoke: /api/guest/ws is always registered. The handler
        # delegates to the GuestRegistry currently owned by the role manager
        # (only present when this node is the active host). Non-host nodes
        # respond with 503 so the dialing peer reconnects elsewhere.
        web_app.router.add_get("/api/guest/ws", self._handle_guest_ws)
        web_static = _Path(__file__).parent.parent / "web" / "static"
        if web_static.is_dir():
            web_app.router.add_static("/", path=str(web_static), show_index=False)

        runner = web.AppRunner(web_app)
        await runner.setup()
        self._web_runner = runner

        host = self.config.web_host or "127.0.0.1"
        port = self.config.web_port if self.config.web_port is not None else 9292
        site = web.TCPSite(runner, host, port)
        await site.start()
        sockets = getattr(getattr(site, "_server", None), "sockets", None) or []
        actual_port = sockets[0].getsockname()[1] if sockets else port
        self._web_port_file.write_text(f"{actual_port}\n", encoding="utf-8")
        logger.info("Web UI listening on %s:%d", host, actual_port)

    async def _stop_web_http(self) -> None:
        runner = getattr(self, "_web_runner", None)
        if runner:
            await runner.cleanup()
            self._web_runner = None
        self._web_port_file.unlink(missing_ok=True)

    async def _stop_http(self) -> None:
        """Stop the HTTP API server."""
        if self._http_runner:
            await self._http_runner.cleanup()
            self._http_runner = None
        self._api_port_file.unlink(missing_ok=True)

    def _pick_mcp_port(self) -> int:
        """Pick an MCP port. Preference order: configured > previous > 9390+."""
        import socket

        def _free(p: int) -> bool:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.bind(("127.0.0.1", p))
                    return True
                except OSError:
                    return False

        configured = getattr(self.config, "mcp_port", 0) or 0
        if configured:
            return configured  # explicit config wins; let uvicorn fail loudly if busy

        candidates: list[int] = []
        if self._mcp_port_file.exists():
            try:
                prev = int(self._mcp_port_file.read_text(encoding="utf-8").strip())
                if prev > 0:
                    candidates.append(prev)
            except Exception:
                pass
        for p in range(9390, 9500):
            if p not in candidates:
                candidates.append(p)

        for p in candidates:
            if _free(p):
                return p
        return 0  # fall back to OS-assigned

    async def _start_mcp_http(self) -> None:
        """Start the MCP streamable-http server (uvicorn)."""
        try:
            import uvicorn
            from boxagent.transports.mcp.server import create_mcp_app

            starlette_app = create_mcp_app(
                config_dir=str(self.config_dir),
                local_dir=str(self.local_dir),
                node_id=self.config.node_id,
                gateway=self,
            )
            mcp_port = self._pick_mcp_port()
            config = uvicorn.Config(
                starlette_app,
                host="127.0.0.1",
                port=mcp_port,
                log_level="warning",
            )
            server = uvicorn.Server(config)
            self._mcp_server = server
            self._mcp_task = asyncio.create_task(server.serve())

            # Wait for server to start and discover actual port
            while not server.started:
                await asyncio.sleep(0.05)

            actual_port = server.servers[0].sockets[0].getsockname()[1]
            self._mcp_port_file.write_text(f"{actual_port}\n", encoding="utf-8")
            logger.info("MCP HTTP server listening on 127.0.0.1:%d", actual_port)
        except Exception as e:
            logger.error("Failed to start MCP HTTP server: %s", e)
            self._mcp_server = None
            self._mcp_task = None

    async def _stop_mcp_http(self) -> None:
        """Stop the MCP HTTP server."""
        if getattr(self, "_mcp_server", None):
            self._mcp_server.should_exit = True
        if getattr(self, "_mcp_task", None):
            try:
                await self._mcp_task
            except Exception:
                pass
            self._mcp_task = None
        self._mcp_server = None
        self._mcp_port_file.unlink(missing_ok=True)

    # ── Web chat handlers ──

    def _web_authorized(self, request: web.Request) -> bool:
        """Allow localhost, trusted-header (tunnel), or matching bearer/query token."""
        token = (self.config.web_token or "").strip()
        # Localhost / loopback always allowed
        peer = request.transport.get_extra_info("peername") if request.transport else None
        host = (peer[0] if peer else request.remote) or ""
        if host in ("127.0.0.1", "::1", "localhost"):
            return True
        # Trusted header (set by tunnel/reverse proxy)
        trust_hdr = (self.config.web_trust_header or "").strip()
        if trust_hdr and request.headers.get(trust_hdr):
            return True
        # No token configured AND no localhost → deny rather than wide-open
        if not token:
            return False
        # Authorization: Bearer ...
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:].strip() == token:
            return True
        # ?token=... (for EventSource which can't set headers)
        if request.query.get("token", "") == token:
            return True
        return False

    def _web_unauthorized(self) -> web.Response:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    async def _handle_web_index(self, request: web.Request) -> web.Response:
        # Always serve the index page so users can paste ?token=... to log in.
        from pathlib import Path as _Path
        index = _Path(__file__).parent.parent / "web" / "static" / "index.html"
        if not index.is_file():
            return web.Response(text="web UI not installed", status=404)
        return web.Response(body=index.read_bytes(), content_type="text/html")

    async def _handle_web_bots(self, request: web.Request) -> web.Response:
        if not self._web_authorized(request):
            return self._web_unauthorized()
        bots = []
        local_mid = self._local_machine_id()
        for name, ch in self._web_channels.items():
            cfg = self.config.bots.get(name)
            workgroup = self.config.workgroups.get(name)
            if cfg is not None:
                bots.append({
                    "name": name,
                    "display_name": cfg.display_name or name,
                    "backend": cfg.ai_backend,
                    "model": cfg.model,
                    "kind": "bot",
                    "machine": local_mid,
                })
            elif workgroup is not None:
                bots.append({
                    "name": name,
                    "display_name": (workgroup.display_name or name) + "  (workgroup)",
                    "backend": workgroup.ai_backend,
                    "model": workgroup.model,
                    "kind": "workgroup",
                    "machine": local_mid,
                })
        # Federate: include bots from connected guests (host role) or
        # from the cached cluster snapshot pushed by host (guest role).
        if self.guest_registry is not None:
            for mid, b in self.guest_registry.list_bots():
                bots.append({
                    "name": b.name,
                    "display_name": (b.display_name or b.name) + f"  @{mid}",
                    "backend": b.backend,
                    "model": b.model,
                    "kind": b.kind,
                    "machine": mid,
                })
        elif self.guest_client is not None:
            for m in self.guest_client.remote_machines:
                mid = m.get("machine_id") or ""
                if not mid or mid == local_mid:
                    continue
                for b in m.get("bots") or []:
                    bots.append({
                        "name": b.get("name") or "",
                        "display_name": (b.get("display_name") or b.get("name") or "") + f"  @{mid}",
                        "backend": b.get("backend") or "",
                        "model": b.get("model") or "",
                        "kind": b.get("kind") or "bot",
                        "machine": mid,
                    })
        return web.json_response({"bots": bots})

    async def _handle_web_machines(self, request: web.Request) -> web.Response:
        """Return all known machines (self + connected/disconnected guests)
        so the UI can render a grouped sidebar with online/offline status."""
        if not self._web_authorized(request):
            return self._web_unauthorized()
        if self.guest_registry is not None:
            return web.json_response({"machines": self._collect_machines()})
        # Guest role: render local machine + cached snapshot from host.
        local_mid = self._local_machine_id()
        local_role = self._local_role()
        machines: list[dict] = [{
            "machine_id": local_mid,
            "online": True,
            "role": local_role,
            "self": True,
            "bots": self._local_bot_descriptors(),
            "last_seen": time.time(),
        }]
        if self.guest_client is not None:
            for m in self.guest_client.remote_machines:
                if m.get("machine_id") == local_mid:
                    continue
                m = dict(m)
                m["self"] = False
                machines.append(m)
        return web.json_response({"machines": machines})

    async def _handle_web_sessions(self, request: web.Request) -> web.Response:
        """List every persisted chat session for a bot, across all channels."""
        if not self._web_authorized(request):
            return self._web_unauthorized()
        bot = request.query.get("bot", "")
        machine = request.query.get("machine", "")
        if not bot or not machine:
            return web.json_response({"ok": False, "error": "missing bot/machine"}, status=400)
        # Remote? proxy via host (guest role) or to the owning guest (host role).
        resp = await self._dispatch_machine_request(machine, "GET", "/api/sessions", request)
        if resp is not None:
            return resp
        if bot not in self._web_channels:
            return web.json_response({"ok": False, "error": "bot not web-enabled"}, status=404)
        if not self._storage:
            return web.json_response({"ok": True, "sessions": []})

        sessions = self._storage.list_chat_sessions(bot)

        main_chat_id = self._storage.get_main_chat_id(bot)

        # Build claude-native index for claude-cli sessions
        claude_session_info: dict[str, dict] = {}
        bot_cfg = self.config.bots.get(bot)
        wg_cfg = self.config.workgroups.get(bot)
        backend = (bot_cfg.ai_backend if bot_cfg else None) or (wg_cfg.ai_backend if wg_cfg else "claude-cli")
        if backend == "claude-cli":
            try:
                from boxagent.sessions import claude_native
                base = claude_native.default_claude_projects_dir()
                if base.is_dir():
                    for proj in base.iterdir():
                        if not proj.is_dir():
                            continue
                        for f in proj.iterdir():
                            if f.suffix == ".jsonl":
                                try:
                                    stat = f.stat()
                                    claude_session_info[f.stem] = {
                                        "size": stat.st_size,
                                        "mtime": stat.st_mtime,
                                    }
                                except OSError:
                                    pass
            except Exception:
                pass

        for s in sessions:
            sid = s.get("session_id") or ""
            s["platform"] = _infer_platform(s["chat_id"])
            s["is_main"] = bool(main_chat_id and s["chat_id"] == main_chat_id)
            s["preview"] = ""
            s["last_ts"] = 0
            s["message_count"] = 0
            if not sid:
                continue

            cached = self._session_meta_cache.get(sid)

            # Try claude-native first
            ci = claude_session_info.get(sid)
            if ci:
                if cached and cached.get("mtime") == ci["mtime"]:
                    s["preview"] = cached.get("preview", "")
                    s["last_ts"] = cached.get("last_ts", 0)
                    s["message_count"] = cached.get("message_count", 0)
                    continue
                from boxagent.sessions import claude_native
                base = claude_native.default_claude_projects_dir()
                for proj in base.iterdir():
                    f = proj / f"{sid}.jsonl"
                    if f.is_file():
                        sess_list = claude_native.list_sessions(proj.name)
                        for sl in sess_list:
                            if sl.get("session_id") == sid:
                                s["message_count"] = sl.get("message_count", 0)
                                s["last_ts"] = sl.get("last_ts", 0)
                                preview = (sl.get("first_user") or "").strip().replace("\n", " ")
                                s["preview"] = preview[:90] + ("..." if len(preview) > 90 else "")
                                break
                        self._session_meta_cache[sid] = {
                            "mtime": ci["mtime"],
                            "preview": s["preview"],
                            "last_ts": s["last_ts"],
                            "message_count": s["message_count"],
                        }
                        break
                continue

            # Transcript-based sessions
            tpath = self._storage.local_dir / "transcripts" / f"{sid}.jsonl"
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
                msg_count = 0
                import json as _json
                for line in tpath.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = _json.loads(line)
                    except Exception:
                        continue
                    if rec.get("chat_id") and rec.get("chat_id") != s["chat_id"]:
                        continue
                    ev = rec.get("event")
                    txt = rec.get("text", "") or ""
                    ts = float(rec.get("ts", 0) or 0)
                    if ts > last_ts:
                        last_ts = ts
                    if ev == "user":
                        last_user = txt
                        msg_count += 1
                    elif ev == "assistant":
                        last_assist = txt
                        msg_count += 1
                preview = (last_assist or last_user or "").strip().replace("\n", " ")
                s["preview"] = preview[:90] + ("..." if len(preview) > 90 else "")
                s["last_ts"] = last_ts
                s["message_count"] = msg_count
                self._session_meta_cache[sid] = {
                    "mtime": tstat.st_mtime,
                    "preview": s["preview"],
                    "last_ts": s["last_ts"],
                    "message_count": msg_count,
                }
            except Exception as e:
                logger.debug("session preview read failed for %s: %s", sid, e)

        sessions.sort(key=lambda x: x.get("last_ts") or 0, reverse=True)
        return web.json_response({"ok": True, "sessions": sessions})

    async def _handle_set_main_session(self, request: web.Request) -> web.Response:
        """POST /api/sessions/set_main {bot, machine, chat_id} — pin main chat_id.

        Empty chat_id clears the pin. Remote machines proxy to the owning guest.
        """
        if not self._web_authorized(request):
            return self._web_unauthorized()
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        bot = str(data.get("bot") or "").strip()
        machine = str(data.get("machine") or "").strip()
        chat_id = str(data.get("chat_id") or "").strip()
        if not bot or not machine:
            return web.json_response({"ok": False, "error": "missing bot/machine"}, status=400)
        if machine != self._local_machine_id():
            resp = await self._dispatch_machine_request(
                machine, "POST", "/api/sessions/set_main", request, body=data,
            )
            if resp is not None:
                return resp
        if self._storage is None:
            return web.json_response({"ok": False, "error": "no storage"}, status=500)
        self._storage.set_main_chat_id(bot, chat_id)
        return web.json_response({"ok": True, "main_chat_id": chat_id})

    async def _handle_version(self, request: web.Request) -> web.Response:
        """GET /api/version — return this node's version, optionally aggregated.

        Without ``?cluster=1``: just this process's commit/version.
        With ``?cluster=1`` (host only): also queries every connected guest via
        cluster RPC and returns ``{self, sats: {machine_id: ...}}``.
        """
        if not self._web_authorized(request):
            return self._web_unauthorized()
        from boxagent._version import __version__, _git_commit, version_string

        local = {
            "machine_id": self._local_machine_id(),
            "version": __version__,
            "commit": _git_commit(),
            "version_string": version_string(),
        }
        if request.query.get("cluster") not in ("1", "true", "yes"):
            return web.json_response({"ok": True, **local})
        # Host mode: ask each connected guest via cluster RPC.
        if self.guest_registry is not None:
            sats: dict[str, object] = {}
            for machine_id, sess in list(self.guest_registry.sessions.items()):
                try:
                    result = await sess.call("GET", "/api/version", timeout=5.0)
                    sats[machine_id] = result.get("body") or {"error": "no body"}
                except Exception as e:
                    sats[machine_id] = {"error": str(e)}
            return web.json_response({"ok": True, "self": local, "sats": sats})
        # Guest mode: ask host via tunnel HTTP, merge.
        if self.guest_client is not None:
            try:
                host_result = await self.guest_client.fetch_host_json("/api/version", {"cluster": "1"})
            except Exception as e:
                host_result = {"error": str(e)}
            return web.json_response({"ok": True, "self": local, "host": host_result})
        # Standalone (no cluster): same shape, empty.
        return web.json_response({"ok": True, "self": local, "sats": {}})

    async def _handle_admin_restart(self, request: web.Request) -> web.Response:
        """POST /api/admin/restart — gracefully exit; supervisor (easy-service)
        is expected to restart the process. Sends SIGTERM to ourselves after
        a short delay so the HTTP response can flush first.
        """
        if not self._web_authorized(request):
            return self._web_unauthorized()
        import os
        import signal as _signal
        loop = asyncio.get_event_loop()
        loop.call_later(0.2, lambda: os.kill(os.getpid(), _signal.SIGTERM))
        return web.json_response({
            "ok": True, "restarting": self._local_machine_id(),
            "note": "SIGTERM scheduled in 0.2s; supervisor must relaunch",
        })

    async def _handle_admin_cluster_restart(self, request: web.Request) -> web.Response:
        """POST /api/admin/cluster_restart — restart guest nodes (and self if asked).

        Body / query options:
          - ``machines: [id, ...]`` — only restart these sats (default: all)
          - ``include_self=1`` — also SIGTERM this host process (deferred 1s)
        """
        if not self._web_authorized(request):
            return self._web_unauthorized()
        if self.guest_registry is None:
            return web.json_response(
                {"ok": False, "error": "not in host mode"}, status=400,
            )
        include_self = request.query.get("include_self") in ("1", "true", "yes")
        target_filter: list[str] | None = None
        try:
            data = await request.json()
            if not include_self:
                include_self = bool(data.get("include_self"))
            raw = data.get("machines")
            if isinstance(raw, list) and raw:
                target_filter = [str(m) for m in raw]
        except Exception:
            pass
        results: dict[str, object] = {}
        for machine_id, sess in list(self.guest_registry.sessions.items()):
            if target_filter is not None and machine_id not in target_filter:
                continue
            try:
                rpc = await sess.call("POST", "/api/admin/restart", timeout=5.0)
                results[machine_id] = rpc.get("body") or {"status": rpc.get("status")}
            except Exception as e:
                results[machine_id] = {"error": str(e)}
        if include_self and (target_filter is None or self._local_machine_id() in target_filter):
            import os
            import signal as _signal
            asyncio.get_event_loop().call_later(
                1.0, lambda: os.kill(os.getpid(), _signal.SIGTERM),
            )
            results[self._local_machine_id()] = {
                "scheduled": True, "delay_seconds": 1.0,
            }
        return web.json_response({"ok": True, "results": results})

    async def _handle_web_history(self, request: web.Request) -> web.Response:
        if not self._web_authorized(request):
            return self._web_unauthorized()
        bot = request.query.get("bot", "")
        chat_id = request.query.get("chat_id", "")
        machine = request.query.get("machine", "")
        if not bot or not chat_id or not machine:
            return web.json_response({"ok": False, "error": "missing bot/chat_id/machine"}, status=400)
        if machine != self._local_machine_id():
            resp = await self._dispatch_machine_request(machine, "GET", "/api/history", request)
            if resp is not None:
                return resp
        if bot not in self._web_channels:
            return web.json_response({"ok": False, "error": "bot not web-enabled"}, status=404)

        history: list[dict] = []
        if self._storage:
            saved = self._storage.load_session(bot, chat_id)
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

            # Claude-native: any session whose stored backend is claude-cli has
            # a ~/.claude/projects/<encoded>/<sid>.jsonl with full tool_use /
            # tool_result blocks — use that for the richest history (text +
            # tool cards). Independent of chat_id shape (Telegram digits /
            # web-uuid / wg:specialist all included).
            if saved_backend == "claude-cli" and sids:
                from boxagent.sessions import claude_native
                base = claude_native.default_claude_projects_dir()
                if base.is_dir():
                    proj_index: dict[str, str] = {}  # session_id → encoded_project
                    for proj in base.iterdir():
                        if not proj.is_dir():
                            continue
                        for f in proj.iterdir():
                            if f.suffix == ".jsonl":
                                proj_index[f.stem] = proj.name
                    for sid in sids:
                        encoded = proj_index.get(sid)
                        if encoded:
                            history.extend(claude_native.read_messages(encoded, sid))
                history.sort(key=lambda r: r.get("ts") or 0)
                total = len(history)
                limit = int(request.query.get("limit", 0) or 0)
                offset = int(request.query.get("offset", 0) or 0)
                if limit > 0:
                    history = history[-(offset + limit):len(history) - offset if offset else None]
                return web.json_response({"ok": True, "total": total, "history": history})

            # Regular bot transcripts — concat per-sid jsonl files in chain
            import json as _json
            for sid in sids:
                tpath = self._storage.local_dir / "transcripts" / f"{sid}.jsonl"
                if not tpath.is_file():
                    continue
                try:
                    for line in tpath.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = _json.loads(line)
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
        limit = int(request.query.get("limit", 0) or 0)
        offset = int(request.query.get("offset", 0) or 0)
        if limit > 0:
            history = history[-(offset + limit):len(history) - offset if offset else None]
        return web.json_response({"ok": True, "total": total, "history": history})

    async def _handle_web_send(self, request: web.Request) -> web.Response:
        if not self._web_authorized(request):
            return self._web_unauthorized()
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        bot = body.get("bot", "")
        chat_id = body.get("chat_id", "")
        text = body.get("text", "")
        machine = body.get("machine", "")
        if not bot or not chat_id or not text or not machine:
            return web.json_response({"ok": False, "error": "missing bot/chat_id/text/machine"}, status=400)
        if machine != self._local_machine_id():
            resp = await self._dispatch_machine_request(machine, "POST", "/api/send", request, body=body)
            if resp is not None:
                return resp
        ch = self._web_channels.get(bot)
        if ch is None:
            return web.json_response({"ok": False, "error": "bot not web-enabled"}, status=404)
        try:
            await ch.inject(chat_id=chat_id, text=text, user_id="web")
        except Exception as e:
            logger.exception("web send failed")
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        return web.json_response({"ok": True})

    async def _handle_web_stream(self, request: web.Request) -> web.StreamResponse:
        if not self._web_authorized(request):
            return self._web_unauthorized()
        bot = request.query.get("bot", "")
        chat_id = request.query.get("chat_id", "")
        machine = request.query.get("machine", "")
        if not bot or not chat_id or not machine:
            return web.json_response({"ok": False, "error": "missing bot/chat_id/machine"}, status=400)
        if machine != self._local_machine_id():
            resp = await self._dispatch_machine_stream(machine, "/api/stream", request)
            if resp is not None:
                return resp
        ch = self._web_channels.get(bot)
        if ch is None:
            return web.json_response({"ok": False, "error": "bot not web-enabled"}, status=404)

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)
        queue = ch.subscribe(chat_id)
        # Initial hello to flush headers on some proxies
        await resp.write(b": connected\n\n")
        import json as _json
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
                    continue
                if event.get("type") == "_close":
                    break
                payload = _json.dumps(event, ensure_ascii=False)
                await resp.write(f"data: {payload}\n\n".encode("utf-8"))
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            ch.unsubscribe(chat_id, queue)
        return resp

    # ── Claude native session picker ──

    async def _handle_claude_projects(self, request: web.Request) -> web.Response:
        if not self._web_authorized(request):
            return self._web_unauthorized()
        machine = request.query.get("machine", "")
        if not machine:
            return web.json_response({"ok": False, "error": "missing machine"}, status=400)
        if machine != self._local_machine_id():
            resp = await self._dispatch_machine_request(machine, "GET", "/api/claude/projects", request)
            if resp is not None:
                return resp
        from boxagent.sessions import claude_native
        projects = await asyncio.to_thread(claude_native.list_projects)
        return web.json_response({"ok": True, "projects": projects})

    async def _handle_claude_sessions(self, request: web.Request) -> web.Response:
        if not self._web_authorized(request):
            return self._web_unauthorized()
        encoded = request.query.get("project", "")
        machine = request.query.get("machine", "")
        if not encoded or not machine:
            return web.json_response({"ok": False, "error": "missing project/machine"}, status=400)
        if machine != self._local_machine_id():
            resp = await self._dispatch_machine_request(machine, "GET", "/api/claude/sessions", request)
            if resp is not None:
                return resp
        from boxagent.sessions import claude_native
        sessions = await asyncio.to_thread(claude_native.list_sessions, encoded)
        return web.json_response({
            "ok": True,
            "sessions": sessions,
        })

    async def _handle_claude_transcript(self, request: web.Request) -> web.Response:
        if not self._web_authorized(request):
            return self._web_unauthorized()
        encoded = request.query.get("project", "")
        sid = request.query.get("session_id", "")
        machine = request.query.get("machine", "")
        if not encoded or not sid or not machine:
            return web.json_response({"ok": False, "error": "missing project/session_id/machine"}, status=400)
        if machine != self._local_machine_id():
            resp = await self._dispatch_machine_request(machine, "GET", "/api/claude/transcript", request)
            if resp is not None:
                return resp
        from boxagent.sessions import claude_native
        messages = await asyncio.to_thread(claude_native.read_messages, encoded, sid)
        return web.json_response({
            "ok": True,
            "messages": messages,
        })

    async def _handle_claude_resume(self, request: web.Request) -> web.Response:
        """Persist the chosen native session_id under a synthetic chat_id so the
        next message in that chat_id resumes it via the appropriate backend.

        Two modes:
        - bot=<real bot>: legacy — binds to a real configured bot, chat_id is
          ``claude-<sid>``, backend is the bot's configured backend.
        - bot="raw": passthrough — chat_id is ``<backend>-<sid>``, backend is
          chosen by the caller (claude-cli / codex-cli / codex-acp).
        """
        if not self._web_authorized(request):
            return self._web_unauthorized()
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        bot = body.get("bot", "")
        sid = body.get("session_id", "")
        encoded = body.get("project", "")
        machine = body.get("machine", "")
        backend_override = body.get("backend", "")  # raw mode only
        if not bot or not sid or not machine:
            return web.json_response({"ok": False, "error": "missing bot/session_id/machine"}, status=400)
        if machine != self._local_machine_id():
            resp = await self._dispatch_machine_request(machine, "POST", "/api/claude/resume", request, body=body)
            if resp is not None:
                return resp
        if bot not in self._web_channels:
            return web.json_response({"ok": False, "error": "bot not web-enabled"}, status=404)

        is_raw = bot == "raw"
        workspace = ""
        if self._storage:
            cfg = self.config.bots.get(bot)
            workgroup = self.config.workgroups.get(bot)
            model = (cfg.model if cfg else None) or (workgroup.model if workgroup else "")
            if is_raw:
                backend = backend_override or "claude-cli"
            else:
                backend = (cfg.ai_backend if cfg else None) or (workgroup.ai_backend if workgroup else "claude-cli")
            from boxagent.sessions import claude_native
            workspace = (await asyncio.to_thread(claude_native.project_cwd, encoded) if encoded else "") or (
                cfg.workspace if cfg else (workgroup.admin_workspace if workgroup else "")
            )
            chat_id = f"{backend.split('-')[0]}-{sid}" if is_raw else f"claude-{sid}"
            self._storage.save_session(
                bot, sid,
                preview="(resumed via web)",
                backend=backend,
                chat_id=chat_id,
                model=model,
                workspace=workspace,
            )
            pool = self._pools.get(bot)
            if pool is None and self._workgroup_mgr is not None:
                pool = self._workgroup_mgr.pools.get(bot)
            if pool is not None:
                if workspace:
                    pool.set_workspace(chat_id, workspace)
                pool.set_session_id(chat_id, sid)
                if is_raw and hasattr(pool, "set_backend"):
                    pool.set_backend(chat_id, backend)
        else:
            chat_id = f"claude-{sid}"
        return web.json_response({
            "ok": True,
            "chat_id": chat_id,
            "session_id": sid,
            "project": encoded,
            "backend": backend if self._storage else "",
            "workspace": workspace if self._storage else "",
        })
