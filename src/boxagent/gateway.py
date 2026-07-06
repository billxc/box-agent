"""Gateway — composition root + internal HTTP API host.

Owns lifecycle wiring; behavior lives in the composed managers built in
``Gateway.start()``:

  - ``_bots``             AgentManager        (per-bot lifecycle + teardown)
  - ``_topology``         TopologyService     (cluster identity / peer list)
  - ``_cluster_rpc``      ClusterRpc          (host↔guest HTTP/SSE proxying)
  - ``_cluster_routes``   ClusterHttpRoutes   (cluster routes on web port)
  - ``_scheduler_routes`` SchedulerHttpRoutes (POST /api/schedule/run)
  - ``_internal_api``     InternalApiServer   (TCP aiohttp app, port file: api-port.txt)
  - ``_mcp_server``       McpHttpServer       (uvicorn streamable-http MCP)
  - ``_web_server``       WebHttpServer       (Web UI aiohttp server)

Two-phase DI:
  Phase 1 — managers built with infrastructure (config / shared dicts /
            sibling refs that already exist).
  Phase 2 — late-bound siblings injected via setters
            (scheduler in ``set_scheduler``, host_election in
            ``set_host_election``).
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from boxagent.agent.agent_manager import AgentManager
from boxagent.cluster.http_routes import ClusterHttpRoutes
from boxagent.cluster.rpc import ClusterRpc
from boxagent.cluster.host_election import HostElection
from boxagent.cluster.topology_service import TopologyService
from boxagent.scheduler.http_routes import SchedulerHttpRoutes
from boxagent.transports.mcp.server import McpHttpServer
from boxagent.transports.web.server import WebHttpServer
from boxagent.config import AppConfig
from boxagent.utils import default_config_dir, default_local_dir, default_workspace_dir
from boxagent.sessions import Storage
from boxagent.scheduler import Scheduler

if TYPE_CHECKING:
    from boxagent.events.bus import EventBus

logger = logging.getLogger(__name__)


# ── Internal HTTP API server (scheduler routes) ──


class InternalApiServer:
    """TCP aiohttp app exposing the internal API used by IPC siblings.

    Held by Gateway. Mounts the scheduler route adapter when provided.

    Port resolved at bind time and written to ``api-port.txt`` so other
    in-process siblings (the schedule CLI, doctor) can find us. Stale
    ``api.sock`` and ``api-port.txt`` from a previous run are deleted at
    start so the new process can re-bind cleanly.
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        local_dir: Path,
        scheduler_routes: SchedulerHttpRoutes | None,
    ) -> None:
        self.config = config
        self.local_dir = local_dir
        self.scheduler_routes = scheduler_routes
        self._runner: web.AppRunner | None = None

    @property
    def port_file(self) -> Path:
        return self.local_dir / "api-port.txt"

    def _clear_artifacts(self) -> None:
        for f in (self.port_file, self.local_dir / "api.sock"):
            if f.exists():
                f.unlink(missing_ok=True)

    async def start(self) -> None:
        from boxagent.web_error_middleware import error_logging_middleware
        app = web.Application(middlewares=[error_logging_middleware])
        if self.scheduler_routes is not None:
            app.router.add_post("/api/schedule/run", self.scheduler_routes.handle_schedule_run)

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
        self.port_file.write_text(f"{actual_port}\n", encoding="utf-8")
        logger.info("HTTP API listening on 127.0.0.1:%d", actual_port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self.port_file.unlink(missing_ok=True)


# ── Gateway ──


@dataclass
class Gateway:
    config: AppConfig
    config_dir: Path = field(default_factory=default_config_dir)
    local_dir: Path = field(default_factory=default_local_dir)
    _storage: Storage | None = field(default=None, repr=False)
    _scheduler: Scheduler | None = field(default=None, repr=False)
    _scheduler_task: asyncio.Task | None = field(default=None, repr=False)
    _host_election: HostElection | None = field(default=None, repr=False)
    _start_time: float = 0.0
    _bots: AgentManager | None = field(default=None, repr=False)
    _topology: TopologyService | None = field(default=None, repr=False)
    _cluster_rpc: ClusterRpc | None = field(default=None, repr=False)
    _cluster_routes: ClusterHttpRoutes | None = field(default=None, repr=False)
    _scheduler_routes: SchedulerHttpRoutes | None = field(default=None, repr=False)
    _internal_api: InternalApiServer | None = field(default=None, repr=False)
    _mcp_server: McpHttpServer | None = field(default=None, repr=False)
    _web_server: WebHttpServer | None = field(default=None, repr=False)
    _event_bus: "EventBus | None" = field(default=None, repr=False)
    _telegram_notifier: "object | None" = field(default=None, repr=False)
    _retention_sweeper: "object | None" = field(default=None, repr=False)
    _event_syncer: "object | None" = field(default=None, repr=False)
    _chat_syncer: "object | None" = field(default=None, repr=False)
    _chat_bus: "object | None" = field(default=None, repr=False)

    async def start(self) -> None:
        self._start_time = time.time()
        storage = Storage(local_dir=self.local_dir)
        self._storage = storage

        # Event log: bind log facade to EventBus so all `log.info(...)` calls
        # from business code persist + dispatch to subscribers (web SSE,
        # telegram notifier, syncer — added in later commits).
        from boxagent.events.bus import EventBus
        from boxagent.events.storage import EventStore
        from boxagent.log import Category, log

        machine_id = (
            self.config.machine_id
            or self.config.node_id
            or "local"
        )
        event_store = EventStore(self.local_dir / "events.db")
        # One shared MessageBus for the whole process: events publish on
        # "events.<category>", chat on "chat.<machine>.<bot>.<chat_id>" — one
        # instance carries both (the owner's "one bus"). Injected into EventBus
        # here and into every WebChannel via AgentManager.
        from boxagent.bus.core import MessageBus
        self._message_bus = MessageBus(machine_id=machine_id)
        self._event_bus = EventBus(store=event_store, machine_id=machine_id, bus=self._message_bus)
        log.bind(self._event_bus)

        # Standalone Telegram push: subscribes to the bus, posts to bot API.
        # Decoupled from chat-bot tokens; uses notify.telegram.* config.
        from boxagent.events.telegram_notifier import TelegramNotifier
        self._telegram_notifier = TelegramNotifier(
            token=self.config.notify_telegram_token,
            chat_id=self.config.notify_telegram_chat_id,
            levels=self.config.notify_telegram_levels,
            categories=self.config.notify_telegram_categories,
        )
        if self._telegram_notifier.enabled:
            self._telegram_notifier.attach(self._event_bus)
            logger.info("telegram notifier enabled (chat_id=%s, levels=%s)",
                        self.config.notify_telegram_chat_id,
                        self.config.notify_telegram_levels)

        from boxagent.events.retention import RetentionSweeper
        self._retention_sweeper = RetentionSweeper(event_store)
        self._retention_sweeper.start()

        # EventSyncer: cross-machine replication. Wired into HostElection
        # below so peers attach as soon as the registry/guest_client appear.
        from boxagent.events.sync import EventSyncer
        self._event_syncer = EventSyncer(event_store, self._event_bus)

        log.info(Category.SYSTEM_STARTUP, "gateway starting",
                 machine_id=machine_id, node_id=self.config.node_id)        # Phase 1: build managers. AgentManager owns its bot-state dicts;
        # everyone else who needs to read them gets the dict by reference.
        self._bots = AgentManager(
            config=self.config,
            config_dir=self.config_dir,
            storage=storage,
            start_time=self._start_time,
            gateway=self,
            message_bus=self._message_bus,
            machine_id=machine_id,
        )
        self._topology = TopologyService(
            config=self.config,
            web_channels=self._bots.web_channels,
        )
        self._cluster_rpc = ClusterRpc(topology=self._topology)
        self._cluster_routes = ClusterHttpRoutes(
            cluster_rpc=self._cluster_rpc,
        )

        # ChatBus：location-transparent chat pub/sub。ChatSyncer 用结构化帧走
        # cluster WS 承载跨机（不再 SSE re-framing）；ChatBus 把它包起来，让
        # /api/stream 对 local + remote 读同一 queue 形状。下面挂进 HostElection
        # （和 event syncer 一样），peer 随 registry/guest_client 出现而 attach。
        from boxagent.cluster.chat_sync import ChatSyncer
        from boxagent.cluster.chat_bus import ChatBus
        topology = self._topology

        def route_chat(target_machine):
            # 一个 subscribe 往哪个 peer 走。guest → 永远走 host；
            # host → 目标 guest 的 session（peer_key == machine_id）。
            if topology.local_role() == "guest":
                return "host"
            registry = topology.guest_registry
            if registry is not None and target_machine in registry.sessions:
                return target_machine
            return None

        self._chat_syncer = ChatSyncer(
            local_machine=self._topology.local_machine_id(),
            route=route_chat,
            message_bus=self._message_bus,
        )
        self._chat_bus = ChatBus(
            local_machine=self._topology.local_machine_id(),
            message_bus=self._message_bus,
            channel_for=self._bots.web_channels.get,
        )

        self._web_server = WebHttpServer(
            config=self.config,
            local_dir=self.local_dir,
            config_dir=self.config_dir,
            storage=storage,
            web_channels=self._bots.web_channels,
            pools=self._bots.pools,
            topology=self._topology,
            cluster_rpc=self._cluster_rpc,
            cluster_routes=self._cluster_routes,
            chat_bus=self._chat_bus,
        )
        self._web_server.set_event_bus(self._event_bus)
        logger.info("Gateway starting (node=%s)", self.config.node_id or "(any)")

        # Web UI first so the page is reachable while the rest boots.
        await self._web_server.start()

        # Bots (incl. synthetic raw passthrough).
        await self._bots.start_all_for_node(self.config.node_id)

        # Scheduler + its HTTP route adapter.
        self._start_scheduler()

        # Internal API + MCP HTTP — internal API hosts scheduler_routes.
        self._internal_api = InternalApiServer(
            config=self.config,
            local_dir=self.local_dir,
            scheduler_routes=self._scheduler_routes,
        )
        await self._internal_api.start()

        self._mcp_server = McpHttpServer(
            config=self.config,
            config_dir=self.config_dir,
            local_dir=self.local_dir,
            gateway=self,
        )
        await self._mcp_server.start()

        # Cluster: host election. host_priority list determines who is host
        # vs guest at runtime, with failover when primary disappears.
        if self.config.cluster_tunnel:
            from boxagent.cluster.bus_wiring import (
                install_guest_client_hooks,
                install_registry_hooks,
            )
            event_syncer = self._event_syncer
            chat_syncer = self._chat_syncer

            # One wiring owns the registry/guest_client callbacks and dispatches
            # both event_* and chat_* frames — no install-order chain.
            def on_registry_ready(registry):
                install_registry_hooks(event_syncer, chat_syncer, registry)

            def on_guest_client_ready(client):
                install_guest_client_hooks(event_syncer, chat_syncer, client)

            self._host_election = HostElection(
                config=self.config,
                on_topology_change=self._topology.on_topology_change,
                bot_provider=self._topology.local_bot_descriptors,
                on_registry_ready=on_registry_ready,
                on_guest_client_ready=on_guest_client_ready,
            )
            # Phase 2: topology can now resolve guest_registry / guest_client.
            self._topology.set_host_election(self._host_election)
            await self._host_election.start()

        logger.info("Gateway ready: %d bot(s) active", len(self.config.bots))

    def _start_scheduler(self) -> None:
        """Create and start the Scheduler after all active bots are online."""
        assert self._bots is not None  # built in start() before this
        schedules_file = self.config_dir / "schedules.yaml"
        self._scheduler = Scheduler(
            schedules_file=schedules_file,
            node_id=self.config.node_id,
            bot_refs=self._bots.build_scheduler_refs(),
            telegram_bots=self.config.telegram_bots,
            default_workspace=str(default_workspace_dir(self.config_dir)),
            local_dir=str(self.local_dir),
        )
        self._scheduler_task = asyncio.create_task(self._scheduler.run_forever())
        # Phase 2 of two-phase DI: scheduler exists now, inject into AgentManager
        # so restart_bot / on_backend_switched can sync scheduler.bot_refs.
        self._bots.set_scheduler(self._scheduler)
        # Build the scheduler's HTTP route adapter alongside the scheduler.
        self._scheduler_routes = SchedulerHttpRoutes(
            config=self.config,
            config_dir=self.config_dir,
            scheduler=self._scheduler,
        )
        logger.info("Scheduler started (file=%s)", schedules_file)

    async def stop(self) -> None:
        logger.info("Gateway shutting down...")

        # Release listening ports first so a restarting process can re-bind
        # immediately, regardless of how long the rest of the shutdown takes.
        if self._web_server is not None:
            await self._web_server.stop()
        if self._internal_api is not None:
            await self._internal_api.stop()
        if self._mcp_server is not None:
            await self._mcp_server.stop()

        # Stop host election — it tears down whichever of guest_client /
        # cluster_tunnel / guest_registry it currently owns.
        if self._host_election is not None:
            try:
                await self._host_election.stop()
            except Exception as e:
                logger.error("Error stopping host election: %s", e)
            self._host_election = None

        # Stop scheduler
        if self._scheduler:
            self._scheduler.stop()
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except (asyncio.CancelledError, Exception):
                pass
            self._scheduler_task = None

        # AgentManager owns channels, web_channels, backends, pools, watchdogs.
        if self._bots is not None:
            # 先取消 owner 侧 chat pump —— 它们持有 manager 即将拆掉的
            # WebChannel 订阅。
            if self._chat_bus is not None:
                await self._chat_bus.aclose()
            await self._bots.stop()

        # Event log: emit shutdown event then unbind + close.
        if self._event_bus is not None:
            from boxagent.log import Category, log

            log.info(Category.SYSTEM_SHUTDOWN, "gateway stopped")
            log.unbind()
            if self._event_syncer is not None:
                try:
                    self._event_syncer.close()
                except Exception:
                    logger.exception("Error closing event syncer")
                self._event_syncer = None
            if self._retention_sweeper is not None:
                try:
                    await self._retention_sweeper.stop()
                except Exception:
                    logger.exception("Error stopping retention sweeper")
                self._retention_sweeper = None
            if self._telegram_notifier is not None:
                self._telegram_notifier.detach(self._event_bus)
                try:
                    await self._telegram_notifier.aclose()
                except Exception:
                    logger.exception("Error closing telegram notifier")
                self._telegram_notifier = None
            try:
                self._event_bus.close()
            except Exception:
                logger.exception("Error closing event bus")
            self._event_bus = None

        logger.info("Gateway stopped")
