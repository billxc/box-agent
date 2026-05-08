"""Gateway — orchestrates all components."""

import asyncio
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from boxagent.agent.claude_process import ClaudeProcess
from boxagent.channels.base import IncomingMessage
from boxagent.channels.web import WebChannel
from boxagent.cluster import ClusterTunnel, GuestClient, GuestRegistry
from boxagent.config import AppConfig, BotConfig, WorkgroupConfig, node_matches
from boxagent.paths import default_config_dir, default_local_dir, default_workspace_dir
from boxagent.router import Router
from boxagent.sessions import SessionPool
from boxagent.sessions.raw_pool import RawSessionPool
from boxagent.scheduler import BotRef, Scheduler, load_schedules
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
    return ClaudeProcess(
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
class Gateway:
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
    _role_manager: object | None = field(default=None, repr=False)
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

        # Cluster: kick off the role manager (host_priority list determines who
        # is host vs guest at runtime, with failover when primary disappears).
        if self.config.cluster_tunnel:
            from boxagent.cluster.role_manager import ClusterRoleManager
            self._role_manager = ClusterRoleManager(config=self.config, gateway=self)
            await self._role_manager.start()

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

        router = Router(
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
            info_lines.append("\u26a0\ufe0f workspace was not a git repo, created .git for skill discovery")
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

        wd = Watchdog(
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

    def _clear_http_artifacts(self) -> None:
        """Remove runtime HTTP endpoint artifacts left by a previous run."""
        for f in (self._api_port_file, self._mcp_port_file,
                  self._web_port_file,
                  self.local_dir / "api.sock"):
            if f.exists():
                f.unlink(missing_ok=True)

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

    @property
    def _web_port_file(self) -> Path:
        return self.local_dir / "web-port.txt"

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
        web_static = _Path(__file__).parent / "web" / "static"
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

    async def _dispatch_machine_request(
        self,
        machine: str,
        method: str,
        path: str,
        request: web.Request,
        body: dict | None = None,
    ) -> web.Response | None:
        """If `machine` is remote, forward and return the response.
        Returns None when the request targets the local node (caller should
        continue with its local handling).

        Host role: forward via GuestSession (existing host→guest RPC).
        Guest role: forward via GuestClient (new guest→host RPC); the
        host then dispatches locally or proxies onward to the right guest.
        """
        if machine == self._local_machine_id():
            return None
        if self._guest_registry is not None:
            sess = self._guest_registry.get(machine)
            if sess is None:
                return web.json_response({"ok": False, "error": "unknown machine"}, status=404)
            return await self._proxy_to_remote(sess, method, path, request, body=body)
        if self._guest_client is not None:
            return await self._proxy_via_host(method, path, request, body=body)
        return web.json_response({"ok": False, "error": "no cluster routing available"}, status=503)

    async def _dispatch_machine_stream(
        self,
        machine: str,
        path: str,
        request: web.Request,
    ) -> web.StreamResponse | None:
        """Streaming counterpart to `_dispatch_machine_request` for SSE."""
        if machine == self._local_machine_id():
            return None
        if self._guest_registry is not None:
            sess = self._guest_registry.get(machine)
            if sess is None:
                return web.json_response({"ok": False, "error": "unknown machine"}, status=404)
            return await self._proxy_stream_to_remote(sess, path, request)
        if self._guest_client is not None:
            return await self._proxy_via_host_stream(path, request)
        return web.json_response({"ok": False, "error": "no cluster routing available"}, status=503)

    async def _proxy_via_host(
        self,
        method: str,
        path: str,
        request: web.Request,
        body: dict | None = None,
    ) -> web.Response:
        """Guest-side: forward an HTTP request to the host over the existing WS."""
        try:
            result = await self._guest_client.call(
                method, path, query=dict(request.query), body=body,
            )
        except asyncio.TimeoutError:
            return web.json_response({"ok": False, "error": "host timeout"}, status=504)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"host error: {e}"}, status=502)
        return web.json_response(result.get("body") or {}, status=int(result.get("status") or 200))

    async def _proxy_via_host_stream(
        self,
        path: str,
        request: web.Request,
    ) -> web.StreamResponse:
        """Guest-side: forward an SSE GET to the host, relay frames to the browser."""
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
        await resp.write(b": connected\n\n")
        try:
            async for data in self._guest_client.call_stream(
                "GET", path, query=dict(request.query),
            ):
                await resp.write(f"data: {data}\n\n".encode("utf-8"))
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

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
            from boxagent.mcp_http import create_mcp_app

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

    async def _handle_schedule_run(self, request: web.Request) -> web.Response:
        """Handle POST /api/schedule/run — execute a schedule once."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        task_id = body.get("id")
        if not task_id:
            return web.json_response({"ok": False, "error": "missing 'id'"}, status=400)

        # Load fresh from disk
        schedules_file = self.config_dir / "schedules.yaml"
        all_tasks = load_schedules(schedules_file, node_id=self.config.node_id)
        if task_id not in all_tasks:
            return web.json_response({"ok": False, "error": f"schedule '{task_id}' not found"}, status=404)

        task = all_tasks[task_id]
        run_async = body.get("async", False)

        if run_async:
            # Fire-and-forget: schedule in background, return immediately
            import asyncio
            asyncio.ensure_future(self._schedule_run_bg(task_id, task))
            return web.json_response({"ok": True, "status": "scheduled"})

        try:
            output = await self._scheduler.execute_once(task)
            return web.json_response({"ok": True, "output": output})
        except Exception as e:
            logger.error("API schedule/run '%s' failed: %s", task_id, e)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _schedule_run_bg(self, task_id: str, task) -> None:
        """Background wrapper for async schedule execution."""
        try:
            await self._scheduler.execute_once(task)
            logger.info("Async schedule/run '%s' completed", task_id)
        except Exception as e:
            logger.error("Async schedule/run '%s' failed: %s", task_id, e)

    async def _handle_workgroup_send(self, request: web.Request) -> web.Response:
        """Handle POST /api/workgroup/send — dispatch task to a specialist (async)."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        target = body.get("target", "")
        message = body.get("message", "")
        from_bot = body.get("from", "")
        reply_chat_id = body.get("reply_chat_id", "")

        if not target:
            return web.json_response({"ok": False, "error": "missing 'target'"}, status=400)
        if not message:
            return web.json_response({"ok": False, "error": "missing 'message'"}, status=400)

        try:
            result = await self._workgroup_mgr.send_to_specialist(
                target, message, from_bot=from_bot, reply_chat_id=reply_chat_id,
            )
            return web.json_response(result)
        except Exception as e:
            logger.error("Workgroup send to '%s' failed: %s", target, e)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _handle_create_specialist(self, request: web.Request) -> web.Response:
        """Handle POST /api/workgroup/create_specialist."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        workgroup_name = body.get("workgroup", "")
        specialist_name = body.get("name", "")
        logger.info(
            "create_specialist request: workgroup=%s name=%s model=%s workspace=%s",
            workgroup_name, specialist_name, body.get("model", ""), body.get("workspace", ""),
        )
        if not workgroup_name or not specialist_name:
            return web.json_response(
                {"ok": False, "error": "missing 'workgroup' or 'name'"}, status=400,
            )

        result = await self._workgroup_mgr.create_specialist(
            workgroup_name, specialist_name,
            model=body.get("model", ""),
            workspace=body.get("workspace", ""),
        )
        status = 200 if result.get("ok") else 400
        return web.json_response(result, status=status)

    async def _handle_reset_specialist(self, request: web.Request) -> web.Response:
        """Handle POST /api/workgroup/reset_specialist."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        target = body.get("name", "")
        if not target:
            return web.json_response({"ok": False, "error": "missing 'name'"}, status=400)

        result = self._workgroup_mgr.reset_specialist(target)
        status = 200 if result.get("ok") else 400
        return web.json_response(result, status=status)

    async def _handle_list_specialists(self, request: web.Request) -> web.Response:
        """Handle GET /api/workgroup/specialists — list all specialists with details."""
        workgroup_name = request.query.get("workgroup", "")
        result = self._workgroup_mgr.list_specialists(workgroup_name)
        return web.json_response(result)

    async def _handle_specialist_status(self, request: web.Request) -> web.Response:
        """Handle GET /api/workgroup/specialist_status — get specialist status + recent chat."""
        name = request.query.get("name", "")
        if not name:
            return web.json_response({"ok": False, "error": "missing 'name'"}, status=400)
        result = self._workgroup_mgr.get_specialist_status(name)
        return web.json_response(result)

    async def _handle_delete_specialist(self, request: web.Request) -> web.Response:
        """Handle POST /api/workgroup/delete_specialist."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        target = body.get("name", "")
        if not target:
            return web.json_response({"ok": False, "error": "missing 'name'"}, status=400)

        result = await self._workgroup_mgr.delete_specialist(target)
        status = 200 if result.get("ok") else 400
        return web.json_response(result, status=status)

    async def _handle_cancel_task(self, request: web.Request) -> web.Response:
        """Handle POST /api/workgroup/cancel_task — cancel a running specialist task."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        task_id = body.get("task_id", "")
        if not task_id:
            return web.json_response({"ok": False, "error": "missing 'task_id'"}, status=400)

        result = await self._workgroup_mgr.cancel_task(task_id)
        status = 200 if result.get("ok") else 400
        return web.json_response(result, status=status)

    async def send_peer(
        self, target: str, sender: str, message: str,
    ) -> dict:
        """Cluster-aware cross-admin peer message dispatch.

        Resolves target locally first, falls back to guest RPC. Used by
        both the HTTP route /api/peer/send and the MCP send_to_peer tool.

        Returns ``{ok: bool, via: "local"|"rpc"|"none", machine?: str, error?: str}``.
        """
        if (
            self._workgroup_mgr is not None
            and target in self._workgroup_mgr.routers
        ):
            await self._dispatch_local_peer(target, sender, message)
            return {"ok": True, "via": "local"}
        if self._guest_registry is not None:
            for machine_id, bot in self._guest_registry.list_bots():
                if bot.name != target or bot.kind != "workgroup":
                    continue
                sess = self._guest_registry.get(machine_id)
                if sess is None:
                    continue
                try:
                    rpc_result = await sess.call(
                        "POST", "/api/wg/peer/recv",
                        body={"target_workgroup": target, "sender": sender, "body": message},
                    )
                except Exception as e:
                    logger.error("Peer RPC to %s failed: %s", machine_id, e)
                    return {"ok": False, "via": "rpc", "error": f"rpc failed: {e}"}
                # Don't trust GuestSession.call's transport-level success —
                # the guest-side handler may have returned 404/500 (e.g. wrong
                # port, unknown workgroup). Surface non-2xx as a real failure
                # so callers (and the admin AI) don't think the message was
                # delivered when it wasn't.
                status = int(rpc_result.get("status") or 0)
                if 200 <= status < 300:
                    return {"ok": True, "via": "rpc", "machine": machine_id}
                body = rpc_result.get("body") or {}
                err = body.get("error") if isinstance(body, dict) else None
                return {
                    "ok": False, "via": "rpc", "machine": machine_id,
                    "error": f"guest returned status={status}: {err or body}",
                }
        # Guest mode: not host, can't see registry — forward to host's
        # /api/peer/send and let host resolve. Without this, sats can only
        # peer-message workgroups they host themselves.
        if self._guest_client is not None:
            try:
                result = await self._guest_client.fetch_host_json(
                    "/api/peer/send", method="POST",
                    body={"target": target, "from": sender, "message": message},
                )
            except Exception as e:
                logger.error("Peer fwd to host failed: %s", e)
                return {"ok": False, "via": "host-fwd", "error": f"host fwd failed: {e}"}
            if result.get("ok"):
                return {"ok": True, "via": "host-fwd", "machine": result.get("machine", "")}
            return {
                "ok": False, "via": "host-fwd",
                "error": result.get("error") or "host returned not-ok",
            }
        return {
            "ok": False, "via": "none",
            "error": f"no workgroup '{target}' found locally or in cluster",
        }

    async def _handle_peer_send(self, request: web.Request) -> web.Response:
        """Handle POST /api/peer/send — thin wrapper around send_peer."""
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        target = payload.get("target", "")
        message = payload.get("message", "")
        from_bot = payload.get("from", "")
        if not target or not message or not from_bot:
            return web.json_response(
                {"ok": False, "error": "missing 'target', 'message', or 'from'"},
                status=400,
            )

        result = await self.send_peer(target, from_bot, message)
        if not result.get("ok"):
            via = result.get("via")
            status = 502 if via == "rpc" else 404
            return web.json_response(result, status=status)
        return web.json_response(result)

    async def _dispatch_local_peer(self, target: str, sender: str, body: str) -> None:
        """Inject a peer message into the local workgroup admin's router.

        Wraps `body` in the workgroup peer envelope (admin always sees the
        same shape regardless of transport).

        Routed to the same chat_id heartbeat dispatches into
        (``heartbeat:<target>``) so the message lands in the admin's main
        session — not a separate ``peer:<sender>`` chat that would spawn a
        fresh, context-less session each time.
        """
        admin_router = self._workgroup_mgr.routers[target]
        envelope = (
            f"[Peer message from {sender}]\n"
            f"{body}\n\n"
            f"---\n"
            f'Reply with: send_to_peer("{sender}", "your reply")'
        )
        from boxagent.channels.base import IncomingMessage
        msg = IncomingMessage(
            channel="internal",
            chat_id=self._get_or_create_main_chat_id(target),
            user_id=sender,
            text=envelope,
            trusted=True,
        )
        await admin_router.handle_message(msg)

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

    async def _handle_wg_peer_recv(self, request: web.Request) -> web.Response:
        """Handle POST /api/wg/peer/recv — receive a peer message from another node.

        Body: {target_workgroup, sender, body} where body is RAW (no envelope).
        Caller (host's _handle_peer_send) routes here via cluster RPC; guest_client
        forwards it over WS to this local gateway.
        """
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        target = payload.get("target_workgroup", "")
        sender = payload.get("sender", "")
        body = payload.get("body", "")
        if not target or not sender:
            return web.json_response(
                {"ok": False, "error": "missing 'target_workgroup' or 'sender'"},
                status=400,
            )
        if (
            self._workgroup_mgr is None
            or target not in self._workgroup_mgr.routers
        ):
            return web.json_response(
                {"ok": False, "error": f"workgroup '{target}' not on this node"},
                status=404,
            )
        await self._dispatch_local_peer(target, sender, body)
        return web.json_response({"ok": True})

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
        index = _Path(__file__).parent / "web" / "static" / "index.html"
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
        if self._guest_registry is not None:
            for mid, b in self._guest_registry.list_bots():
                bots.append({
                    "name": b.name,
                    "display_name": (b.display_name or b.name) + f"  @{mid}",
                    "backend": b.backend,
                    "model": b.model,
                    "kind": b.kind,
                    "machine": mid,
                })
        elif self._guest_client is not None:
            for m in self._guest_client.remote_machines:
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

    async def _handle_guest_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Permanent route — delegates to the GuestRegistry only when this
        node is the active host; otherwise returns 503 so the dialing peer
        falls back / reconnects elsewhere."""
        registry = self._guest_registry
        if registry is None:
            return web.json_response(
                {"ok": False, "error": "not host"}, status=503,
            )
        return await registry.handle_ws(request)

    async def _handle_web_machines(self, request: web.Request) -> web.Response:
        """Return all known machines (self + connected/disconnected guests)
        so the UI can render a grouped sidebar with online/offline status."""
        if not self._web_authorized(request):
            return self._web_unauthorized()
        if self._guest_registry is not None:
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
        if self._guest_client is not None:
            for m in self._guest_client.remote_machines:
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
        if self._guest_registry is not None:
            sats: dict[str, object] = {}
            for machine_id, sess in list(self._guest_registry.sessions.items()):
                try:
                    result = await sess.call("GET", "/api/version", timeout=5.0)
                    sats[machine_id] = result.get("body") or {"error": "no body"}
                except Exception as e:
                    sats[machine_id] = {"error": str(e)}
            return web.json_response({"ok": True, "self": local, "sats": sats})
        # Guest mode: ask host via tunnel HTTP, merge.
        if self._guest_client is not None:
            try:
                host_result = await self._guest_client.fetch_host_json("/api/version", {"cluster": "1"})
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
        if self._guest_registry is None:
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
        for machine_id, sess in list(self._guest_registry.sessions.items()):
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

    def _local_machine_id(self) -> str:
        return self.config.machine_id or self.config.node_id or "local"

    def _local_role(self) -> str:
        """Current cluster role of this node — driven by ClusterRoleManager."""
        rm = self._role_manager
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

    async def _proxy_to_remote(
        self,
        sess,
        method: str,
        path: str,
        request: web.Request,
        body: dict | None = None,
    ) -> web.Response:
        """Forward an HTTP request to a guest over WS RPC and return its response."""
        try:
            result = await sess.call(
                method, path,
                query=dict(request.query),
                body=body,
            )
        except asyncio.TimeoutError:
            return web.json_response({"ok": False, "error": "remote timeout"}, status=504)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"remote error: {e}"}, status=502)
        return web.json_response(result.get("body") or {}, status=int(result.get("status") or 200))

    async def _proxy_stream_to_remote(
        self,
        sess,
        path: str,
        request: web.Request,
    ) -> web.StreamResponse:
        """Forward an SSE GET to a guest, relay frames to the browser."""
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
        await resp.write(b": connected\n\n")
        try:
            async for data in sess.call_stream("GET", path, query=dict(request.query)):
                await resp.write(f"data: {data}\n\n".encode("utf-8"))
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

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

    async def stop(self) -> None:
        logger.info("Gateway shutting down...")

        # Release listening ports first so a restarting process can re-bind immediately,
        # regardless of how long the rest of the shutdown takes.
        await self._stop_web_http()
        await self._stop_http()
        await self._stop_mcp_http()

        # Stop role manager — it tears down whichever of guest_client /
        # cluster_tunnel / guest_registry it currently owns.
        if self._role_manager is not None:
            try:
                await self._role_manager.stop()
            except Exception as e:
                logger.error("Error stopping role manager: %s", e)
            self._role_manager = None

        # Stop guest WS client (if still running — role manager normally owns this)
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
