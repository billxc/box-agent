"""Web transport — aiohttp server, auth, and route handlers for the Web UI.

Mounted as a mixin on Gateway. Handlers reference Gateway state via ``self``
(``self._storage``, ``self._web_channels``, ``self.config``, etc.); the mixin
itself does not own any new state.

The host's *web* port (default 9292) also carries cluster RPC routes
(``/api/peer/send``, ``/api/wg/peer/recv``, ``/api/guest/ws``) — those are
registered by the Gateway via ``_register_extra_web_routes`` on the same
aiohttp app, since the rest of the cluster code already targets that port.
"""

from dataclasses import asdict


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
    }


def _message_to_dict(m) -> dict:
    """Serialise a ``boxagent.history.Message`` to the legacy record shape
    consumed by the web UI's transcript replay."""
    base = {"role": m.role, "ts": m.ts}
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


import asyncio
import logging
import time
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)


class WebServerMixin:
    # ── Lifecycle ──

    async def _start_web_http(self) -> None:
        """Build and start the Web UI aiohttp server (own port)."""
        web_app = web.Application()

        # Web UI / API routes
        self._register_web_routes(web_app)

        # Hook for non-web routes that share this port (cluster RPC etc.)
        register_extras = getattr(self, "_register_extra_web_routes", None)
        if register_extras is not None:
            register_extras(web_app)

        # Static files last so the catch-all doesn't shadow API routes
        web_static = Path(__file__).parent.parent.parent / "web" / "static"
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

    def _register_web_routes(self, app: web.Application) -> None:
        app.router.add_get("/", self._handle_web_index)
        app.router.add_get("/api/bots", self._handle_web_bots)
        app.router.add_get("/api/machines", self._handle_web_machines)
        app.router.add_get("/api/sessions", self._handle_web_sessions)
        app.router.add_post("/api/sessions/set_main", self._handle_set_main_session)
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

    # ── Auth ──

    def _web_authorized(self, request: web.Request) -> bool:
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

    def _web_unauthorized(self) -> web.Response:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    # ── Handlers ──

    async def _handle_web_index(self, request: web.Request) -> web.Response:
        # Always serve the index page so users can paste ?token=... to log in.
        index = Path(__file__).parent.parent.parent / "web" / "static" / "index.html"
        if not index.is_file():
            return web.Response(text="web UI not installed", status=404)
        return web.Response(body=index.read_bytes(), content_type="text/html")

    async def _handle_web_bots(self, request: web.Request) -> web.Response:
        if not self._web_authorized(request):
            return self._web_unauthorized()
        bots = []
        local_mid = self._topology.local_machine_id()
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
        if not self._web_authorized(request):
            return self._web_unauthorized()
        if self.guest_registry is not None:
            return web.json_response({"machines": self._topology.collect_machines()})
        local_mid = self._topology.local_machine_id()
        local_role = self._topology.local_role()
        machines: list[dict] = [{
            "machine_id": local_mid,
            "online": True,
            "role": local_role,
            "self": True,
            "bots": self._topology.local_bot_descriptors(),
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
        from boxagent.utils import infer_platform
        if not self._web_authorized(request):
            return self._web_unauthorized()
        bot = request.query.get("bot", "")
        machine = request.query.get("machine", "")
        if not bot or not machine:
            return web.json_response({"ok": False, "error": "missing bot/machine"}, status=400)
        resp = await self._cluster_rpc.dispatch_machine_request(machine, "GET", "/api/sessions", request)
        if resp is not None:
            return resp
        if bot not in self._web_channels:
            return web.json_response({"ok": False, "error": "bot not web-enabled"}, status=404)
        if not self._storage:
            return web.json_response({"ok": True, "sessions": []})

        sessions = self._storage.list_chat_sessions(bot)
        main_chat_id = self._storage.get_main_chat_id(bot)

        claude_session_info: dict[str, dict] = {}
        bot_cfg = self.config.bots.get(bot)
        wg_cfg = self.config.workgroups.get(bot)
        backend = (bot_cfg.ai_backend if bot_cfg else None) or (wg_cfg.ai_backend if wg_cfg else "claude-cli")
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
            s["is_main"] = bool(main_chat_id and s["chat_id"] == main_chat_id)
            s["preview"] = ""
            s["last_ts"] = 0
            s["message_count"] = 0
            if not sid:
                continue

            cached = self._session_meta_cache.get(sid)

            # Try claude-native lookup first (covers claude-cli + agent-sdk-claude).
            if _claude_history is not None:
                if cached:
                    s["preview"] = cached.get("preview", "")
                    s["last_ts"] = cached.get("last_ts", 0)
                    s["message_count"] = cached.get("message_count", 0)
                    continue
                info = await _claude_history.get_session_info(sid)
                if info is not None:
                    preview = (info.first_user or "").strip().replace("\n", " ")
                    s["preview"] = preview[:90] + ("..." if len(preview) > 90 else "")
                    s["last_ts"] = info.last_ts
                    s["message_count"] = info.message_count
                    self._session_meta_cache[sid] = {
                        "mtime": info.last_ts,
                        "preview": s["preview"],
                        "last_ts": s["last_ts"],
                        "message_count": s["message_count"],
                    }
                    continue

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
        if machine != self._topology.local_machine_id():
            resp = await self._cluster_rpc.dispatch_machine_request(
                machine, "POST", "/api/sessions/set_main", request, body=data,
            )
            if resp is not None:
                return resp
        if self._storage is None:
            return web.json_response({"ok": False, "error": "no storage"}, status=500)
        self._storage.set_main_chat_id(bot, chat_id)
        return web.json_response({"ok": True, "main_chat_id": chat_id})

    async def _handle_version(self, request: web.Request) -> web.Response:
        if not self._web_authorized(request):
            return self._web_unauthorized()
        from boxagent._version import __version__, _git_commit, version_string

        local = {
            "machine_id": self._topology.local_machine_id(),
            "version": __version__,
            "commit": _git_commit(),
            "version_string": version_string(),
        }
        if request.query.get("cluster") not in ("1", "true", "yes"):
            return web.json_response({"ok": True, **local})
        if self.guest_registry is not None:
            sats: dict[str, object] = {}
            for machine_id, sess in list(self.guest_registry.sessions.items()):
                try:
                    result = await sess.call("GET", "/api/version", timeout=5.0)
                    sats[machine_id] = result.get("body") or {"error": "no body"}
                except Exception as e:
                    sats[machine_id] = {"error": str(e)}
            return web.json_response({"ok": True, "self": local, "sats": sats})
        if self.guest_client is not None:
            try:
                host_result = await self.guest_client.fetch_host_json("/api/version", {"cluster": "1"})
            except Exception as e:
                host_result = {"error": str(e)}
            return web.json_response({"ok": True, "self": local, "host": host_result})
        return web.json_response({"ok": True, "self": local, "sats": {}})

    async def _handle_admin_restart(self, request: web.Request) -> web.Response:
        if not self._web_authorized(request):
            return self._web_unauthorized()
        import os
        import signal as _signal
        loop = asyncio.get_event_loop()
        loop.call_later(0.2, lambda: os.kill(os.getpid(), _signal.SIGTERM))
        return web.json_response({
            "ok": True, "restarting": self._topology.local_machine_id(),
            "note": "SIGTERM scheduled in 0.2s; supervisor must relaunch",
        })

    async def _handle_admin_cluster_restart(self, request: web.Request) -> web.Response:
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
        if include_self and (target_filter is None or self._topology.local_machine_id() in target_filter):
            import os
            import signal as _signal
            asyncio.get_event_loop().call_later(
                1.0, lambda: os.kill(os.getpid(), _signal.SIGTERM),
            )
            results[self._topology.local_machine_id()] = {
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
        if machine != self._topology.local_machine_id():
            resp = await self._cluster_rpc.dispatch_machine_request(machine, "GET", "/api/history", request)
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

            if saved_backend in ("claude-cli", "agent-sdk-claude") and sids:
                from boxagent.history import get_history
                history_impl = get_history(saved_backend)
                # SDK can find a session without us knowing the project
                # — pass empty project_id and let it search.
                for sid in sids:
                    msgs = await history_impl.read_messages(sid)
                    history.extend(_message_to_dict(m) for m in msgs)
                history.sort(key=lambda r: r.get("ts") or 0)
                total = len(history)
                limit = int(request.query.get("limit", 0) or 0)
                offset = int(request.query.get("offset", 0) or 0)
                if limit > 0:
                    history = history[-(offset + limit):len(history) - offset if offset else None]
                return web.json_response({"ok": True, "total": total, "history": history})

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
        if machine != self._topology.local_machine_id():
            resp = await self._cluster_rpc.dispatch_machine_request(machine, "POST", "/api/send", request, body=body)
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
        if machine != self._topology.local_machine_id():
            resp = await self._cluster_rpc.dispatch_machine_stream(machine, "/api/stream", request)
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
        if machine != self._topology.local_machine_id():
            resp = await self._cluster_rpc.dispatch_machine_request(machine, "GET", "/api/claude/projects", request)
            if resp is not None:
                return resp
        from boxagent.history import get_history
        history = get_history("claude-cli")
        projects = await history.list_projects()
        return web.json_response({
            "ok": True,
            "projects": [_project_to_dict(p) for p in projects],
        })

    async def _handle_claude_sessions(self, request: web.Request) -> web.Response:
        if not self._web_authorized(request):
            return self._web_unauthorized()
        encoded = request.query.get("project", "")
        machine = request.query.get("machine", "")
        if not encoded or not machine:
            return web.json_response({"ok": False, "error": "missing project/machine"}, status=400)
        if machine != self._topology.local_machine_id():
            resp = await self._cluster_rpc.dispatch_machine_request(machine, "GET", "/api/claude/sessions", request)
            if resp is not None:
                return resp
        from boxagent.history import get_history
        history = get_history("claude-cli")
        sessions = await history.list_sessions(encoded)
        return web.json_response({
            "ok": True,
            "sessions": [_session_info_to_dict(s) for s in sessions],
        })

    async def _handle_claude_transcript(self, request: web.Request) -> web.Response:
        if not self._web_authorized(request):
            return self._web_unauthorized()
        encoded = request.query.get("project", "")
        sid = request.query.get("session_id", "")
        machine = request.query.get("machine", "")
        if not encoded or not sid or not machine:
            return web.json_response({"ok": False, "error": "missing project/session_id/machine"}, status=400)
        if machine != self._topology.local_machine_id():
            resp = await self._cluster_rpc.dispatch_machine_request(machine, "GET", "/api/claude/transcript", request)
            if resp is not None:
                return resp
        from boxagent.history import get_history
        history = get_history("claude-cli")
        messages = await history.read_messages(sid, encoded)
        return web.json_response({
            "ok": True,
            "messages": [_message_to_dict(m) for m in messages],
        })

    async def _handle_claude_resume(self, request: web.Request) -> web.Response:
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
        backend_override = body.get("backend", "")
        if not bot or not sid or not machine:
            return web.json_response({"ok": False, "error": "missing bot/session_id/machine"}, status=400)
        if machine != self._topology.local_machine_id():
            resp = await self._cluster_rpc.dispatch_machine_request(machine, "POST", "/api/claude/resume", request, body=body)
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
            # ``encoded`` (project_id) is now the cwd path — use it
            # directly. Older callers that don't supply project still
            # fall back to bot/workgroup defaults below.
            workspace = encoded or (
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
