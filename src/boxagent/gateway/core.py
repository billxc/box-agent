"""Gateway core — dataclass state, lifecycle, and shared helpers.

Mixin layout: see ``boxagent.gateway.__init__``. This module owns the
``@dataclass``-decorated ``_GatewayCore`` (fields + lifecycle + cluster
helpers); HTTP, peer, workgroup, and cluster-RPC handlers live in
sibling mixin modules.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from boxagent.agent.claude_process import ClaudeProcess
from boxagent.channels.web import WebChannel
from boxagent.cluster import ClusterTunnel, GuestClient, GuestRegistry
from boxagent.config import AppConfig, BotConfig, node_matches
from boxagent.paths import default_config_dir, default_local_dir, default_workspace_dir
from boxagent.router import Router
from boxagent.sessions import SessionPool
from boxagent.sessions.raw_pool import RawSessionPool
from boxagent.scheduler import BotRef, Scheduler
from boxagent.sessions import Storage
from boxagent.watchdog import Watchdog
from boxagent.workgroup import WorkgroupManager

from aiohttp import web

logger = logging.getLogger(__name__)

_PEER_HEADER_RE = re.compile(
    r"^\[To:\s*(?P<target>[^\]]+)\]\s*\[From:\s*(?P<sender>[^\]]+)\]\s*\n?",
)


def _parse_peer_message(text: str) -> tuple[str, str, str]:
    """Parse ``[To: x] [From: y]\nbody`` → (target, sender, body).

    Returns ("", "", text) if the header is missing.
    """
    m = _PEER_HEADER_RE.match(text)
    if not m:
        return "", "", text
    return m.group("target").strip(), m.group("sender").strip(), text[m.end():]


def _supports_persistent_session(ai_backend: str) -> bool:
    """Whether a backend can resume a saved session after restart."""
    return ai_backend in ("claude-cli", "codex-cli", "codex-acp")


def _infer_platform(chat_id: str) -> str:
    """Best-effort guess for which channel a chat_id originated from."""
    if not chat_id:
        return "unknown"
    if chat_id.startswith("claude-"):
        return "claude"
    if chat_id.startswith("web-"):
        return "web"
    if chat_id.lstrip("-").isdigit():
        return "telegram"
    return "other"


def _create_backend(bot_cfg: BotConfig, session_id: str | None) -> object:
    """Instantiate the AI backend based on config."""
    from boxagent import gateway as _gw_pkg
    if bot_cfg.ai_backend == "codex-acp":
        from boxagent.agent.acp_process import ACPProcess

        return ACPProcess(
            workspace=bot_cfg.workspace,
            session_id=session_id,
            model=bot_cfg.model,
            agent=bot_cfg.agent,
            bot_name=bot_cfg.name,
        )
    if bot_cfg.ai_backend == "codex-cli":
        from boxagent.agent.codex_process import CodexProcess

        return CodexProcess(
            workspace=bot_cfg.workspace,
            session_id=session_id,
            model=bot_cfg.model,
            agent=bot_cfg.agent,
            bot_name=bot_cfg.name,
            yolo=bot_cfg.yolo,
        )
    return _gw_pkg.ClaudeProcess(
        workspace=bot_cfg.workspace,
        session_id=session_id,
        model=bot_cfg.model,
        agent=bot_cfg.agent,
        bot_name=bot_cfg.name,
        yolo=bot_cfg.yolo,
    )


def _ensure_git_repo(workspace: Path) -> bool:
    """Ensure workspace is a git repo (minimal skeleton).

    Claude Code uses git root to locate ``.claude/skills/``.  If the
    workspace lives inside a parent git repo the skills directory won't
    be found.  Creating a minimal ``.git`` makes the workspace its own
    git root so skill discovery works correctly.

    Return *True* if a new ``.git`` was created.
    """
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    git_dir = workspace / ".git"
    if git_dir.exists():
        return False
    git_dir.mkdir(exist_ok=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "objects").mkdir(exist_ok=True)
    (git_dir / "refs").mkdir(exist_ok=True)
    (git_dir / "refs" / "heads").mkdir(exist_ok=True)
    logger.info("Created minimal .git in %s (Claude Code needs git root to discover skills)", workspace)
    return True


def sync_skills(
    workspace: str,
    extra_skill_dirs: list[str],
    ai_backend: str = "claude-cli",
) -> list[str]:
    """Symlink skill subdirs into the backend-specific skills directory.

    - Claude-style backends: {workspace}/.claude/skills/
    - Codex ACP backend: {workspace}/.agents/skills/
    """
    skills_root = ".agents" if ai_backend in ("codex-acp", "codex-cli") else ".claude"
    skills_dir = Path(workspace) / skills_root / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    # Clean up broken symlinks
    for entry in skills_dir.iterdir():
        if entry.is_symlink() and not entry.exists():
            logger.info("Removing broken skill symlink: %s", entry)
            entry.unlink()

    linked = []
    for src_dir in extra_skill_dirs:
        src_path = Path(src_dir).expanduser().resolve()
        if not src_path.is_dir():
            logger.warning("Skill dir not found: %s", src_path)
            continue
        for child in sorted(src_path.iterdir()):
            if not child.is_dir():
                continue
            link = skills_dir / child.name
            if link.is_symlink():
                link.unlink()
            elif link.exists():
                continue  # don't overwrite real dirs
            link.symlink_to(child)
            linked.append(child.name)
    return linked


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
    _cli_processes: dict[str, object] = field(
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
    _guest_registry: GuestRegistry | None = field(default=None, repr=False)
    _guest_client: GuestClient | None = field(default=None, repr=False)
    _cluster_tunnel: ClusterTunnel | None = field(default=None, repr=False)
    _host_election: object | None = field(default=None, repr=False)
    _start_time: float = 0.0
    _workgroup_mgr: WorkgroupManager | None = field(default=None, repr=False)

    async def start(self) -> None:
        self._start_time = time.time()
        self._storage = Storage(local_dir=self.local_dir)
        logger.info("Gateway starting (node=%s)", self.config.node_id or "(any)")

        # Start Web UI first so the page is reachable while the rest boots.
        await self._start_web_http()

        # Start each bot
        for name, bot_cfg in self.config.bots.items():
            if not node_matches(bot_cfg.enabled_on_nodes, self.config.node_id):
                logger.info("Bot '%s' skipped (enabled_on_nodes=%s, current=%s)", name, bot_cfg.enabled_on_nodes, self.config.node_id)
                continue
            await self._start_bot(name, bot_cfg)

        # Register the synthetic ``raw`` bot (web-only passthrough).
        await self._start_raw_bot()

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
                _peer_provider=self._build_peer_descriptors,
            )
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
            self._host_election = HostElection(config=self.config, gateway=self)
            await self._host_election.start()

        logger.info(
            "Gateway ready: %d bot(s) active", len(self.config.bots)
        )

    async def _start_bot(self, name: str, bot_cfg: BotConfig) -> None:
        session_id = None
        if _supports_persistent_session(bot_cfg.ai_backend):
            saved = self._storage.load_session(name)
            if isinstance(saved, dict):
                session_id = saved.get("session_id")
            elif isinstance(saved, str):
                session_id = saved

        cli_process = _create_backend(bot_cfg, session_id)
        cli_process.start()
        self._cli_processes[name] = cli_process

        # Create session pool
        def _factory():
            return _create_backend(bot_cfg, None)

        pool = SessionPool(
            size=3,
            default_model=bot_cfg.model,
            default_workspace=bot_cfg.workspace,
            storage=self._storage,
            bot_name=name,
        )
        pool.start(_factory)
        self._pools[name] = pool

        # Ensure workspace is a git repo (Claude Code uses git root to find skills)
        ws_path = Path(bot_cfg.workspace)
        git_created = _ensure_git_repo(ws_path)

        # Sync skill symlinks
        linked: list[str] = []
        if bot_cfg.extra_skill_dirs:
            linked = sync_skills(
                bot_cfg.workspace,
                bot_cfg.extra_skill_dirs,
                bot_cfg.ai_backend,
            )
            logger.info("Bot '%s' synced %d skill(s): %s", name, len(linked), linked)

        display_name = bot_cfg.display_name or name

        # Primary channel for Router, notifications, watchdog
        primary_channel = None

        # --- Telegram channel ---
        if bot_cfg.telegram_token:
            from boxagent.channels.telegram import TelegramChannel
            channel = TelegramChannel(
                token=bot_cfg.telegram_token,
                allowed_users=bot_cfg.allowed_users,
                tool_calls_display=bot_cfg.display_tool_calls,
            )
            primary_channel = channel
            self._channels[name] = channel

        from boxagent import gateway as _gw_pkg
        router = _gw_pkg.Router(
            cli_process=cli_process,
            channel=primary_channel,
            allowed_users=bot_cfg.allowed_users,
            storage=self._storage,
            pool=pool,
            bot_name=name,
            display_name=display_name,
            config_dir=str(self.config_dir),
            node_id=self.config.node_id,
            local_dir=self._storage.local_dir if self._storage else None,
            start_time=self._start_time,
            workspace=bot_cfg.workspace,
            extra_skill_dirs=bot_cfg.extra_skill_dirs,
            ai_backend=bot_cfg.ai_backend,
            on_backend_switched=self._on_backend_switched,
            has_peer_channel=False,  # regular bots don't peer; only workgroup admins do
            telegram_token=bot_cfg.telegram_token,
        )

        # Wire Telegram channel to router
        if name in self._channels:
            router._channels["telegram"] = self._channels[name]
            self._channels[name].on_message = router.handle_message
            await self._channels[name].start()

        # Peer messaging: regular bots have no send_to_peer capability.
        # Workgroup admins get it via WorkgroupManager.start_workgroup, routed
        # through Gateway.send_peer (cluster-aware: local in-process / remote RPC).

        # --- Web channel (optional) ---
        if bot_cfg.web_enabled:
            web_channel = WebChannel(bot_name=name)
            web_channel.on_message = router.handle_message
            self._web_channels[name] = web_channel
            router._channels["web"] = web_channel
            logger.info("Bot '%s' web channel enabled", name)

        self._routers[name] = router

        # Notify user that bot is online
        import datetime
        skill_count = len(linked)
        channels_active = []
        if bot_cfg.telegram_token:
            channels_active.append("telegram")
        if bot_cfg.web_enabled:
            channels_active.append("web")
        info_lines = [
            f"\U0001f7e2 *{display_name}* is online",
            f"node: `{self.config.node_id or '(any)'}`",
            f"model: `{bot_cfg.model or 'default'}`",
            f"backend: `{bot_cfg.ai_backend}`",
            f"workspace: `{bot_cfg.workspace}`",
            f"channels: {', '.join(channels_active)}",
            f"skills: {skill_count}",
            f"time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}",
        ]
        if git_created:
            info_lines.append("⚠️ workspace was not a git repo, created .git for skill discovery")
        notify_text = "\n".join(info_lines)

        # Telegram: send asynchronously so the next bot's startup isn't blocked on this HTTPS call
        tg_chat_id = str(bot_cfg.telegram_allowed_users[0]) if bot_cfg.telegram_token and bot_cfg.telegram_allowed_users else ""
        if tg_chat_id and name in self._channels:
            async def _send_tg_notify(ch=self._channels[name], chat_id=tg_chat_id, text=notify_text, bot_name=name):
                try:
                    await ch.send_text(chat_id, text)
                except Exception as e:
                    logger.warning("Failed to send Telegram startup notification for '%s': %s", bot_name, e)
            asyncio.create_task(_send_tg_notify())

        async def restart_bot(n=name, bc=bot_cfg):
            await self._restart_bot(n, bc)

        # Watchdog chat_id for error notifications
        wd_chat_id = tg_chat_id

        wd = _gw_pkg.Watchdog(
            cli_process=cli_process,
            channel=primary_channel,
            chat_id=wd_chat_id,
            bot_name=name,
            on_restart=restart_bot,
            pool=pool,
        )
        task = asyncio.create_task(wd.run_forever())
        self._watchdogs[name] = wd
        self._watchdog_tasks.append(task)

        logger.info("Bot '%s' started (session=%s)", name, session_id)

    # ---- raw virtual bot ----

    def _raw_backend_factory(self, *, backend: str, workspace: str, model: str,
                             session_id: str | None, bot_name: str) -> object:
        """Spawn a fresh per-chat backend process for the raw bot."""
        cfg = BotConfig(
            name=bot_name,
            ai_backend=backend or "claude-cli",
            workspace=workspace or "",
            model=model or "",
            yolo=True,           # raw bot: no permission prompts (web-only)
            passthrough=True,
        )
        return _create_backend(cfg, session_id)

    async def _start_raw_bot(self) -> None:
        """Register the synthetic ``raw`` bot.

        ``raw`` is a web-only passthrough bot: no Telegram, no
        BoxAgent context/MCP injection, per-chat backend chosen at resume
        time. Used as a clean ``--resume`` shell for native claude / codex
        sessions.
        """
        name = "raw"
        bot_cfg = BotConfig(
            name=name,
            ai_backend="claude-cli",   # placeholder; real backend per-chat
            workspace="",
            display_name="Raw passthrough",
            passthrough=True,
            web_enabled=True,
            yolo=True,
        )
        self.config.bots[name] = bot_cfg

        pool = RawSessionPool(
            storage=self._storage,
            bot_name=name,
            backend_factory=self._raw_backend_factory,
        )
        pool.start()
        self._pools[name] = pool

        # Stub cli_process (Router requires one); never started.
        stub = ClaudeProcess(
            workspace="/tmp",
            session_id=None,
            model="",
            agent="",
            bot_name=name,
            yolo=True,
        )
        self._cli_processes[name] = stub

        router = Router(
            cli_process=stub,
            channel=None,
            allowed_users=[],
            storage=self._storage,
            pool=pool,
            bot_name=name,
            display_name=bot_cfg.display_name,
            config_dir=str(self.config_dir),
            node_id=self.config.node_id,
            local_dir=self._storage.local_dir if self._storage else None,
            start_time=self._start_time,
            workspace="",
            extra_skill_dirs=[],
            ai_backend="claude-cli",
            on_backend_switched=self._on_backend_switched,
            has_peer_channel=False,
            telegram_token="",
            passthrough=True,
        )

        web_channel = WebChannel(bot_name=name)
        web_channel.on_message = router.handle_message
        self._web_channels[name] = web_channel
        router._channels["web"] = web_channel

        self._routers[name] = router
        logger.info("Bot 'raw' (passthrough, web-only) registered")

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
                cli_process=self._cli_processes[name],
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
        logger.info("Scheduler started (file=%s)", schedules_file)

    async def _restart_bot(self, name: str, bot_cfg: BotConfig) -> None:
        """Restart a dead backend process."""
        old_cli = self._cli_processes.get(name)
        session_id = None
        if old_cli and _supports_persistent_session(bot_cfg.ai_backend):
            session_id = old_cli.session_id
        if old_cli:
            try:
                await old_cli.stop()
            except Exception:
                pass

        new_cli = _create_backend(bot_cfg, session_id)
        new_cli.start()
        self._cli_processes[name] = new_cli

        # Re-sync skill symlinks
        if bot_cfg.extra_skill_dirs:
            sync_skills(
                bot_cfg.workspace,
                bot_cfg.extra_skill_dirs,
                bot_cfg.ai_backend,
            )

        # Update router reference
        if name in self._routers:
            self._routers[name].cli_process = new_cli

        # Update scheduler reference
        if self._scheduler and name in self._scheduler.bot_refs:
            self._scheduler.bot_refs[name].cli_process = new_cli
            self._scheduler.bot_refs[name].telegram_token = bot_cfg.telegram_token

        # Update watchdog reference
        if name in self._watchdogs:
            self._watchdogs[name].cli_process = new_cli

        logger.info("Bot '%s' backend restarted", name)

    async def _on_backend_switched(self, bot_name: str, new_cli: object, new_backend: str) -> None:
        """Called by Router after /backend switch — sync external references."""
        self._cli_processes[bot_name] = new_cli
        if self._scheduler and bot_name in self._scheduler.bot_refs:
            self._scheduler.bot_refs[bot_name].cli_process = new_cli
            self._scheduler.bot_refs[bot_name].ai_backend = new_backend
        if bot_name in self._watchdogs:
            self._watchdogs[bot_name].cli_process = new_cli
        logger.info("Bot '%s' backend switched to %s (refs synced)", bot_name, new_backend)

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

    def _local_bot_descriptors(self) -> list[dict]:
        """List of {name, display_name, backend, model, kind} for everything web-enabled here."""
        out: list[dict] = []
        for name in self._web_channels:
            cfg = self.config.bots.get(name)
            workgroup = self.config.workgroups.get(name)
            if cfg is not None:
                out.append({
                    "name": name,
                    "display_name": cfg.display_name or name,
                    "backend": cfg.ai_backend,
                    "model": cfg.model,
                    "kind": "bot",
                })
            elif workgroup is not None:
                out.append({
                    "name": name,
                    "display_name": workgroup.display_name or name,
                    "backend": workgroup.ai_backend,
                    "model": workgroup.model,
                    "kind": "workgroup",
                })
        return out

    def _build_peer_descriptors(self, exclude: str = "") -> list[dict]:
        """List all workgroup admins reachable from this node, excluding *exclude*.

        Sources combined:
        - Local workgroups (from ``self._workgroup_mgr.routers``)
        - Remote workgroup-kind bots from connected guests
          (``self._guest_registry.list_bots()``)
        - Remote workgroup-kind bots from disconnected-but-known guests
          (``self._guest_registry.history``) — flagged ``online=False``
        - On a guest (no local registry): ``self._guest_client.remote_peers``
          pushed by host via ``peers_snapshot`` frames.

        Each entry: ``{name, machine, online, kind, description}``. Used by
        Router.get_peers → AgentEnv.peers → context block; admin AI uses the
        *name* field as the ``send_to_peer(target=…)`` argument.
        """
        out: list[dict] = []
        if self._workgroup_mgr is not None:
            for name in self._workgroup_mgr.routers:
                if name == exclude:
                    continue
                if name not in self.config.workgroups:
                    continue  # routers also holds specialists; only workgroup names here
                workgroup = self.config.workgroups[name]
                out.append({
                    "name": name,
                    "machine": "local",
                    "online": True,
                    "kind": "workgroup",
                    "description": workgroup.display_name or "",
                })
        if self._guest_registry is not None:
            for machine_id, bot in self._guest_registry.list_bots():
                if bot.kind != "workgroup" or bot.name == exclude:
                    continue
                out.append({
                    "name": bot.name,
                    "machine": machine_id,
                    "online": True,
                    "kind": "workgroup",
                    "description": bot.display_name or "",
                })
            seen = {(p["name"], p["machine"]) for p in out}
            for machine_id, info in (self._guest_registry.history or {}).items():
                for b in info.get("bots") or []:
                    if b.get("kind") != "workgroup":
                        continue
                    name = b.get("name") or ""
                    if name == exclude or (name, machine_id) in seen:
                        continue
                    out.append({
                        "name": name,
                        "machine": machine_id,
                        "online": False,
                        "kind": "workgroup",
                        "description": b.get("display_name") or "",
                    })
        elif self._guest_client is not None:
            # Guest mode: registry is None, but host pushes peers_snapshot
            # frames containing the cross-cluster workgroup peer list.
            for p in self._guest_client.remote_peers:
                if not isinstance(p, dict):
                    continue
                if p.get("name") == exclude:
                    continue
                out.append({
                    "name": p.get("name", ""),
                    "machine": p.get("machine", ""),
                    "online": bool(p.get("online", True)),
                    "kind": p.get("kind", "workgroup"),
                    "description": p.get("description", ""),
                })
        return out

    async def _push_peers_snapshot_to_sats(self, changed_machine_id: str | None) -> None:
        """Send each connected guest a `peers_snapshot` frame so its admin
        can see workgroups elsewhere in the cluster.

        Triggered by GuestRegistry on hello / bots_update / disconnect.
        Each guest receives a list filtered to exclude its own workgroup-kind
        bots (so it doesn't see itself as a peer).

        ``changed_machine_id`` is just informational (which guest's state moved);
        we always re-broadcast to everyone since one guest's change affects what
        the others can route to.
        """
        if self._guest_registry is None:
            return
        # Collect per-guest exclusion sets up front (avoid recompute per send).
        for machine_id, sess in list(self._guest_registry.sessions.items()):
            self_workgroup_names = {
                b.name for b in sess.bots if b.kind == "workgroup"
            }
            peers: list[dict] = []
            # Host's own local workgroups
            if self._workgroup_mgr is not None:
                for wg_name in self._workgroup_mgr.routers:
                    if wg_name in self_workgroup_names:
                        continue
                    if wg_name not in self.config.workgroups:
                        continue
                    workgroup = self.config.workgroups[wg_name]
                    peers.append({
                        "name": wg_name,
                        "machine": self.config.node_id or "host",
                        "online": True,
                        "kind": "workgroup",
                        "description": workgroup.display_name or "",
                    })
            # Other sats' workgroup-kind bots
            for other_mid, other_bot in self._guest_registry.list_bots():
                if other_mid == machine_id:
                    continue  # don't tell a guest about itself
                if other_bot.kind != "workgroup":
                    continue
                if other_bot.name in self_workgroup_names:
                    continue
                peers.append({
                    "name": other_bot.name,
                    "machine": other_mid,
                    "online": True,
                    "kind": "workgroup",
                    "description": other_bot.display_name or "",
                })
            try:
                await sess.ws.send_json({"type": "peers_snapshot", "peers": peers})
            except Exception as e:
                logger.warning("peers_snapshot push to %s failed: %s", machine_id, e)

    async def _on_topology_change(self, changed_machine_id: str | None) -> None:
        """Single hook for GuestRegistry topology events. Fans out to both
        the workgroup peers snapshot and the cluster machines snapshot so each
        guest keeps an up-to-date view for its own webui."""
        await self._push_peers_snapshot_to_sats(changed_machine_id)
        await self._push_machines_snapshot_to_sats(changed_machine_id)

    def _collect_machines(self) -> list[dict]:
        """Build the same machine list `_handle_web_machines` returns. Pure
        helper so the snapshot pusher and the HTTP handler share one source.

        Host node only — sats don't run a registry. Returns host's local
        machine first, then every connected/known guest.
        """
        local_mid = self._local_machine_id()
        local_role = self._local_role()
        machines: list[dict] = [{
            "machine_id": local_mid,
            "online": True,
            "role": local_role,
            "self": True,
            "host_index": self.config.my_host_index,
            "bots": self._local_bot_descriptors(),
            "last_seen": time.time(),
        }]
        if self._guest_registry is not None:
            for m in self._guest_registry.list_machines():
                m["role"] = "guest"
                m["self"] = False
                mid = m.get("machine_id") or ""
                m["host_index"] = self.config.host_priority.index(mid) if mid in self.config.host_priority else -1
                machines.append(m)
        return machines

    async def _push_machines_snapshot_to_sats(self, changed_machine_id: str | None) -> None:
        """Push the full cluster machine list to every connected guest so each
        guest's webui can render the same sidebar the host shows. Per-guest the
        snapshot is filtered to drop the receiving guest's own row (the guest
        already renders itself from local state with ``self: true``).
        """
        if self._guest_registry is None:
            return
        all_machines = self._collect_machines()
        for machine_id, sess in list(self._guest_registry.sessions.items()):
            filtered = [m for m in all_machines if m.get("machine_id") != machine_id]
            try:
                await sess.ws.send_json({"type": "machines_snapshot", "machines": filtered})
            except Exception as e:
                logger.warning("machines_snapshot push to %s failed: %s", machine_id, e)

    def _local_machine_id(self) -> str:
        return self.config.machine_id or self.config.node_id or "local"

    def _local_role(self) -> str:
        """Current cluster role of this node — driven by HostElection."""
        rm = self._host_election
        if rm is None:
            return "single"
        state = getattr(rm, "state", "init")
        if state == "host":
            return "host"
        if state == "guest":
            return "guest"
        # init / standalone / unknown
        if self.config.cluster_tunnel:
            return "guest"  # default optimistic — manager hasn't promoted yet
        return "single"

    def _remote_session_for(self, machine_id: str, bot: str):
        """Return GuestSession owning `bot` on `machine_id`, or None."""
        if self._guest_registry is None:
            return None
        if self._guest_registry.get_bot(machine_id, bot) is None:
            return None
        return self._guest_registry.get(machine_id)

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

        # Stop guest WS client (if still running — host election normally owns this)
        if self._guest_client is not None:
            try:
                await self._guest_client.stop()
            except Exception as e:
                logger.error("Error stopping guest client: %s", e)
            self._guest_client = None

        # Stop cluster devtunnel (host only — likewise normally torn down by role manager)
        if self._cluster_tunnel is not None:
            try:
                await self._cluster_tunnel.stop()
            except Exception as e:
                logger.error("Error stopping cluster tunnel: %s", e)
            self._cluster_tunnel = None

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

        for name, cli_process in self._cli_processes.items():
            try:
                # Save session before stopping
                if self._storage and cli_process.session_id:
                    self._storage.save_session(name, cli_process.session_id)
                await cli_process.stop()
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
