"""HTTP API mixin — internal API server and MCP. Web UI lives in transports/web."""

import asyncio
import logging

from aiohttp import web

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
