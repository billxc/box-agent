"""Web transport — aiohttp server, auth, and route handlers for the Web UI.

Composition class. Held by Gateway as ``self._web_server``. Built in one
phase with config + storage + shared dicts + topology + cluster_rpc +
cluster_routes (all Phase-1 siblings).

The host's *web* port (default 9292) also carries the cluster guest WS
route (``/api/guest/ws``); it is mounted by ``ClusterHttpRoutes.register``
on the same aiohttp app, since the rest of the cluster code already
targets that port.
"""

import asyncio
import logging
import time
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)


def _project_to_dict(p) -> dict:
    """Serialise a ``boxagent.history.ProjectInfo`` for the Web UI.

    Frontend expects ``encoded`` (legacy field name) — we round-trip the
    project_id through it so the resume picker can hand it back to
    ``/api/claude/sessions``.
    """
    return {
        "encoded": p.project_id,
        "label": p.label,
        "cwd": p.cwd,
        "session_count": p.session_count,
        "last_ts": p.last_ts,
    }


def _session_info_to_dict(s) -> dict:
    """Serialise a ``boxagent.history.SessionInfo`` for the Web UI."""
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
    """Serialise a ``boxagent.history.Message`` to the legacy record shape
    consumed by the web UI's transcript replay."""
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
        # Internal state — process-local preview cache (sid → {mtime, ...})
        # used by /api/sessions to avoid re-reading transcript JSONL every poll.
        self.session_meta_cache: dict = {}
        self._runner: web.AppRunner | None = None

    def set_event_bus(self, event_bus) -> None:
        self.event_bus = event_bus

    @property
    def web_port_file(self) -> Path:
        return self.local_dir / "web-port.txt"

    # ── Lifecycle ──

    async def start(self) -> None:
        """Build and start the Web UI aiohttp server (own port)."""
        from boxagent.web_error_middleware import error_logging_middleware
        web_app = web.Application(middlewares=[error_logging_middleware])

        # Web UI / API routes
        self._register_routes(web_app)

        # Hook for non-web routes that share this port (cluster RPC etc.)
        if self.cluster_routes is not None:
            self.cluster_routes.register(web_app)

        # Static files last so the catch-all doesn't shadow API routes
        web_static = Path(__file__).parent / "static"
        if web_static.is_dir():
            web_app.router.add_static("/", path=str(web_static), show_index=False)

        runner = web.AppRunner(web_app)
        await runner.setup()
        self._runner = runner

        host = self.config.web_host or "127.0.0.1"
        port = self.config.web_port if self.config.web_port is not None else 9292
        site = web.TCPSite(runner, host, port)
        await site.start()
        sockets = getattr(getattr(site, "_server", None), "sockets", None) or []
        actual_port = sockets[0].getsockname()[1] if sockets else port
        self.web_port_file.write_text(f"{actual_port}\n", encoding="utf-8")
        logger.info("Web UI listening on %s:%d", host, actual_port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self.web_port_file.unlink(missing_ok=True)

    def _register_routes(self, app: web.Application) -> None:
        app.router.add_get("/", self._handle_web_index)
        app.router.add_get("/api/bots", self._handle_web_bots)
        app.router.add_get("/api/machines", self._handle_web_machines)
        app.router.add_get("/api/sessions", self._handle_web_sessions)
        app.router.add_post("/api/sessions/rename", self._handle_rename_session)
        app.router.add_get("/api/session_info", self._handle_web_session_info)
        app.router.add_get("/api/version", self._handle_version)
        app.router.add_post("/api/admin/restart", self._handle_admin_restart)
        app.router.add_post("/api/admin/cluster_restart", self._handle_admin_cluster_restart)
        app.router.add_get("/api/history", self._handle_web_history)
        app.router.add_post("/api/send", self._handle_web_send)
        app.router.add_get("/api/stream", self._handle_web_stream)
        app.router.add_get("/api/claude/projects", self._handle_claude_projects)
        app.router.add_get("/api/claude/sessions", self._handle_claude_sessions)
        app.router.add_get("/api/claude/transcript", self._handle_claude_transcript)
        app.router.add_post("/api/claude/resume", self._handle_claude_resume)
        # Event log routes (commit #4)
        app.router.add_get("/api/events", self._handle_events_query)
        app.router.add_get("/api/events/stream", self._handle_events_stream)
        app.router.add_get("/api/events/categories", self._handle_events_categories)
        app.router.add_get("/api/events/machines", self._handle_events_machines)
        app.router.add_post("/api/events/{event_id}/read", self._handle_events_mark_read)
        app.router.add_post("/api/events/read_all", self._handle_events_read_all)
        # Raw log file (boxagent.log)
        app.router.add_get("/api/logs", self._handle_logs_query)
        # Schedule run-log routes
        app.router.add_get("/api/schedules", self._handle_schedules_list)
        app.router.add_get("/api/schedules/runs", self._handle_schedules_runs)
        app.router.add_get("/api/schedules/runs/{task_id}/{run_index}", self._handle_schedules_run_detail)

    # ── Auth ──

    def _authorized(self, request: web.Request) -> bool:
        """Allow localhost, trusted-header (tunnel), or matching bearer/query token."""
        token = (self.config.web_token or "").strip()
        peer = request.transport.get_extra_info("peername") if request.transport else None
        host = (peer[0] if peer else request.remote) or ""
        if host in ("127.0.0.1", "::1", "localhost"):
            return True
        trust_hdr = (self.config.web_trust_header or "").strip()
        if trust_hdr and request.headers.get(trust_hdr):
            return True
        if not token:
            return False
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:].strip() == token:
            return True
        if request.query.get("token", "") == token:
            return True
        return False

    def _unauthorized(self) -> web.Response:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    # ── Handlers ──

    async def _handle_web_index(self, request: web.Request) -> web.Response:
        # Always serve the index page so users can paste ?token=... to log in.
        index = Path(__file__).parent / "static" / "index.html"
        if not index.is_file():
            return web.Response(text="web UI not installed", status=404)
        return web.Response(body=index.read_bytes(), content_type="text/html")

    async def _handle_web_bots(self, request: web.Request) -> web.Response:
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
        return web.json_response({"bots": bots})

    async def _handle_web_machines(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        if self.topology.guest_registry is not None:
            return web.json_response({"machines": self.topology.collect_machines()})
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
        return web.json_response({"machines": machines})

    async def _handle_web_sessions(self, request: web.Request) -> web.Response:
        from boxagent.utils import infer_platform
        if not self._authorized(request):
            return self._unauthorized()
        bot = request.query.get("bot", "")
        machine = request.query.get("machine", "")
        if not bot or not machine:
            return web.json_response({"ok": False, "error": "missing bot/machine"}, status=400)
        response = await self.cluster_rpc.dispatch_machine_request(machine, "GET", "/api/sessions", request)
        if response is not None:
            return response
        if bot not in self.web_channels:
            return web.json_response({"ok": False, "error": "bot not web-enabled"}, status=404)
        if not self.storage:
            return web.json_response({"ok": True, "sessions": []})

        sessions = self.storage.list_chat_sessions(bot)

        bot_config = self.config.bots.get(bot)
        backend = (bot_config.ai_backend if bot_config else None) or "claude-cli"
        if backend in ("claude-cli", "agent-sdk-claude"):
            try:
                from boxagent.history import get_history
                history = get_history(backend)
                # SDK gives us session metadata directly per-sid via
                # get_session_info — no need to scan the project tree.
                # We populate claude_session_info lazily in the loop below.
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

            # Try claude-native lookup first (covers claude-cli + agent-sdk-claude).
            if _claude_history is not None:
                info = await _claude_history.get_session_info(sid)
                if info is not None:
                    # Reuse cache only when SDK reports the same last_modified —
                    # otherwise the session has new messages / away_summary /
                    # rename and we must re-read.
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
                    event = rec.get("event")
                    text = rec.get("text", "") or ""
                    ts = float(rec.get("ts", 0) or 0)
                    if ts > last_ts:
                        last_ts = ts
                    if event == "user":
                        last_user = text
                        msg_count += 1
                    elif event == "assistant":
                        last_assist = text
                        msg_count += 1
                preview = (last_assist or last_user or "").strip().replace("\n", " ")
                s["preview"] = preview[:90] + ("..." if len(preview) > 90 else "")
                s["last_ts"] = last_ts
                s["message_count"] = msg_count
                self.session_meta_cache[sid] = {
                    "mtime": tstat.st_mtime,
                    "preview": s["preview"],
                    "last_ts": s["last_ts"],
                    "message_count": msg_count,
                }
            except Exception as e:
                logger.debug("session preview read failed for %s: %s", sid, e)

        sessions.sort(key=lambda x: x.get("last_ts") or 0, reverse=True)
        return web.json_response({"ok": True, "sessions": sessions})

    async def _handle_rename_session(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        bot = str(data.get("bot") or "").strip()
        machine = str(data.get("machine") or "").strip()
        session_id = str(data.get("session_id") or "").strip()
        title = str(data.get("title") or "").strip()
        if not bot or not machine or not session_id:
            return web.json_response({"ok": False, "error": "missing bot/machine/session_id"}, status=400)
        if not title:
            return web.json_response({"ok": False, "error": "title is empty"}, status=400)
        if machine != self.topology.local_machine_id():
            response = await self.cluster_rpc.dispatch_machine_request(
                machine, "POST", "/api/sessions/rename", request, body=data,
            )
            if response is not None:
                return response
        bot_config = self.config.bots.get(bot)
        bot_config = self.config.bots.get(bot)
        backend = (bot_config.ai_backend if bot_config else None) or "claude-cli"
        if backend not in ("claude-cli", "agent-sdk-claude"):
            return web.json_response(
                {"ok": False, "error": f"backend {backend!r} does not support rename"},
                status=400,
            )
        try:
            from boxagent.history import get_history
            history = get_history(backend)
            await history.rename_session(session_id, title)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        self.session_meta_cache.pop(session_id, None)
        return web.json_response({"ok": True, "session_id": session_id, "title": title})

    async def _handle_version(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        from boxagent._version import __version__, _git_commit, version_string

        local = {
            "machine_id": self.topology.local_machine_id(),
            "version": __version__,
            "commit": _git_commit(),
            "version_string": version_string(),
        }
        if request.query.get("cluster") not in ("1", "true", "yes"):
            return web.json_response({"ok": True, **local})
        if self.topology.guest_registry is not None:
            sats: dict[str, object] = {}
            for machine_id, session in list(self.topology.guest_registry.sessions.items()):
                try:
                    result = await self.cluster_rpc.request(machine_id, "GET", "/api/version", timeout=5.0)
                    sats[machine_id] = result.get("body") or {"error": "no body"}
                except Exception as e:
                    sats[machine_id] = {"error": str(e)}
            return web.json_response({"ok": True, "self": local, "sats": sats})
        if self.topology.guest_client is not None:
            try:
                host_result = await self.topology.guest_client.fetch_host_json("/api/version", {"cluster": "1"})
            except Exception as e:
                host_result = {"error": str(e)}
            return web.json_response({"ok": True, "self": local, "host": host_result})
        return web.json_response({"ok": True, "self": local, "sats": {}})

    async def _handle_admin_restart(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        import os
        import signal as _signal
        loop = asyncio.get_event_loop()
        loop.call_later(0.2, lambda: os.kill(os.getpid(), _signal.SIGTERM))
        return web.json_response({
            "ok": True, "restarting": self.topology.local_machine_id(),
            "note": "SIGTERM scheduled in 0.2s; supervisor must relaunch",
        })

    async def _handle_admin_cluster_restart(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        try:
            data = await request.json()
        except Exception:
            data = {}
        if self.topology.guest_registry is None:
            # Guest mode: only the host owns the cluster registry, so forward
            # the request upstream and surface its response. Lets the per-machine
            # Restart button work from any node's UI.
            guest_client = self.topology.guest_client
            if guest_client is None:
                return web.json_response(
                    {"ok": False, "error": "no host connection"}, status=503,
                )
            try:
                result = await guest_client.fetch_host_json(
                    "/api/admin/cluster_restart",
                    query=dict(request.query),
                    method="POST",
                    body=data if isinstance(data, dict) else {},
                )
            except Exception as e:
                return web.json_response(
                    {"ok": False, "error": f"host fwd failed: {e}"}, status=502,
                )
            return web.json_response(result)
        include_self = request.query.get("include_self") in ("1", "true", "yes")
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
        return web.json_response({"ok": True, "results": results})

    async def _handle_web_history(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        bot = request.query.get("bot", "")
        chat_id = request.query.get("chat_id", "")
        machine = request.query.get("machine", "")
        if not bot or not chat_id or not machine:
            return web.json_response({"ok": False, "error": "missing bot/chat_id/machine"}, status=400)
        if machine != self.topology.local_machine_id():
            response = await self.cluster_rpc.dispatch_machine_request(machine, "GET", "/api/history", request)
            if response is not None:
                return response
        if bot not in self.web_channels:
            return web.json_response({"ok": False, "error": "bot not web-enabled"}, status=404)

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
                # Walk the SDK's compact chain off whichever session is the
                # oldest one we already know about — catches cases where
                # storage's previous_session_ids missed a hop (cross-machine
                # restore, manual /resume of a native session, native
                # auto-compact while storage was unavailable).
                walk_method = getattr(history_impl, "walk_compact_chain", None)
                if walk_method is not None and sids:
                    try:
                        ancestors = await walk_method(sids[-1])
                    except Exception:  # walking is best-effort
                        ancestors = []
                    if ancestors:
                        existing = set(sids)
                        # ancestors are oldest-first; we render newest-first → reverse
                        for sid in reversed(ancestors):
                            if sid not in existing:
                                sids.append(sid)
                                existing.add(sid)
                # SDK can find a session without us knowing the project
                # — pass empty project_id and let it search.
                for sid in sids:
                    messages = await history_impl.read_messages(sid)
                    history.extend(_message_to_dict(m) for m in messages)
                history.sort(key=lambda r: r.get("ts") or 0)
                total = len(history)
                limit = int(request.query.get("limit", 0) or 0)
                offset = int(request.query.get("offset", 0) or 0)
                if limit > 0:
                    history = history[-(offset + limit):len(history) - offset if offset else None]
                return web.json_response({"ok": True, "total": total, "history": history})

            import json as _json
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

    async def _handle_web_session_info(self, request: web.Request) -> web.Response:
        """SessionInfo for one ``session_id`` (chat-decoupled).

        Required: ``session_id``, ``backend_kind``, ``machine`` (for
        cluster_rpc dispatch). Optional: ``model`` (for context_window
        lookup), ``workspace`` (helps locate Codex transcripts).
        """
        if not self._authorized(request):
            return self._unauthorized()
        session_id = request.query.get("session_id", "")
        backend_kind = request.query.get("backend_kind", "")
        machine = request.query.get("machine", "")
        model = request.query.get("model", "")
        workspace = request.query.get("workspace", "")
        if not session_id or not backend_kind or not machine:
            return web.json_response(
                {"ok": False, "error": "missing session_id/backend_kind/machine"},
                status=400,
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
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        return web.json_response({"ok": True, "info": {
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

    async def _handle_web_send(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
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
        if machine != self.topology.local_machine_id():
            response = await self.cluster_rpc.dispatch_machine_request(machine, "POST", "/api/send", request, body=body)
            if response is not None:
                return response
        channel = self.web_channels.get(bot)
        if channel is None:
            return web.json_response({"ok": False, "error": "bot not web-enabled"}, status=404)
        try:
            await channel.inject(chat_id=chat_id, text=text, user_id="web")
        except Exception as e:
            logger.exception("web send failed")
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        return web.json_response({"ok": True})

    async def _handle_web_stream(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            return self._unauthorized()
        bot = request.query.get("bot", "")
        chat_id = request.query.get("chat_id", "")
        machine = request.query.get("machine", "")
        if not bot or not chat_id or not machine:
            return web.json_response({"ok": False, "error": "missing bot/chat_id/machine"}, status=400)
        if self.message_bus is None:
            return web.json_response({"ok": False, "error": "bus unavailable"}, status=503)

        # Location-transparent: a browser subscribes to chat.<owner>.<bot>.<chat_id>
        # and the bus delivers, whether the owning bot is on this machine (its
        # WebChannel publishes locally) or a remote one (the ClusterBus forwards
        # its broadcast packet here). Same SSE loop either way.
        from boxagent.bus.subscriber import QueueSubscriber
        local_machine = self.topology.local_machine_id()
        owner = machine or local_machine
        if owner == local_machine and self.web_channels.get(bot) is None:
            return web.json_response({"ok": False, "error": "bot not web-enabled"}, status=404)
        topic = f"chat.{owner}.{bot}.{chat_id}"
        queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
        subscription = self.message_bus.subscribe(topic, QueueSubscriber(queue, topic))

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        await response.write(b": connected\n\n")
        import json as _json
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    await response.write(b": ping\n\n")
                    continue
                if event.get("type") == "_close":
                    break
                payload = _json.dumps(event, ensure_ascii=False)
                await response.write(f"data: {payload}\n\n".encode("utf-8"))
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            subscription.close()
        return response

    # ── Claude native session picker ──

    async def _handle_claude_projects(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        machine = request.query.get("machine", "")
        if not machine:
            return web.json_response({"ok": False, "error": "missing machine"}, status=400)
        if machine != self.topology.local_machine_id():
            response = await self.cluster_rpc.dispatch_machine_request(machine, "GET", "/api/claude/projects", request)
            if response is not None:
                return response
        from boxagent.history import get_history
        history = get_history("claude-cli")
        projects = await history.list_projects()
        total = len(projects)
        try:
            limit = int(request.query.get("limit", "30"))
        except ValueError:
            limit = 30
        try:
            offset = int(request.query.get("offset", "0"))
        except ValueError:
            offset = 0
        offset = max(offset, 0)
        if limit > 0:
            page = projects[offset:offset + limit]
        else:
            page = projects[offset:]
        return web.json_response({
            "ok": True,
            "projects": [_project_to_dict(p) for p in page],
            "total": total,
            "offset": offset,
            "has_more": offset + len(page) < total,
        })

    async def _handle_claude_sessions(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        encoded = request.query.get("project", "")
        machine = request.query.get("machine", "")
        if not encoded or not machine:
            return web.json_response({"ok": False, "error": "missing project/machine"}, status=400)
        if machine != self.topology.local_machine_id():
            response = await self.cluster_rpc.dispatch_machine_request(machine, "GET", "/api/claude/sessions", request)
            if response is not None:
                return response
        from boxagent.history import get_history
        history = get_history("claude-cli")
        limit_raw = request.query.get("limit")
        if limit_raw is not None:
            try:
                limit = max(0, min(500, int(limit_raw)))
                offset = max(0, int(request.query.get("offset", "0")))
            except ValueError:
                return web.json_response({"ok": False, "error": "invalid offset/limit"}, status=400)
            sessions, total = await history.list_sessions_paginated(encoded, offset, limit)
            return web.json_response({
                "ok": True,
                "sessions": [_session_info_to_dict(s) for s in sessions],
                "total": total,
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(sessions) < total,
            })
        sessions = await history.list_sessions(encoded)
        return web.json_response({
            "ok": True,
            "sessions": [_session_info_to_dict(s) for s in sessions],
        })

    async def _handle_claude_transcript(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        encoded = request.query.get("project", "")
        sid = request.query.get("session_id", "")
        machine = request.query.get("machine", "")
        backend_kind = request.query.get("backend", "") or "claude-cli"
        if not encoded or not sid or not machine:
            return web.json_response({"ok": False, "error": "missing project/session_id/machine"}, status=400)
        if machine != self.topology.local_machine_id():
            response = await self.cluster_rpc.dispatch_machine_request(machine, "GET", "/api/claude/transcript", request)
            if response is not None:
                return response
        from boxagent.history import get_history
        try:
            history = get_history(backend_kind)
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        messages = await history.read_messages(sid, encoded)
        return web.json_response({
            "ok": True,
            "messages": [_message_to_dict(m) for m in messages],
        })

    async def _handle_claude_resume(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        bot = body.get("bot", "")
        sid = body.get("session_id", "")
        encoded = body.get("project", "")
        machine = body.get("machine", "")
        backend_override = body.get("backend", "")
        if not bot or not sid or not machine:
            return web.json_response({"ok": False, "error": "missing bot/session_id/machine"}, status=400)
        if machine != self.topology.local_machine_id():
            response = await self.cluster_rpc.dispatch_machine_request(machine, "POST", "/api/claude/resume", request, body=body)
            if response is not None:
                return response
        if bot not in self.web_channels:
            return web.json_response({"ok": False, "error": "bot not web-enabled"}, status=404)

        is_raw = bot == "raw"
        workspace = ""
        if self.storage:
            config = self.config.bots.get(bot)
            model = (config.model if config else None) or ""
            if is_raw:
                backend = backend_override or "claude-cli"
            else:
                backend = (config.ai_backend if config else None) or "claude-cli"
            # ``encoded`` (project_id) is now the cwd path — use it
            # directly. Older callers that don't supply project still
            # fall back to bot defaults below.
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
        return web.json_response({
            "ok": True,
            "chat_id": chat_id,
            "session_id": sid,
            "project": encoded,
            "backend": backend if self.storage else "",
            "workspace": workspace if self.storage else "",
        })

    # ── Event log handlers (commit #4) ──

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

    def _events_query_args(self, request: web.Request) -> dict:
        q = request.query
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

    async def _handle_events_query(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        if self.event_bus is None:
            return web.json_response({"ok": True, "events": []})
        kwargs = self._events_query_args(request)
        kwargs.setdefault("limit", 200)
        events = self.event_bus._store.query(**kwargs)
        next_cursor = events[-1].id if len(events) >= kwargs["limit"] else None
        return web.json_response({
            "ok": True,
            "events": [self._event_to_dict(e) for e in events],
            "next_cursor": next_cursor,
        })

    async def _handle_events_categories(self, request: web.Request) -> web.Response:
        """Distinct categories for the tree-nav builder."""
        if not self._authorized(request):
            return self._unauthorized()
        if self.event_bus is None:
            return web.json_response({"ok": True, "categories": []})
        cursor = self.event_bus._store._conn.execute(
            "SELECT category, COUNT(*) FROM events GROUP BY category ORDER BY category"
        )
        rows = [{"category": r[0], "count": r[1]} for r in cursor.fetchall()]
        return web.json_response({"ok": True, "categories": rows})

    async def _handle_events_machines(self, request: web.Request) -> web.Response:
        """Distinct origin_machine values seen in event history (includes offline nodes)."""
        if not self._authorized(request):
            return self._unauthorized()
        if self.event_bus is None:
            return web.json_response({"ok": True, "machines": []})
        cursor = self.event_bus._store._conn.execute(
            "SELECT origin_machine, COUNT(*) FROM events GROUP BY origin_machine ORDER BY origin_machine"
        )
        rows = [{"machine_id": r[0], "count": r[1]} for r in cursor.fetchall() if r[0]]
        return web.json_response({"ok": True, "machines": rows})

    async def _handle_events_mark_read(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        if self.event_bus is None:
            return web.json_response({"ok": True, "updated": 0})
        try:
            event_id = int(request.match_info["event_id"])
        except (KeyError, ValueError):
            return web.json_response({"ok": False, "error": "bad event_id"}, status=400)
        n = self.event_bus._store.mark_read([event_id])
        return web.json_response({"ok": True, "updated": n})

    async def _handle_events_read_all(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        if self.event_bus is None:
            return web.json_response({"ok": True, "updated": 0})
        body = await request.json() if request.body_exists else {}
        kwargs = {}
        if isinstance(body, dict):
            for k in ("bot", "levels", "machines", "category_prefix", "since", "until"):
                if k in body:
                    kwargs[k] = body[k]
        kwargs["unread_only"] = True
        events = self.event_bus._store.query(**kwargs, limit=10000)
        ids = [e.id for e in events if e.id is not None]
        n = self.event_bus._store.mark_read(ids)
        return web.json_response({"ok": True, "updated": n})

    async def _handle_logs_query(self, request: web.Request) -> web.Response:
        """Tail of <local-dir>/boxagent.log for the Web UI Logs page.

        Query params:
          machine — target machine_id (forwarded via cluster RPC if not local)
          limit   — max entries to return (default 200, capped at 2000)
          offset  — entries to skip from end (for pagination, default 0)
          levels  — comma-separated level filter (case-insensitive)
          grep    — case-insensitive substring filter
        """
        if not self._authorized(request):
            return self._unauthorized()
        machine = request.query.get("machine", "").strip()
        if machine:
            response = await self.cluster_rpc.dispatch_machine_request(machine, "GET", "/api/logs", request)
            if response is not None:
                return response
        from boxagent.transports.web.log_file import read_tail
        try:
            limit = max(1, min(2000, int(request.query.get("limit", "200"))))
        except ValueError:
            limit = 200
        try:
            offset = max(0, int(request.query.get("offset", "0")))
        except ValueError:
            offset = 0
        levels_raw = request.query.get("levels", "").strip()
        levels = [s.strip() for s in levels_raw.split(",") if s.strip()] if levels_raw else None
        grep = request.query.get("grep", "").strip() or None
        log_file = getattr(self.config, "log_file", None)
        if not log_file:
            return web.json_response({"ok": True, "lines": [], "has_more": False, "log_file": None})
        result = read_tail(Path(log_file), limit=limit, offset=offset, levels=levels, grep=grep)
        return web.json_response({
            "ok": True,
            "lines": result["lines"],
            "has_more": result["has_more"],
            "log_file": str(log_file),
        })

    async def _handle_events_stream(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            return self._unauthorized()
        from boxagent.events.web_stream import EventStreamSubscriber
        import json as _json

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        if self.event_bus is None:
            await response.write(b"event: ready\ndata: {}\n\n")
            return response

        q = request.query
        sub = EventStreamSubscriber(
            bus=self.event_bus,
            levels=([s for s in q["levels"].split(",") if s] if q.get("levels") else None),
            machines=([s for s in q["machines"].split(",") if s] if q.get("machines") else None),
            bot=q.get("bot") or None,
            category_prefix=q.get("category_prefix") or None,
        )
        try:
            await response.write(b"event: ready\ndata: {}\n\n")
            while True:
                try:
                    event = await asyncio.wait_for(sub.queue.get(), timeout=15.0)
                    payload = _json.dumps(self._event_to_dict(event), ensure_ascii=False)
                    await response.write(f"event: event\ndata: {payload}\n\n".encode("utf-8"))
                except asyncio.TimeoutError:
                    await response.write(b": ping\n\n")
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            sub.close()
        return response

    # ── Schedules ──

    async def _handle_schedules_list(self, request: web.Request) -> web.Response:
        """GET /api/schedules?machine= — list schedules.yaml entries."""
        if not self._authorized(request):
            return self._unauthorized()
        machine = request.query.get("machine", "")
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
            return web.json_response({"ok": False, "error": str(e)}, status=500)
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
        return web.json_response({"ok": True, "schedules": items, "node_id": self.config.node_id})

    async def _handle_schedules_runs(self, request: web.Request) -> web.Response:
        """GET /api/schedules/runs?task=&machine=&limit= — list run records."""
        if not self._authorized(request):
            return self._unauthorized()
        machine = request.query.get("machine", "")
        if machine:
            response = await self.cluster_rpc.dispatch_machine_request(
                machine, "GET", "/api/schedules/runs", request,
            )
            if response is not None:
                return response
        from boxagent.scheduler.cli import load_run_logs
        task_id = request.query.get("task", "")
        try:
            limit = max(1, int(request.query.get("limit", "50")))
        except ValueError:
            limit = 50
        try:
            offset = max(0, int(request.query.get("offset", "0")))
        except ValueError:
            offset = 0
        all_entries = load_run_logs(self.local_dir, task_id=task_id)
        page = all_entries[offset:offset + limit]
        return web.json_response({
            "ok": True, "runs": page, "offset": offset,
            "total": len(all_entries), "node_id": self.config.node_id,
        })

    async def _handle_schedules_run_detail(self, request: web.Request) -> web.Response:
        """GET /api/schedules/runs/<task>/<index>?machine= — single run record."""
        if not self._authorized(request):
            return self._unauthorized()
        machine = request.query.get("machine", "")
        if machine:
            response = await self.cluster_rpc.dispatch_machine_request(
                machine, "GET", request.path, request,
            )
            if response is not None:
                return response
        from boxagent.scheduler.cli import load_run_logs
        task_id = request.match_info["task_id"]
        try:
            run_index = int(request.match_info["run_index"])
        except ValueError:
            return web.json_response({"ok": False, "error": "invalid run_index"}, status=400)
        entries = load_run_logs(self.local_dir, task_id=task_id)
        if run_index < 1 or run_index > len(entries):
            return web.json_response({"ok": False, "error": "not found"}, status=404)
        return web.json_response({"ok": True, "run": entries[run_index - 1]})
