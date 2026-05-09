"""Gateway core — composition root.

Owns the dataclass state and the start/stop wiring; all behavior lives in
8 composed managers built in ``start()``:

  - ``_bots``             AgentManager        (per-bot lifecycle)
  - ``_topology``         TopologyService     (cluster identity / peer list)
  - ``_peer``             PeerService         (cross-admin peer messaging)
  - ``_cluster_rpc``      ClusterRpc          (host↔guest HTTP/SSE proxying)
  - ``_cluster_routes``   ClusterHttpRoutes   (cluster routes on web port)
  - ``_workgroup_routes`` WorkgroupHttpRoutes (workgroup HTTP handlers)
  - ``_http_server``      HttpApiServer       (internal API + MCP HTTP)
  - ``_web_server``       WebHttpServer       (Web UI aiohttp server)

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

from boxagent.agent.agent_manager import AgentManager, _create_backend, _ensure_git_repo, sync_skills
from boxagent.cluster.cluster_http_routes import ClusterHttpRoutes
from boxagent.cluster.cluster_rpc import ClusterRpc
from boxagent.cluster.peer_service import PeerService
from boxagent.cluster.topology_service import TopologyService
from boxagent.gateway.http_api_server import HttpApiServer
from boxagent.scheduler.scheduler_http_routes import SchedulerHttpRoutes
from boxagent.transports.web.server import WebHttpServer
from boxagent.transports.web import WebChannel
from boxagent.cluster import ClusterTunnel, GuestClient, GuestRegistry
from boxagent.config import AppConfig, node_matches
from boxagent.utils import default_config_dir, default_local_dir, default_workspace_dir
from boxagent.router import Router
from boxagent.sessions import SessionPool
from boxagent.scheduler import BotRef, Scheduler
from boxagent.sessions import Storage
from boxagent.watchdog import Watchdog
from boxagent.workgroup import WorkgroupManager

from aiohttp import web

logger = logging.getLogger(__name__)


@dataclass
class _GatewayCore:
    config: AppConfig
    config_dir: Path = field(default_factory=default_config_dir)
    local_dir: Path = field(default_factory=default_local_dir)
    _channels: dict[str, object] = field(
        default_factory=dict, repr=False
    )
    _web_channels: dict[str, WebChannel] = field(
        default_factory=dict, repr=False
    )
    _backends: dict[str, object] = field(
        default_factory=dict, repr=False
    )
    _pools: dict[str, SessionPool] = field(
        default_factory=dict, repr=False
    )
    _routers: dict[str, Router] = field(default_factory=dict, repr=False)
    _storage: Storage | None = field(default=None, repr=False)
    _session_meta_cache: dict[str, dict] = field(default_factory=dict, repr=False)
    _watchdogs: dict[str, Watchdog] = field(default_factory=dict, repr=False)
    _watchdog_tasks: list[asyncio.Task] = field(
        default_factory=list, repr=False
    )
    _scheduler: Scheduler | None = field(default=None, repr=False)
    _scheduler_task: asyncio.Task | None = field(default=None, repr=False)
    _host_election: object | None = field(default=None, repr=False)
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

    # Public read-only views into HostElection-owned components. Read sites
    # use these instead of reaching into ``_host_election.registry`` directly,
    # so HostElection-vs-None checks stay in one place.
    @property
    def guest_registry(self) -> "GuestRegistry | None":
        he = self._host_election
        return he.registry if he is not None else None

    @property
    def guest_client(self) -> "GuestClient | None":
        he = self._host_election
        return he.client if he is not None else None

    @property
    def cluster_tunnel(self) -> "ClusterTunnel | None":
        he = self._host_election
        return he.tunnel if he is not None else None

    async def start(self) -> None:
        self._start_time = time.time()
        self._storage = Storage(local_dir=self.local_dir)
        # Phase 1: AgentManager + TopologyService get their infrastructure
        # (storage + shared dicts that other managers also read).
        self._bots = AgentManager(
            config=self.config,
            config_dir=self.config_dir,
            storage=self._storage,
            start_time=self._start_time,
            backends=self._backends,
            pools=self._pools,
            routers=self._routers,
            channels=self._channels,
            web_channels=self._web_channels,
            watchdogs=self._watchdogs,
            watchdog_tasks=self._watchdog_tasks,
        )
        self._topology = TopologyService(
            config=self.config,
            web_channels=self._web_channels,
        )
        self._peer = PeerService(
            topology=self._topology,
            main_chat_id_provider=self._get_or_create_main_chat_id,
        )
        self._cluster_rpc = ClusterRpc(topology=self._topology)
        self._cluster_routes = ClusterHttpRoutes(
            peer=self._peer, cluster_rpc=self._cluster_rpc,
        )
        self._web_server = WebHttpServer(
            config=self.config,
            local_dir=self.local_dir,
            storage=self._storage,
            web_channels=self._web_channels,
            pools=self._pools,
            session_meta_cache=self._session_meta_cache,
            topology=self._topology,
            cluster_rpc=self._cluster_rpc,
            cluster_routes=self._cluster_routes,
        )
        logger.info("Gateway starting (node=%s)", self.config.node_id or "(any)")

        # Start Web UI first so the page is reachable while the rest boots.
        await self._web_server.start()

        # Start each bot
        for name, bot_cfg in self.config.bots.items():
            if not node_matches(bot_cfg.enabled_on_nodes, self.config.node_id):
                logger.info("Bot '%s' skipped (enabled_on_nodes=%s, current=%s)", name, bot_cfg.enabled_on_nodes, self.config.node_id)
                continue
            await self._bots.start_bot(name, bot_cfg)

        # Register the synthetic ``raw`` bot (web-only passthrough).
        await self._bots.start_raw_bot()

        # Start workgroups
        if self.config.workgroups:
            self._workgroup_mgr = WorkgroupManager(
                config=self.config.workgroups,
                config_dir=str(self.config_dir),
                node_id=self.config.node_id,
                local_dir=self._storage.local_dir if self._storage else None,
                start_time=self._start_time,
                storage=self._storage,
                web_channels=self._web_channels,
                _create_backend=_create_backend,
                _ensure_git_repo=_ensure_git_repo,
                _sync_skills=sync_skills,
                _peer_provider=self._topology.build_peer_descriptors,
            )
            # Phase 2: topology + peer + web server now see workgroup_mgr.
            # (workgroup_mgr.routes ships with the manager; no separate setter.)
            self._topology.set_workgroup_mgr(self._workgroup_mgr)
            self._peer.set_workgroup_mgr(self._workgroup_mgr)
            self._web_server.set_workgroup_mgr(self._workgroup_mgr)
            for workgroup_name, workgroup_config in self.config.workgroups.items():
                if not node_matches(workgroup_config.enabled_on_nodes, self.config.node_id):
                    logger.info("Workgroup '%s' skipped (enabled_on_nodes=%s, current=%s)", workgroup_name, workgroup_config.enabled_on_nodes, self.config.node_id)
                    continue
                await self._workgroup_mgr.start_workgroup(workgroup_name, workgroup_config)

        # Start scheduler
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

        # Cluster: kick off host election (host_priority list determines who
        # is host vs guest at runtime, with failover when primary disappears).
        if self.config.cluster_tunnel:
            from boxagent.cluster.host_election import HostElection
            self._host_election = HostElection(
                config=self.config,
                on_topology_change=self._topology.on_topology_change,
                bot_provider=self._topology.local_bot_descriptors,
            )
            # Phase 2: topology can now resolve guest_registry / guest_client
            # (and thus serve _collect_machines / build_peer_descriptors host data).
            self._topology.set_host_election(self._host_election)
            await self._host_election.start()

        logger.info(
            "Gateway ready: %d bot(s) active", len(self.config.bots)
        )


    def _start_scheduler(self) -> None:
        """Create and start the Scheduler after all active bots are online."""
        schedules_file = self.config_dir / "schedules.yaml"
        bot_refs: dict[str, BotRef] = {}
        for name in self._routers:
            if name == "raw":
                continue  # synthetic web-only bot, never a scheduler target
            bot_cfg = self.config.bots[name]
            chat_id = str(bot_cfg.allowed_users[0]) if bot_cfg.allowed_users else ""
            primary_channel = self._channels.get(name)
            bot_refs[name] = BotRef(
                backend=self._backends[name],
                channel=primary_channel,
                chat_id=chat_id,
                ai_backend=bot_cfg.ai_backend,
                telegram_token=bot_cfg.telegram_token,
            )

        self._scheduler = Scheduler(
            schedules_file=schedules_file,
            node_id=self.config.node_id,
            bot_refs=bot_refs,
            telegram_bots=self.config.telegram_bots,
            default_workspace=str(default_workspace_dir(self.config_dir)),
            local_dir=str(self.local_dir),
        )
        self._scheduler_task = asyncio.create_task(self._scheduler.run_forever())
        # Phase 2 of two-phase DI: scheduler exists now, inject into AgentManager
        # so restart_bot / on_backend_switched can sync scheduler.bot_refs.
        if self._bots is not None:
            self._bots.set_scheduler(self._scheduler)
        # Build the scheduler's HTTP route adapter alongside the scheduler.
        self._scheduler_routes = SchedulerHttpRoutes(
            config=self.config,
            config_dir=self.config_dir,
            scheduler=self._scheduler,
        )
        logger.info("Scheduler started (file=%s)", schedules_file)

    @property
    def _api_port_file(self) -> Path:
        return self.local_dir / "api-port.txt"

    @property
    def _mcp_port_file(self) -> Path:
        return self.local_dir / "mcp-port.txt"

    @property
    def _web_port_file(self) -> Path:
        return self.local_dir / "web-port.txt"

    def _clear_http_artifacts(self) -> None:
        """Remove runtime HTTP endpoint artifacts left by a previous run."""
        for f in (self._api_port_file, self._mcp_port_file,
                  self._web_port_file,
                  self.local_dir / "api.sock"):
            if f.exists():
                f.unlink(missing_ok=True)

    def _get_or_create_main_chat_id(self, bot: str) -> str:
        """Return the persisted main chat_id for a bot, minting one if unset.

        Used for heartbeat ticks and incoming peer messages so they always
        land in the admin's designated main session. Web UI can override
        via /api/sessions/set_main.
        """
        if self._storage is None:
            return f"main-{bot}-{int(time.time())}"
        cid = self._storage.get_main_chat_id(bot)
        if cid:
            return cid
        cid = f"main-{bot}-{int(time.time())}"
        self._storage.set_main_chat_id(bot, cid)
        return cid

    async def stop(self) -> None:
        logger.info("Gateway shutting down...")

        # Release listening ports first so a restarting process can re-bind immediately,
        # regardless of how long the rest of the shutdown takes.
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

        # Cancel watchdogs
        for task in self._watchdog_tasks:
            task.cancel()

        # Await all cancelled background tasks to prevent resource leaks
        bg_tasks = list(self._watchdog_tasks)
        if self._scheduler_task:
            bg_tasks.append(self._scheduler_task)
        if bg_tasks:
            await asyncio.gather(*bg_tasks, return_exceptions=True)
        self._watchdog_tasks.clear()
        self._scheduler_task = None

        for name, ch in self._channels.items():
            try:
                await ch.stop()
            except Exception as e:
                logger.error("Error stopping channel %s: %s", name, e)

        for name, ch in self._web_channels.items():
            try:
                await ch.stop()
            except Exception as e:
                logger.error("Error stopping web channel %s: %s", name, e)

        for name, backend in self._backends.items():
            try:
                # Save session before stopping
                if self._storage and backend.session_id:
                    self._storage.save_session(name, backend.session_id)
                await backend.stop()
            except Exception as e:
                logger.error("Error stopping CLI %s: %s", name, e)

        for name, pool in self._pools.items():
            try:
                await pool.stop()
            except Exception as e:
                logger.error("Error stopping pool %s: %s", name, e)

        # Stop workgroup resources
        if self._workgroup_mgr:
            await self._workgroup_mgr.stop()

        logger.info("Gateway stopped")


# ── Gateway: composition root (no mixins) ──


class Gateway(_GatewayCore):
    """Top-level Gateway. All behavior lives in composed managers
    (``self._bots``, ``self._topology``, ``self._peer``, ``self._cluster_rpc``,
    ``self._cluster_routes``, ``self._workgroup_routes``, ``self._http_server``,
    ``self._web_server``); ``_GatewayCore`` only owns the dataclass state and
    the start/stop wiring."""
    pass
