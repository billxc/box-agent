"""Gateway — orchestrates all components."""

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from boxagent.agent.claude_process import ClaudeProcess
from boxagent.channels.base import IncomingMessage
from boxagent.channels.telegram import TelegramChannel
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


def _supports_persistent_session(ai_backend: str) -> bool:
    """Whether a backend can resume a saved session after restart."""
    return ai_backend in ("claude-cli", "codex-cli", "codex-acp")


def _create_backend(bot_cfg: BotConfig, session_id: str | None) -> object:
    """Instantiate the AI backend based on config."""
    if bot_cfg.ai_backend == "codex-acp":
        from boxagent.agent.acp_process import ACPProcess

        return ACPProcess(
            workspace=bot_cfg.workspace,
            session_id=session_id,
            model=bot_cfg.model,
            agent=bot_cfg.agent,
            bot_token=bot_cfg.telegram_token,
            bot_name=bot_cfg.name,
        )
    if bot_cfg.ai_backend == "codex-cli":
        from boxagent.agent.codex_process import CodexProcess

        return CodexProcess(
            workspace=bot_cfg.workspace,
            session_id=session_id,
            model=bot_cfg.model,
            agent=bot_cfg.agent,
            bot_token=bot_cfg.telegram_token,
            bot_name=bot_cfg.name,
            yolo=bot_cfg.yolo,
        )
    return ClaudeProcess(
        workspace=bot_cfg.workspace,
        session_id=session_id,
        model=bot_cfg.model,
        agent=bot_cfg.agent,
        bot_token=bot_cfg.telegram_token,
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

        # Expose paths for MCP server subprocesses (schedule/session tools)
        os.environ.setdefault("BOXAGENT_CONFIG_DIR", str(self.config_dir))
        os.environ.setdefault("BOXAGENT_LOCAL_DIR", str(self.local_dir))
        if self.config.node_id:
            os.environ.setdefault("BOXAGENT_NODE_ID", self.config.node_id)

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

        # Telegram: send immediately (user ID = chat ID for private chats)
        tg_chat_id = str(bot_cfg.telegram_allowed_users[0]) if bot_cfg.telegram_token and bot_cfg.telegram_allowed_users else ""
        if tg_chat_id and name in self._channels:
            try:
                await self._channels[name].send_text(tg_chat_id, notify_text)
            except Exception as e:
                logger.warning("Failed to send Telegram startup notification for '%s': %s", name, e)

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
    def _sock_path(self) -> Path:
        return self.local_dir / "api.sock"

    @property
    def _api_port_file(self) -> Path:
        return self.local_dir / "api-port.txt"

    def _clear_http_artifacts(self) -> None:
        """Remove runtime HTTP endpoint artifacts left by a previous run."""
        if self._sock_path.exists():
            self._sock_path.unlink()
        if self._api_port_file.exists():
            self._api_port_file.unlink()

    async def _start_http(self) -> None:
        """Start the internal HTTP API server (Unix socket + optional TCP)."""
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
        runner = web.AppRunner(app)
        await runner.setup()
        self._http_runner = runner

        self.local_dir.mkdir(parents=True, exist_ok=True)
        self._clear_http_artifacts()

        # Listen on Unix socket (Linux/macOS) or fallback to TCP (Windows)
        if sys.platform != "win32":
            sock_path = self._sock_path
            unix_site = web.UnixSite(runner, str(sock_path))
            await unix_site.start()
            # Record inode so _stop_http only deletes OUR socket
            try:
                self._sock_inode = sock_path.stat().st_ino
            except OSError:
                self._sock_inode = 0
            logger.info("HTTP API listening on unix:%s", sock_path)
        else:
            # Windows: no Unix sockets. If api_port is unset, ask the OS for a free port.
            port = self.config.api_port or 0
            tcp_site = web.TCPSite(runner, "127.0.0.1", port)
            await tcp_site.start()
            sockets = getattr(getattr(tcp_site, "_server", None), "sockets", None) or []
            actual_port = sockets[0].getsockname()[1] if sockets else port
            self._api_port_file.write_text(f"{actual_port}\n", encoding="utf-8")
            logger.info("HTTP API listening on 127.0.0.1:%d (Windows fallback)", actual_port)
            logger.info("HTTP API port file written to %s", self._api_port_file)

        # Optionally also listen on TCP (additional port, Linux/macOS)
        if self.config.api_port and sys.platform != "win32":
            tcp_site = web.TCPSite(runner, "127.0.0.1", self.config.api_port)
            await tcp_site.start()
            logger.info("HTTP API also listening on 127.0.0.1:%d", self.config.api_port)

    async def _stop_http(self) -> None:
        """Stop the HTTP API server."""
        if self._http_runner:
            await self._http_runner.cleanup()
            self._http_runner = None
        # Only delete socket if it's the one WE created (inode match).
        # Prevents a shutting-down instance from deleting a new instance's socket.
        sock = self._sock_path
        if sock.exists():
            try:
                current_inode = sock.stat().st_ino
            except OSError:
                current_inode = 0
            if current_inode == getattr(self, "_sock_inode", 0) and current_inode != 0:
                sock.unlink(missing_ok=True)
                logger.info("Removed own socket: %s", sock)
            else:
                logger.info("Socket %s belongs to another instance (inode %d != %d), not removing",
                            sock, current_inode, getattr(self, "_sock_inode", 0))
        if self._api_port_file.exists():
            self._api_port_file.unlink(missing_ok=True)

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

    async def stop(self) -> None:
        logger.info("Gateway shutting down...")

        # Stop HTTP API
        await self._stop_http()

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
