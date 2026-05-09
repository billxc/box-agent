"""Gateway core — composition root.

Owns the dataclass state and the start/stop wiring; all behavior lives in
the composed managers built in ``start()``:

  - ``_bots``             AgentManager        (per-bot lifecycle + teardown)
  - ``_topology``         TopologyService     (cluster identity / peer list)
  - ``_peer``             PeerService         (cross-admin peer messaging)
  - ``_cluster_rpc``      ClusterRpc          (host↔guest HTTP/SSE proxying)
  - ``_cluster_routes``   ClusterHttpRoutes   (cluster routes on web port)
  - ``_scheduler_routes`` SchedulerHttpRoutes (POST /api/schedule/run)
  - ``_http_server``      HttpApiServer       (internal API + MCP HTTP)
  - ``_web_server``       WebHttpServer       (Web UI aiohttp server)

Workgroup HTTP routes live on the WorkgroupManager itself
(``workgroup_mgr.routes``) so wiring stays inside the workgroup module.

Two-phase DI:
  Phase 1 — managers built with infrastructure (config / shared dicts /
            sibling refs that already exist).
  Phase 2 — late-bound siblings injected via setters
            (workgroup_mgr in ``set_workgroup_mgr``, scheduler in
            ``set_scheduler``, host_election in ``set_host_election``).
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from boxagent.agent.agent_manager import AgentManager
from boxagent.cluster.cluster_http_routes import ClusterHttpRoutes
from boxagent.cluster.cluster_rpc import ClusterRpc
from boxagent.cluster.host_election import HostElection
from boxagent.cluster.peer_service import PeerService
from boxagent.cluster.topology_service import TopologyService
from boxagent.gateway.http_api_server import HttpApiServer
from boxagent.scheduler.scheduler_http_routes import SchedulerHttpRoutes
from boxagent.transports.web.server import WebHttpServer
from boxagent.config import AppConfig
from boxagent.utils import default_config_dir, default_local_dir, default_workspace_dir
from boxagent.sessions import Storage
from boxagent.scheduler import Scheduler
from boxagent.workgroup import WorkgroupManager

logger = logging.getLogger(__name__)


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
    _workgroup_mgr: WorkgroupManager | None = field(default=None, repr=False)
    _bots: AgentManager | None = field(default=None, repr=False)
    _topology: TopologyService | None = field(default=None, repr=False)
    _peer: PeerService | None = field(default=None, repr=False)
    _cluster_rpc: ClusterRpc | None = field(default=None, repr=False)
    _cluster_routes: ClusterHttpRoutes | None = field(default=None, repr=False)
    _scheduler_routes: SchedulerHttpRoutes | None = field(default=None, repr=False)
    _http_server: HttpApiServer | None = field(default=None, repr=False)
    _web_server: WebHttpServer | None = field(default=None, repr=False)

    async def start(self) -> None:
        self._start_time = time.time()
        storage = Storage(local_dir=self.local_dir)
        self._storage = storage
        # Phase 1: build managers. AgentManager owns its bot-state dicts;
        # everyone else who needs to read them gets the dict by reference.
        self._bots = AgentManager(
            config=self.config,
            config_dir=self.config_dir,
            storage=storage,
            start_time=self._start_time,
        )
        self._topology = TopologyService(
            config=self.config,
            web_channels=self._bots.web_channels,
        )
        self._peer = PeerService(
            topology=self._topology,
            main_chat_id_provider=storage.get_or_create_main_chat_id,
        )
        self._cluster_rpc = ClusterRpc(topology=self._topology)
        self._cluster_routes = ClusterHttpRoutes(
            peer=self._peer, cluster_rpc=self._cluster_rpc,
        )
        self._web_server = WebHttpServer(
            config=self.config,
            local_dir=self.local_dir,
            storage=storage,
            web_channels=self._bots.web_channels,
            pools=self._bots.pools,
            topology=self._topology,
            cluster_rpc=self._cluster_rpc,
            cluster_routes=self._cluster_routes,
        )
        logger.info("Gateway starting (node=%s)", self.config.node_id or "(any)")

        # Web UI first so the page is reachable while the rest boots.
        await self._web_server.start()

        # Bots (incl. synthetic raw passthrough).
        await self._bots.start_all_for_node(self.config.node_id)

        # Workgroups.
        if self.config.workgroups:
            self._workgroup_mgr = WorkgroupManager(
                config=self.config.workgroups,
                config_dir=str(self.config_dir),
                node_id=self.config.node_id,
                local_dir=storage.local_dir,
                start_time=self._start_time,
                storage=storage,
                web_channels=self._bots.web_channels,
                _peer_provider=self._topology.build_peer_descriptors,
            )
            # Phase 2: topology + peer + web server now see workgroup_mgr.
            # (workgroup_mgr.routes ships with the manager; no separate setter.)
            self._topology.set_workgroup_mgr(self._workgroup_mgr)
            self._peer.set_workgroup_mgr(self._workgroup_mgr)
            self._web_server.set_workgroup_mgr(self._workgroup_mgr)
            await self._workgroup_mgr.start_all_for_node(self.config.node_id)

        # Scheduler + its HTTP route adapter.
        self._start_scheduler()

        # HttpApiServer needs workgroup_mgr.routes + scheduler_routes — built
        # now that both upstream deps exist (no Phase-2 setter needed).
        self._http_server = HttpApiServer(
            config=self.config,
            config_dir=self.config_dir,
            local_dir=self.local_dir,
            peer=self._peer,
            workgroup_routes=(self._workgroup_mgr.routes if self._workgroup_mgr else None),
            scheduler_routes=self._scheduler_routes,
            mcp_gateway_context=self,
        )
        await self._http_server.start()

        # Cluster: host election. host_priority list determines who is host
        # vs guest at runtime, with failover when primary disappears.
        if self.config.cluster_tunnel:
            self._host_election = HostElection(
                config=self.config,
                on_topology_change=self._topology.on_topology_change,
                bot_provider=self._topology.local_bot_descriptors,
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
        if self._http_server is not None:
            await self._http_server.stop()
            await self._http_server.stop_mcp()

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
            await self._bots.stop()

        # Workgroup resources
        if self._workgroup_mgr:
            await self._workgroup_mgr.stop()

        logger.info("Gateway stopped")
