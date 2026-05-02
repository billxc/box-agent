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
from boxagent.cluster import ClusterTunnel, SatelliteClient, SatelliteRegistry
from boxagent.config import AppConfig, BotConfig, WorkgroupConfig, node_matches
from boxagent.paths import default_config_dir, default_local_dir, default_workspace_dir
from boxagent.router import Router
from boxagent.sessions import SessionPool
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
    if chat_id.startswith("heartbeat:"):
        return "heartbeat"
    if chat_id.startswith("claude-"):
        return "claude"
    if chat_id.startswith("web-"):
        return "web"
    if chat_id.lstrip("-").isdigit():
        # Telegram user ids are typically <= 12 digits; Discord snowflakes are 17-19.
        return "discord" if len(chat_id.lstrip("-")) >= 17 else "telegram"
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
    # Shared Discord channels keyed by identity (bot_id or token).
    _discord_channels: dict[str, object] = field(
        default_factory=dict, repr=False
    )
    # Maps bot_name → Discord identity key for shared channel lookup.
    _bot_discord_key: dict[str, str] = field(
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
    _watchdogs: dict[str, Watchdog] = field(default_factory=dict, repr=False)
    _watchdog_tasks: list[asyncio.Task] = field(
        default_factory=list, repr=False
    )
    _scheduler: Scheduler | None = field(default=None, repr=False)
    _scheduler_task: asyncio.Task | None = field(default=None, repr=False)
    _http_runner: web.AppRunner | None = field(default=None, repr=False)
    _sat_registry: SatelliteRegistry | None = field(default=None, repr=False)
    _sat_client: SatelliteClient | None = field(default=None, repr=False)
    _cluster_tunnel: ClusterTunnel | None = field(default=None, repr=False)
    _start_time: float = 0.0
    _workgroup_mgr: WorkgroupManager | None = field(default=None, repr=False)

    def _get_bot_discord_channel(self, bot_name: str) -> object | None:
        """Return the shared Discord channel for a bot, or None."""
        key = self._bot_discord_key.get(bot_name)
        if key is None:
            return None
        return self._discord_channels.get(key)

    async def start(self) -> None:
        self._start_time = time.time()
        self._storage = Storage(local_dir=self.local_dir)
        logger.info("Gateway starting (node=%s)", self.config.node_id or "(any)")

        # Start Web UI first so the page is reachable while the rest boots.
        await self._start_web_http()

        # Phase 1: Create shared Discord channel instances (one per unique bot identity)
        self._create_shared_discord_channels()

        # Phase 2: Start each bot (registers routes on shared Discord channels)
        for name, bot_cfg in self.config.bots.items():
            if not node_matches(bot_cfg.enabled_on_nodes, self.config.node_id):
                logger.info("Bot '%s' skipped (enabled_on_nodes=%s, current=%s)", name, bot_cfg.enabled_on_nodes, self.config.node_id)
                continue
            await self._start_bot(name, bot_cfg)

        # Phase 3: Start all shared Discord channels (one start() per unique client)
        for dc_key, dc_ch in self._discord_channels.items():
            await dc_ch.start()
            logger.info("Shared Discord channel '%s' started", dc_key)

        # Phase 4: Start workgroups (after Discord channels are started)
        if self.config.workgroups:
            self._workgroup_mgr = WorkgroupManager(
                config=self.config.workgroups,
                config_dir=str(self.config_dir),
                node_id=self.config.node_id,
                local_dir=self._storage.local_dir if self._storage else None,
                start_time=self._start_time,
                storage=self._storage,
                discord_channels=self._discord_channels,
                web_channels=self._web_channels,
                _create_backend=_create_backend,
                _ensure_git_repo=_ensure_git_repo,
                _sync_skills=sync_skills,
            )
            for wg_name, wg_cfg in self.config.workgroups.items():
                if not node_matches(wg_cfg.enabled_on_nodes, self.config.node_id):
                    logger.info("Workgroup '%s' skipped (enabled_on_nodes=%s, current=%s)", wg_name, wg_cfg.enabled_on_nodes, self.config.node_id)
                    continue
                await self._workgroup_mgr.start_workgroup(wg_name, wg_cfg)

        # Start scheduler
        self._start_scheduler()

        # Start HTTP API
        await self._start_http()

        # If configured as a cluster host, manage our own devtunnel for /api/sat/ws
        if self.config.satellite_token and self.config.cluster_tunnel:
            self._cluster_tunnel = ClusterTunnel(
                name=self.config.cluster_tunnel,
                port=self.config.web_port or 9292,
            )
            try:
                url = await self._cluster_tunnel.start()
                logger.info("Cluster: host devtunnel ready → %s", url)
            except Exception as e:
                logger.error("Cluster: failed to start host devtunnel: %s", e)
                self._cluster_tunnel = None

        # If configured as a satellite, dial the host (URL resolved via tunnel_name)
        if (not self.config.satellite_token) and self.config.cluster_tunnel and self.config.host_token:
            mid = self.config.machine_id or self.config.node_id or "satellite"
            self._sat_client = SatelliteClient(
                host_url="",  # resolved by client via tunnel_name
                host_token=self.config.host_token,
                machine_id=mid,
                local_web_port=self.config.web_port or 9292,
                local_web_token=self.config.web_token,
                tunnel_name=self.config.cluster_tunnel,
                bot_provider=self._local_bot_descriptors,
            )
            self._sat_client.start()
            logger.info(
                "Cluster: satellite mode — will resolve host via tunnel '%s' (machine_id=%s)",
                self.config.cluster_tunnel, mid,
            )

        logger.info(
            "Gateway ready: %d bot(s) active", len(self.config.bots)
        )

    def _create_shared_discord_channels(self) -> None:
        """Pre-create one DiscordChannel per unique Discord bot identity."""
        from boxagent.channels.discord import DiscordChannel

        for name, bot_cfg in self.config.bots.items():
            if not node_matches(bot_cfg.enabled_on_nodes, self.config.node_id):
                continue
            if not bot_cfg.discord_token:
                continue
            key = bot_cfg.discord_bot_id or bot_cfg.discord_token
            if key not in self._discord_channels:
                self._discord_channels[key] = DiscordChannel(
                    token=bot_cfg.discord_token,
                    tool_calls_display=bot_cfg.display_tool_calls,
                )
            self._bot_discord_key[name] = key

        # Ensure workgroup Discord identities are also created
        for wg_name, wg_cfg in self.config.workgroups.items():
            if not node_matches(wg_cfg.enabled_on_nodes, self.config.node_id):
                continue
            key = wg_cfg.discord_bot_id
            if key and key not in self._discord_channels:
                self._discord_channels[key] = DiscordChannel(
                    token=wg_cfg.discord_token,
                )

    async def _start_bot(self, name: str, bot_cfg: BotConfig) -> None:
        session_id = None
        if _supports_persistent_session(bot_cfg.ai_backend):
            saved = self._storage.load_session(name)
            if isinstance(saved, dict):
                session_id = saved.get("session_id")
            elif isinstance(saved, str):
                session_id = saved

        cli = _create_backend(bot_cfg, session_id)
        cli.start()
        self._cli_processes[name] = cli

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

        # --- Discord channel (shared instance) ---
        dc_channel = self._get_bot_discord_channel(name)
        if dc_channel is not None:
            if primary_channel is None:
                primary_channel = dc_channel

        router = Router(
            cli_process=cli,
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
            has_peer_channel=bool(bot_cfg.discord_peer_channel),
            telegram_token=bot_cfg.telegram_token,
        )

        # Wire Telegram channel to router
        if name in self._channels:
            router._channels["telegram"] = self._channels[name]
            self._channels[name].on_message = router.handle_message
            await self._channels[name].start()

        # Register route on shared Discord channel (start() happens later in Gateway.start)
        if dc_channel is not None:
            from boxagent.channels.discord import DM_CATEGORY

            categories: list = list(bot_cfg.discord_allowed_categories)
            if bot_cfg.discord_dm:
                categories.append(DM_CATEGORY)
            if categories:
                dc_channel.register_route(router.handle_message, categories)
            router._channels["discord"] = dc_channel

        # Register peer channel route (separate from category routing)
        if bot_cfg.discord_peer_channel and dc_channel is not None:
            peer_ch_id = bot_cfg.discord_peer_channel
            comm_ch_id = str(bot_cfg.discord_comm_channel)
            _bot_name = name

            async def _peer_handler(msg, _name=_bot_name, _comm=comm_ch_id, _router=router):
                target, sender, body = _parse_peer_message(msg.text)
                if target != _name:
                    return
                # Rewrite chat_id to comm_channel so the response streams there
                msg = IncomingMessage(
                    channel=msg.channel,
                    chat_id=_comm,
                    user_id=msg.user_id,
                    text=(
                        f"[Peer message from {sender}]\n"
                        f"{body}\n\n"
                        f"---\n"
                        f'Reply with: send_to_peer("{sender}", "your reply")'
                    ),
                    attachments=msg.attachments,
                    trusted=True,
                    channel_info=msg.channel_info,
                )
                await _router.handle_message(msg)

            dc_channel.register_channel_route(_peer_handler, peer_ch_id)

        # --- Web channel (optional) ---
        if bot_cfg.web_enabled:
            web_ch = WebChannel(bot_name=name)
            web_ch.on_message = router.handle_message
            self._web_channels[name] = web_ch
            router._channels["web"] = web_ch
            logger.info("Bot '%s' web channel enabled", name)

        self._routers[name] = router

        # Notify user that bot is online
        import datetime
        skill_count = len(linked)
        channels_active = []
        if bot_cfg.telegram_token:
            channels_active.append("telegram")
        if dc_channel is not None:
            channels_active.append("discord")
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
            async def _send_tg_notify(ch=self._channels[name], cid=tg_chat_id, text=notify_text, bot_name=name):
                try:
                    await ch.send_text(cid, text)
                except Exception as e:
                    logger.warning("Failed to send Telegram startup notification for '%s': %s", bot_name, e)
            asyncio.create_task(_send_tg_notify())

        # Discord: send DM after bot is ready (on_ready fires async)
        dc_user_id = str(bot_cfg.discord_allowed_users[0]) if bot_cfg.discord_token and bot_cfg.discord_allowed_users else ""
        if dc_user_id and dc_channel is not None:
            async def _send_discord_notify(ch=dc_channel, uid=dc_user_id, text=notify_text, bot_name=name):
                # Wait for Discord client to be created (Phase 3) and connected
                for _ in range(60):
                    if ch._client is not None:
                        break
                    await asyncio.sleep(0.5)
                else:
                    logger.warning("Discord client never initialized for '%s', skipping startup notification", bot_name)
                    return
                await ch._client.wait_until_ready()
                try:
                    await ch.send_dm(uid, text)
                except Exception as e:
                    logger.warning("Failed to send Discord startup notification for '%s': %s", bot_name, e)

            asyncio.create_task(_send_discord_notify())

        async def restart_bot(n=name, bc=bot_cfg):
            await self._restart_bot(n, bc)

        # Watchdog chat_id for error notifications
        wd_chat_id = tg_chat_id or dc_user_id

        wd = Watchdog(
            cli_process=cli,
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

    def _start_scheduler(self) -> None:
        """Create and start the Scheduler after all active bots are online."""
        schedules_file = self.config_dir / "schedules.yaml"
        bot_refs: dict[str, BotRef] = {}
        for name in self._routers:
            bot_cfg = self.config.bots[name]
            chat_id = str(bot_cfg.allowed_users[0]) if bot_cfg.allowed_users else ""
            primary_channel = self._channels.get(name) or self._get_bot_discord_channel(name)
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
        app.router.add_post("/api/workgroup/update_topic", self._handle_update_topic)
        app.router.add_post("/api/workgroup/cancel_task", self._handle_cancel_task)
        app.router.add_post("/api/peer/send", self._handle_peer_send)

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

        wapp = web.Application()
        wapp.router.add_get("/", self._handle_web_index)
        wapp.router.add_get("/api/bots", self._handle_web_bots)
        wapp.router.add_get("/api/machines", self._handle_web_machines)
        wapp.router.add_get("/api/sessions", self._handle_web_sessions)
        wapp.router.add_get("/api/history", self._handle_web_history)
        wapp.router.add_post("/api/send", self._handle_web_send)
        wapp.router.add_get("/api/stream", self._handle_web_stream)
        wapp.router.add_get("/api/claude/projects", self._handle_claude_projects)
        wapp.router.add_get("/api/claude/sessions", self._handle_claude_sessions)
        wapp.router.add_get("/api/claude/transcript", self._handle_claude_transcript)
        wapp.router.add_post("/api/claude/resume", self._handle_claude_resume)
        # Hub-and-spoke: WS endpoint for satellite nodes
        if self.config.satellite_token:
            self._sat_registry = SatelliteRegistry(expected_token=self.config.satellite_token)
            wapp.router.add_get("/api/sat/ws", self._sat_registry.handle_ws)
            logger.info("Cluster: host mode enabled (accepting satellites at /api/sat/ws)")
        web_static = _Path(__file__).parent / "web" / "static"
        if web_static.is_dir():
            wapp.router.add_static("/", path=str(web_static), show_index=False)

        runner = web.AppRunner(wapp)
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
            wg = self.config.workgroups.get(name)
            if cfg is not None:
                out.append({
                    "name": name,
                    "display_name": cfg.display_name or name,
                    "backend": cfg.ai_backend,
                    "model": cfg.model,
                    "kind": "bot",
                })
            elif wg is not None:
                out.append({
                    "name": name,
                    "display_name": wg.display_name or name,
                    "backend": wg.ai_backend,
                    "model": wg.model,
                    "kind": "workgroup",
                })
        return out

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

        wg_name = body.get("workgroup", "")
        sp_name = body.get("name", "")
        logger.info(
            "create_specialist request: wg=%s name=%s model=%s workspace=%s",
            wg_name, sp_name, body.get("model", ""), body.get("workspace", ""),
        )
        if not wg_name or not sp_name:
            return web.json_response(
                {"ok": False, "error": "missing 'workgroup' or 'name'"}, status=400,
            )

        result = await self._workgroup_mgr.create_specialist(
            wg_name, sp_name,
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
        wg_name = request.query.get("workgroup", "")
        result = self._workgroup_mgr.list_specialists(wg_name)
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

    async def _handle_update_topic(self, request: web.Request) -> web.Response:
        """Handle POST /api/workgroup/update_topic — update a Discord channel's topic."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        channel_id = body.get("channel_id", "")
        topic = body.get("topic", "")
        if not channel_id:
            return web.json_response({"ok": False, "error": "missing 'channel_id'"}, status=400)

        # Find the Discord channel object from any workgroup
        dc_channel = None
        if self._workgroup_mgr:
            for dc in self._workgroup_mgr.discord_channels.values():
                dc_channel = dc
                break
        if dc_channel is None:
            return web.json_response({"ok": False, "error": "no Discord channel available"}, status=400)

        try:
            await dc_channel.update_channel_topic(int(channel_id), topic)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

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

    async def _handle_peer_send(self, request: web.Request) -> web.Response:
        """Handle POST /api/peer/send — send a message to the peer channel."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        target = body.get("target", "")
        message = body.get("message", "")
        from_bot = body.get("from", "")
        if not target or not message or not from_bot:
            return web.json_response(
                {"ok": False, "error": "missing 'target', 'message', or 'from'"},
                status=400,
            )

        # Find sender's Discord channel and peer channel ID
        # Check regular bots first, then workgroups
        peer_channel_id = 0
        dc_key = None
        bot_cfg = self.config.bots.get(from_bot)
        if bot_cfg and bot_cfg.discord_peer_channel:
            peer_channel_id = bot_cfg.discord_peer_channel
            dc_key = self._bot_discord_key.get(from_bot)
        else:
            wg_cfg = self.config.workgroups.get(from_bot)
            if wg_cfg and wg_cfg.discord_peer_channel:
                peer_channel_id = wg_cfg.discord_peer_channel
                dc_key = wg_cfg.discord_bot_id

        if not peer_channel_id:
            return web.json_response(
                {"ok": False, "error": f"bot '{from_bot}' has no peer channel configured"},
                status=400,
            )

        dc_channel = self._discord_channels.get(dc_key) if dc_key else None
        if dc_channel is None:
            return web.json_response(
                {"ok": False, "error": f"no Discord channel for bot '{from_bot}'"},
                status=400,
            )

        peer_chat_id = str(peer_channel_id)
        formatted = f"[To: {target}] [From: {from_bot}]\n{message}"
        try:
            await dc_channel.send_text(peer_chat_id, formatted)
        except Exception as e:
            logger.error("Failed to send peer message: %s", e)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

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
            wg = self.config.workgroups.get(name)
            if cfg is not None:
                bots.append({
                    "name": name,
                    "display_name": cfg.display_name or name,
                    "backend": cfg.ai_backend,
                    "model": cfg.model,
                    "kind": "bot",
                    "machine": local_mid,
                })
            elif wg is not None:
                bots.append({
                    "name": name,
                    "display_name": (wg.display_name or name) + "  (workgroup)",
                    "backend": wg.ai_backend,
                    "model": wg.model,
                    "kind": "workgroup",
                    "machine": local_mid,
                })
        # Federate: include bots from connected satellites
        if self._sat_registry is not None:
            for mid, b in self._sat_registry.list_bots():
                bots.append({
                    "name": b.name,
                    "display_name": (b.display_name or b.name) + f"  @{mid}",
                    "backend": b.backend,
                    "model": b.model,
                    "kind": b.kind,
                    "machine": mid,
                })
        return web.json_response({"bots": bots})

    async def _handle_web_machines(self, request: web.Request) -> web.Response:
        """Return all known machines (self + connected/disconnected satellites)
        so the UI can render a grouped sidebar with online/offline status."""
        if not self._web_authorized(request):
            return self._web_unauthorized()
        local_mid = self._local_machine_id()
        local_role = "host" if self.config.satellite_token else (
            "satellite" if self.config.host_token else "single"
        )
        machines: list[dict] = [{
            "machine_id": local_mid,
            "online": True,
            "role": local_role,
            "self": True,
            "bots": self._local_bot_descriptors(),
            "last_seen": time.time(),
        }]
        if self._sat_registry is not None:
            for m in self._sat_registry.list_machines():
                m["role"] = "satellite"
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
        # Remote? proxy to the satellite that owns this bot.
        if machine != self._local_machine_id():
            sess = self._remote_session_for(machine, bot)
            if sess is None:
                return web.json_response({"ok": False, "error": "unknown machine/bot"}, status=404)
            return await self._proxy_to_remote(sess, "GET", "/api/sessions", request)
        if bot not in self._web_channels:
            return web.json_response({"ok": False, "error": "bot not web-enabled"}, status=404)
        if not self._storage:
            return web.json_response({"ok": True, "sessions": []})

        sessions = self._storage.list_chat_sessions(bot)

        # Workgroup heartbeat: admin uses admin_discord_channel as the chat_id
        # for periodic ticks, so flag that one specifically.
        wg = self.config.workgroups.get(bot)
        heartbeat_chat_id = ""
        if wg is not None:
            if wg.heartbeat_interval_seconds and wg.admin_discord_channel:
                heartbeat_chat_id = str(wg.admin_discord_channel)
            elif wg.heartbeat_interval_seconds:
                heartbeat_chat_id = f"heartbeat:{bot}"

        for s in sessions:
            sid = s.get("session_id") or ""
            s["platform"] = _infer_platform(s["chat_id"])
            if heartbeat_chat_id and s["chat_id"] == heartbeat_chat_id:
                s["platform"] = "heartbeat"
            s["preview"] = ""
            s["last_ts"] = 0
            if not sid:
                continue
            tpath = self._storage.local_dir / "transcripts" / f"{sid}.jsonl"
            if not tpath.is_file():
                continue
            try:
                last_user = ""
                last_assist = ""
                last_ts = 0.0
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
                    elif ev == "assistant":
                        last_assist = txt
                preview = (last_assist or last_user or "").strip().replace("\n", " ")
                s["preview"] = preview[:90] + ("..." if len(preview) > 90 else "")
                s["last_ts"] = last_ts
            except Exception as e:
                logger.debug("session preview read failed for %s: %s", sid, e)

        sessions.sort(key=lambda x: x.get("last_ts") or 0, reverse=True)
        return web.json_response({"ok": True, "sessions": sessions})

    def _local_machine_id(self) -> str:
        return self.config.machine_id or self.config.node_id or "local"

    def _remote_session_for(self, machine_id: str, bot: str):
        """Return SatelliteSession owning `bot` on `machine_id`, or None."""
        if self._sat_registry is None:
            return None
        if self._sat_registry.get_bot(machine_id, bot) is None:
            return None
        return self._sat_registry.get(machine_id)

    async def _proxy_to_remote(
        self,
        sess,
        method: str,
        path: str,
        request: web.Request,
        body: dict | None = None,
    ) -> web.Response:
        """Forward an HTTP request to a satellite over WS RPC and return its response."""
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
        """Forward an SSE GET to a satellite, relay frames to the browser."""
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
            sess = self._remote_session_for(machine, bot)
            if sess is None:
                return web.json_response({"ok": False, "error": "unknown machine/bot"}, status=404)
            return await self._proxy_to_remote(sess, "GET", "/api/history", request)
        if bot not in self._web_channels:
            return web.json_response({"ok": False, "error": "bot not web-enabled"}, status=404)

        history: list[dict] = []
        if self._storage:
            saved = self._storage.load_session(bot, chat_id)
            session_id = ""
            prev_chain: list[str] = []
            if isinstance(saved, dict):
                session_id = saved.get("session_id", "")
                raw_prev = saved.get("previous_session_ids") or []
                if isinstance(raw_prev, list):
                    prev_chain = [str(s) for s in raw_prev if isinstance(s, str) and s]
            elif isinstance(saved, str):
                session_id = saved
            sids = ([session_id] if session_id else []) + prev_chain

            # Resumed Claude native session — walk chain across ~/.claude/projects/.../<sid>.jsonl
            if chat_id.startswith("claude-") and sids:
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
                return web.json_response({"ok": True, "history": history})

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
        return web.json_response({"ok": True, "history": history})

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
            sess = self._remote_session_for(machine, bot)
            if sess is None:
                return web.json_response({"ok": False, "error": "unknown machine/bot"}, status=404)
            return await self._proxy_to_remote(sess, "POST", "/api/send", request, body=body)
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
            sess = self._remote_session_for(machine, bot)
            if sess is None:
                return web.json_response({"ok": False, "error": "unknown machine/bot"}, status=404)
            return await self._proxy_stream_to_remote(sess, "/api/stream", request)
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
            if self._sat_registry is None or self._sat_registry.get(machine) is None:
                return web.json_response({"ok": False, "error": "unknown machine"}, status=404)
            return await self._proxy_to_remote(self._sat_registry.get(machine), "GET", "/api/claude/projects", request)
        from boxagent.sessions import claude_native
        return web.json_response({"ok": True, "projects": claude_native.list_projects()})

    async def _handle_claude_sessions(self, request: web.Request) -> web.Response:
        if not self._web_authorized(request):
            return self._web_unauthorized()
        encoded = request.query.get("project", "")
        machine = request.query.get("machine", "")
        if not encoded or not machine:
            return web.json_response({"ok": False, "error": "missing project/machine"}, status=400)
        if machine != self._local_machine_id():
            if self._sat_registry is None or self._sat_registry.get(machine) is None:
                return web.json_response({"ok": False, "error": "unknown machine"}, status=404)
            return await self._proxy_to_remote(self._sat_registry.get(machine), "GET", "/api/claude/sessions", request)
        from boxagent.sessions import claude_native
        return web.json_response({
            "ok": True,
            "sessions": claude_native.list_sessions(encoded),
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
            if self._sat_registry is None or self._sat_registry.get(machine) is None:
                return web.json_response({"ok": False, "error": "unknown machine"}, status=404)
            return await self._proxy_to_remote(self._sat_registry.get(machine), "GET", "/api/claude/transcript", request)
        from boxagent.sessions import claude_native
        return web.json_response({
            "ok": True,
            "messages": claude_native.read_messages(encoded, sid),
        })

    async def _handle_claude_resume(self, request: web.Request) -> web.Response:
        """Persist the chosen Claude session_id under a synthetic chat_id so the
        next message in that chat_id resumes that Claude session via ``--resume``.
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
        if not bot or not sid or not machine:
            return web.json_response({"ok": False, "error": "missing bot/session_id/machine"}, status=400)
        if machine != self._local_machine_id():
            sess = self._remote_session_for(machine, bot)
            if sess is None:
                return web.json_response({"ok": False, "error": "unknown machine/bot"}, status=404)
            return await self._proxy_to_remote(sess, "POST", "/api/claude/resume", request, body=body)
        if bot not in self._web_channels:
            return web.json_response({"ok": False, "error": "bot not web-enabled"}, status=404)

        chat_id = f"claude-{sid}"
        if self._storage:
            cfg = self.config.bots.get(bot)
            wg = self.config.workgroups.get(bot)
            model = (cfg.model if cfg else None) or (wg.model if wg else "")
            backend = (cfg.ai_backend if cfg else None) or (wg.ai_backend if wg else "claude-cli")
            from boxagent.sessions import claude_native
            workspace = (claude_native.project_cwd(encoded) if encoded else "") or (
                cfg.workspace if cfg else (wg.admin_workspace if wg else "")
            )
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
            if pool is not None and workspace:
                pool.set_workspace(chat_id, workspace)
                pool.set_session_id(chat_id, sid)
        return web.json_response({
            "ok": True,
            "chat_id": chat_id,
            "session_id": sid,
            "project": encoded,
            "workspace": workspace if self._storage else "",
        })

    async def stop(self) -> None:
        logger.info("Gateway shutting down...")

        # Release listening ports first so a restarting process can re-bind immediately,
        # regardless of how long the rest of the shutdown takes.
        await self._stop_web_http()
        await self._stop_http()
        await self._stop_mcp_http()

        # Stop satellite WS client (if running)
        if self._sat_client is not None:
            try:
                await self._sat_client.stop()
            except Exception as e:
                logger.error("Error stopping sat client: %s", e)
            self._sat_client = None

        # Stop cluster devtunnel (host only)
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

        for name, ch in self._discord_channels.items():
            try:
                await ch.stop()
            except Exception as e:
                logger.error("Error stopping discord channel %s: %s", name, e)

        for name, ch in self._web_channels.items():
            try:
                await ch.stop()
            except Exception as e:
                logger.error("Error stopping web channel %s: %s", name, e)

        for name, cli in self._cli_processes.items():
            try:
                # Save session before stopping
                if self._storage and cli.session_id:
                    self._storage.save_session(name, cli.session_id)
                await cli.stop()
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
