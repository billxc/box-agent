"""Internal HTTP API + MCP HTTP server.

Composition class. Held by Gateway as ``self._http_server``. Built late
in ``Gateway.start()`` (after WorkgroupManager + Scheduler exist) so all
deps are ready at construction time — single-phase DI, no setters.

Responsibilities:
- Internal API aiohttp app (port file: api-port.txt) — workgroup +
  scheduler + peer HTTP handlers
- MCP streamable-http server (uvicorn, port file: mcp-port.txt)

NOTE: this is an internal port (not the Web UI port). Cluster RPCs that
guests forward must hit the Web UI port instead — see ClusterHttpRoutes.
"""

import asyncio
import logging
import socket
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from boxagent.cluster.peer_service import PeerService
    from boxagent.config import AppConfig
    from boxagent.scheduler.http_routes import SchedulerHttpRoutes
    from boxagent.workgroup.http_routes import WorkgroupHttpRoutes

logger = logging.getLogger(__name__)


class HttpApiServer:
    def __init__(
        self,
        *,
        config: "AppConfig",
        config_dir: Path,
        local_dir: Path,
        peer: "PeerService",
        workgroup_routes: "WorkgroupHttpRoutes | None",
        scheduler_routes: "SchedulerHttpRoutes | None",
        mcp_gateway_context: object,
    ) -> None:
        self.config = config
        self.config_dir = config_dir
        self.local_dir = local_dir
        self.peer = peer
        self.workgroup_routes = workgroup_routes
        self.scheduler_routes = scheduler_routes
        self._mcp_gateway_context = mcp_gateway_context

        self._runner: web.AppRunner | None = None
        self._mcp_server = None
        self._mcp_task: asyncio.Task | None = None

    # ── Port-file accessors ──

    @property
    def api_port_file(self) -> Path:
        return self.local_dir / "api-port.txt"

    @property
    def mcp_port_file(self) -> Path:
        return self.local_dir / "mcp-port.txt"

    def _clear_artifacts(self) -> None:
        for f in (self.api_port_file, self.mcp_port_file, self.local_dir / "api.sock"):
            if f.exists():
                f.unlink(missing_ok=True)

    # ── Lifecycle ──

    async def start(self) -> None:
        """Start the internal HTTP API server (TCP only)."""
        app = web.Application()
        if self.scheduler_routes is not None:
            app.router.add_post("/api/schedule/run", self.scheduler_routes.handle_schedule_run)
        if self.workgroup_routes is not None:
            wg = self.workgroup_routes
            app.router.add_get("/api/workgroup/specialists", wg.handle_list_specialists)
            app.router.add_get("/api/workgroup/specialist_status", wg.handle_specialist_status)
            app.router.add_post("/api/workgroup/send", wg.handle_workgroup_send)
            app.router.add_post("/api/workgroup/create_specialist", wg.handle_create_specialist)
            app.router.add_post("/api/workgroup/reset_specialist", wg.handle_reset_specialist)
            app.router.add_post("/api/workgroup/delete_specialist", wg.handle_delete_specialist)
            app.router.add_post("/api/workgroup/cancel_task", wg.handle_cancel_task)
        app.router.add_post("/api/peer/send", self.peer.handle_peer_send)
        # NOTE: /api/wg/peer/recv lives on the Web UI port (see ClusterHttpRoutes)
        # because guest_client forwards RPC frames to the web port.

        runner = web.AppRunner(app)
        await runner.setup()
        self._runner = runner

        self.local_dir.mkdir(parents=True, exist_ok=True)
        self._clear_artifacts()

        port = self.config.api_port or 0
        tcp_site = web.TCPSite(runner, "127.0.0.1", port)
        await tcp_site.start()
        sockets = getattr(getattr(tcp_site, "_server", None), "sockets", None) or []
        actual_port = sockets[0].getsockname()[1] if sockets else port
        self.api_port_file.write_text(f"{actual_port}\n", encoding="utf-8")
        logger.info("HTTP API listening on 127.0.0.1:%d", actual_port)

        await self.start_mcp()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self.api_port_file.unlink(missing_ok=True)

    # ── MCP HTTP ──

    def _pick_mcp_port(self) -> int:
        """Pick an MCP port. Preference order: configured > previous > 9390+."""
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
            return configured

        candidates: list[int] = []
        if self.mcp_port_file.exists():
            try:
                prev = int(self.mcp_port_file.read_text(encoding="utf-8").strip())
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
        return 0

    async def start_mcp(self) -> None:
        """Start the MCP streamable-http server (uvicorn)."""
        try:
            import uvicorn
            from boxagent.transports.mcp.server import create_mcp_app

            starlette_app = create_mcp_app(
                config_dir=str(self.config_dir),
                local_dir=str(self.local_dir),
                node_id=self.config.node_id,
                gateway=self._mcp_gateway_context,  # type: ignore[arg-type]
            )
            mcp_port = self._pick_mcp_port()
            uvi_config = uvicorn.Config(
                starlette_app,
                host="127.0.0.1",
                port=mcp_port,
                log_level="warning",
            )
            server = uvicorn.Server(uvi_config)
            self._mcp_server = server
            self._mcp_task = asyncio.create_task(server.serve())

            while not server.started:
                await asyncio.sleep(0.05)

            actual_port = server.servers[0].sockets[0].getsockname()[1]
            self.mcp_port_file.write_text(f"{actual_port}\n", encoding="utf-8")
            logger.info("MCP HTTP server listening on 127.0.0.1:%d", actual_port)
        except Exception as e:
            logger.error("Failed to start MCP HTTP server: %s", e)
            self._mcp_server = None
            self._mcp_task = None

    async def stop_mcp(self) -> None:
        if self._mcp_server:
            self._mcp_server.should_exit = True
        if self._mcp_task:
            try:
                await self._mcp_task
            except Exception:
                pass
            self._mcp_task = None
        self._mcp_server = None
        self.mcp_port_file.unlink(missing_ok=True)
