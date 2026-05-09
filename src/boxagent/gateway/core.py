"""Gateway core — dataclass state, lifecycle, and shared helpers.

Mixin layout: see ``boxagent.gateway.__init__``. This module owns the
``@dataclass``-decorated ``_GatewayCore`` (fields + lifecycle + cluster
helpers); HTTP, peer, workgroup, and cluster-RPC handlers live in
sibling mixin modules.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from boxagent.agent.manager import AgentManager, _create_backend, _ensure_git_repo, sync_skills
from boxagent.cluster.peer import PeerService
from boxagent.cluster.topology import TopologyService
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
    _http_runner: web.AppRunner | None = field(default=None, repr=False)
    _host_election: object | None = field(default=None, repr=False)
    _start_time: float = 0.0
    _workgroup_mgr: WorkgroupManager | None = field(default=None, repr=False)
    _bots: AgentManager | None = field(default=None, repr=False)
    _topology: TopologyService | None = field(default=None, repr=False)
    _peer: PeerService | None = field(default=None, repr=False)

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
        logger.info("Gateway starting (node=%s)", self.config.node_id or "(any)")

        # Start Web UI first so the page is reachable while the rest boots.
        await self._start_web_http()

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
            # Phase 2: topology + peer now see workgroup_mgr.
            self._topology.set_workgroup_mgr(self._workgroup_mgr)
            self._peer.set_workgroup_mgr(self._workgroup_mgr)
            for workgroup_name, workgroup_config in self.config.workgroups.items():
                if not node_matches(workgroup_config.enabled_on_nodes, self.config.node_id):
                    logger.info("Workgroup '%s' skipped (enabled_on_nodes=%s, current=%s)", workgroup_name, workgroup_config.enabled_on_nodes, self.config.node_id)
                    continue
                await self._workgroup_mgr.start_workgroup(workgroup_name, workgroup_config)

        # Start scheduler
        self._start_scheduler()

        # Start HTTP API
        await self._start_http()

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
        await self._stop_web_http()
        await self._stop_http()
        await self._stop_mcp_http()

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


# ── Gateway: compose mixins on top of _GatewayCore ──

from boxagent.cluster.routes import ClusterRoutesMixin
from boxagent.cluster.rpc import ClusterRpcMixin
from boxagent.gateway.http_api import HttpApiMixin
from boxagent.transports.web.server import WebServerMixin
from boxagent.workgroup.routes import WorkgroupApiMixin


class Gateway(
    WebServerMixin,
    HttpApiMixin,
    WorkgroupApiMixin,
    ClusterRoutesMixin,
    ClusterRpcMixin,
    _GatewayCore,
):
    """Top-level Gateway. State + lifecycle live in ``_GatewayCore``;
    request handlers come from the mixins."""
    pass
