"""Gateway — orchestrates all components."""

import asyncio
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from boxagent.agent.claude_process import ClaudeProcess
from boxagent.channels.telegram import TelegramChannel
from boxagent.config import AppConfig, BotConfig, node_matches
from boxagent.paths import default_config_dir, default_local_dir, default_workspace_dir
from boxagent.router import Router
from boxagent.scheduler import BotRef, Scheduler, load_schedules
from boxagent.storage import Storage
from boxagent.watchdog import Watchdog

from aiohttp import web

logger = logging.getLogger(__name__)


def _supports_persistent_session(ai_backend: str) -> bool:
    """Whether a backend can resume a saved session after restart."""
    return ai_backend in ("claude-cli", "codex-cli", "codex-acp")


def _create_backend(bot_cfg: BotConfig, session_id: str | None, copilot_api_port: int = 0) -> object:
    """Instantiate the AI backend based on config."""
    if bot_cfg.ai_backend == "codex-acp":
        from boxagent.agent.acp_process import ACPProcess

        return ACPProcess(
            workspace=bot_cfg.workspace,
            session_id=session_id,
            model=bot_cfg.model,
            agent=bot_cfg.agent,
            bot_token=bot_cfg.telegram_token,
            copilot_api_port=copilot_api_port,
        )
    if bot_cfg.ai_backend == "codex-cli":
        from boxagent.agent.codex_process import CodexProcess

        return CodexProcess(
            workspace=bot_cfg.workspace,
            session_id=session_id,
            model=bot_cfg.model,
            agent=bot_cfg.agent,
            bot_token=bot_cfg.telegram_token,
            copilot_api_port=copilot_api_port,
            yolo=bot_cfg.yolo,
        )
    return ClaudeProcess(
        workspace=bot_cfg.workspace,
        session_id=session_id,
        model=bot_cfg.model,
        agent=bot_cfg.agent,
        bot_token=bot_cfg.telegram_token,
        copilot_api_port=copilot_api_port,
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
    git_dir = workspace / ".git"
    if git_dir.exists():
        return False
    git_dir.mkdir(parents=True, exist_ok=True)
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
    _channels: dict[str, TelegramChannel] = field(
        default_factory=dict, repr=False
    )
    _cli_processes: dict[str, object] = field(
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
    _copilot_api_proc: object = field(default=None, repr=False)
    _copilot_api_port: int = field(default=0, repr=False)
    _copilot_api_task: asyncio.Task | None = field(default=None, repr=False)
    _copilot_auth_proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _start_time: float = 0.0

    async def start(self) -> None:
        self._start_time = time.time()
        self._storage = Storage(local_dir=self.local_dir)
        logger.info("Gateway starting (node=%s)", self.config.node_id or "(any)")

        # Start copilot-api proxy if enabled
        if self.config.copilot_api:
            await self._start_copilot_api()

        for name, bot_cfg in self.config.bots.items():
            if not node_matches(bot_cfg.enabled_on_nodes, self.config.node_id):
                logger.info("Bot '%s' skipped (enabled_on_nodes=%s, current=%s)", name, bot_cfg.enabled_on_nodes, self.config.node_id)
                continue
            await self._start_bot(name, bot_cfg)

        # Start scheduler
        self._start_scheduler()

        # Start HTTP API
        await self._start_http()

        logger.info(
            "Gateway ready: %d bot(s) active", len(self.config.bots)
        )

    async def _start_bot(self, name: str, bot_cfg: BotConfig) -> None:
        session_id = None
        if _supports_persistent_session(bot_cfg.ai_backend):
            session_id = self._storage.load_session(
                name,
                backend=bot_cfg.ai_backend,
                workspace=bot_cfg.workspace,
            )

        cli = _create_backend(bot_cfg, session_id, self._copilot_api_port)
        cli.start()
        self._cli_processes[name] = cli

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

        channel = TelegramChannel(
            token=bot_cfg.telegram_token,
            allowed_users=bot_cfg.allowed_users,
            tool_calls_display=bot_cfg.display_tool_calls,
        )

        display_name = bot_cfg.display_name or name

        router = Router(
            cli_process=cli,
            channel=channel,
            allowed_users=bot_cfg.allowed_users,
            storage=self._storage,
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
        # If resuming a saved session, skip context re-injection —
        # the backend already has context from the original session.
        if session_id:
            router._session_context_injected = True
        channel.on_message = router.handle_message

        await channel.start()
        self._channels[name] = channel
        self._routers[name] = router

        # Notify user that bot is online
        chat_id = str(bot_cfg.allowed_users[0]) if bot_cfg.allowed_users else ""
        if chat_id:
            try:
                import datetime
                skill_count = len(linked)
                copilot_status = "on" if self._copilot_api_port else "off"
                info_lines = [
                    f"🟢 *{display_name}* is online",
                    f"node: `{self.config.node_id or '(any)'}`",
                    f"model: `{bot_cfg.model or 'default'}`",
                    f"backend: `{bot_cfg.ai_backend}`",
                    f"workspace: `{bot_cfg.workspace}`",
                    f"skills: {skill_count}",
                    f"copilot-api: {copilot_status}",
                    f"session: `{session_id or 'new'}`",
                    f"time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}",
                ]
                if git_created:
                    info_lines.append("⚠️ workspace was not a git repo, created .git for skill discovery")
                await channel.send_text(chat_id, "\n".join(info_lines))
            except Exception as e:
                logger.warning("Failed to send startup notification for '%s': %s", name, e)

        async def restart_bot(n=name, bc=bot_cfg):
            await self._restart_bot(n, bc)

        wd = Watchdog(
            cli_process=cli,
            channel=channel,
            chat_id=chat_id,
            bot_name=name,
            on_restart=restart_bot,
        )
        task = asyncio.create_task(wd.run_forever())
        self._watchdogs[name] = wd
        self._watchdog_tasks.append(task)

        logger.info("Bot '%s' started (session=%s)", name, session_id)

    def _start_scheduler(self) -> None:
        """Create and start the Scheduler after all active bots are online."""
        schedules_file = self.config_dir / "schedules.yaml"
        bot_refs: dict[str, BotRef] = {}
        for name in self._channels:
            bot_cfg = self.config.bots[name]
            chat_id = str(bot_cfg.allowed_users[0]) if bot_cfg.allowed_users else ""
            bot_refs[name] = BotRef(
                cli_process=self._cli_processes[name],
                channel=self._channels[name],
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
            copilot_api_port=self._copilot_api_port,
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

        new_cli = _create_backend(bot_cfg, session_id, self._copilot_api_port)
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
        self._clear_http_artifacts()

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

    async def _start_copilot_api(self) -> None:
        """Start copilot-api proxy.

        Always allocates a port upfront so env injection works immediately.
        If token is missing, runs auth in background — proxy starts once
        auth completes.
        """
        from boxagent.copilot_api import start_copilot_api, find_token_path, get_free_port

        # Always allocate port upfront so backends get env injected immediately
        self._copilot_api_port = get_free_port()
        logger.info("copilot-api port reserved: %d", self._copilot_api_port)

        result = await start_copilot_api(port=self._copilot_api_port)
        if result:
            self._copilot_api_proc = result
        elif find_token_path() is None:
            # No token — run auth flow in background
            self._copilot_api_task = asyncio.create_task(self._run_copilot_auth())

    async def _run_copilot_auth(self) -> None:
        """Background task: run auth, send device code to Telegram, then start proxy."""
        import re
        import shutil
        from boxagent.copilot_api import (
            find_token_path, start_copilot_api, get_auth_message,
            _AUTH_TIMEOUT,
        )

        xca = shutil.which("xc-copilot-api") or shutil.which("xc-copilot-api.exe")
        if not xca:
            logger.warning("Cannot run copilot-api auth: xc-copilot-api not found")
            return

        logger.info("Starting copilot-api auth flow...")
        proc = await asyncio.create_subprocess_exec(
            xca, "auth",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        self._copilot_auth_proc = proc

        # Read output to find device code, send to Telegram
        try:
            while True:
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=_AUTH_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.error("copilot-api auth timed out")
                    self._kill_proc_tree(proc)
                    return

                if not line:
                    break

                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue

                logger.debug("auth output: %s", text)

                m = re.search(r'code\s+"([A-Z0-9-]+)"\s+in\s+(https://\S+)', text)
                if m:
                    code, url = m.group(1), m.group(2)
                    msg = get_auth_message(code, url)
                    logger.info("Device code: %s", code)
                    # Send to all active channels
                    for ch_name, channel in self._channels.items():
                        bot_cfg = self.config.bots.get(ch_name)
                        if bot_cfg and bot_cfg.allowed_users:
                            chat_id = str(bot_cfg.allowed_users[0])
                            try:
                                await channel.send_text(chat_id, msg)
                            except Exception as e:
                                logger.warning("Failed to send auth code to %s: %s", ch_name, e)
                            break  # send to first bot only

            await proc.wait()

            if proc.returncode == 0 and find_token_path():
                logger.info("copilot-api auth completed, starting proxy...")
                from boxagent.copilot_api import start_copilot_api
                result = await start_copilot_api(port=self._copilot_api_port)
                if result:
                    self._copilot_api_proc = result
            else:
                logger.error("copilot-api auth failed (exit=%s)", proc.returncode)

        except asyncio.CancelledError:
            self._kill_proc_tree(proc)
        except Exception as e:
            logger.error("copilot-api auth error: %s", e)
            try:
                self._kill_proc_tree(proc)
            except Exception:
                pass

    @staticmethod
    def _kill_proc_tree(proc: asyncio.subprocess.Process) -> None:
        """Kill a subprocess and its children via process group."""
        if proc.returncode is not None:
            return
        pid = proc.pid
        if sys.platform != "win32":
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError):
                pass
        proc.kill()

    async def _stop_copilot_api(self) -> None:
        """Stop copilot-api proxy if we started it."""
        if self._copilot_api_task:
            self._copilot_api_task.cancel()
            try:
                await self._copilot_api_task
            except (asyncio.CancelledError, Exception):
                pass
            self._copilot_api_task = None
        # Kill auth subprocess if still running
        if self._copilot_auth_proc and self._copilot_auth_proc.returncode is None:
            try:
                self._kill_proc_tree(self._copilot_auth_proc)
                await self._copilot_auth_proc.wait()
            except Exception:
                pass
            self._copilot_auth_proc = None
        if self._copilot_api_proc:
            from boxagent.copilot_api import stop_copilot_api
            await stop_copilot_api(self._copilot_api_proc)
            self._copilot_api_proc = None
            self._copilot_api_port = 0

    async def stop(self) -> None:
        logger.info("Gateway shutting down...")

        # Stop HTTP API
        await self._stop_http()

        # Stop copilot-api
        await self._stop_copilot_api()

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

        for name, cli in self._cli_processes.items():
            try:
                # Save session before stopping
                if self._storage and cli.session_id:
                    router = self._routers.get(name)
                    backend = getattr(router, "ai_backend", "")
                    workspace = getattr(router, "workspace", "")
                    if not backend or not workspace:
                        bot_cfg = self.config.bots.get(name)
                        if bot_cfg:
                            backend = backend or getattr(bot_cfg, "ai_backend", "")
                            workspace = workspace or getattr(bot_cfg, "workspace", "")
                    self._storage.save_session(
                        name,
                        cli.session_id,
                        backend=backend,
                        workspace=workspace,
                    )
                await cli.stop()
            except Exception as e:
                logger.error("Error stopping CLI %s: %s", name, e)

        logger.info("Gateway stopped")
