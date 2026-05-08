"""Bot orchestration — backend factory, workspace setup, lifecycle.

Free helpers (`_create_backend`, `_ensure_git_repo`, `sync_skills`,
`_supports_persistent_session`) are also injected into WorkgroupManager and
re-exported via ``boxagent.gateway`` for back-compat with tests / commands.

``BotsMixin`` is mounted on Gateway and owns the per-bot lifecycle:
``_start_bot``, ``_restart_bot``, ``_start_raw_bot``, plus helpers used by
the Router (``_on_backend_switched``, ``_raw_backend_factory``).
"""

import asyncio
import datetime
import logging
from pathlib import Path

from boxagent.agent.claude_process import ClaudeProcess
from boxagent.agent.protocol import AgentBackend
from boxagent.config import BotConfig
from boxagent.sessions import SessionPool
from boxagent.sessions.raw_pool import RawSessionPool
from boxagent.transports.web import WebChannel

logger = logging.getLogger(__name__)


# ── Free helpers (also re-exported via boxagent.gateway for back-compat) ──

def _supports_persistent_session(ai_backend: str) -> bool:
    """Whether a backend can resume a saved session after restart."""
    return ai_backend in ("claude-cli", "codex-cli")


def _create_backend(bot_cfg: BotConfig, session_id: str | None) -> AgentBackend:
    """Instantiate the AI backend based on config.

    Looks up ``ClaudeProcess`` via ``boxagent.gateway`` so tests can patch
    ``boxagent.gateway.ClaudeProcess`` to inject mocks.
    """
    from boxagent import gateway as _gw_pkg
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
    - Codex CLI backend: {workspace}/.agents/skills/
    """
    skills_root = ".agents" if ai_backend == "codex-cli" else ".claude"
    skills_dir = Path(workspace) / skills_root / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

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


# ── Mixin: per-bot lifecycle ──

class BotsMixin:
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

        ws_path = Path(bot_cfg.workspace)
        git_created = _ensure_git_repo(ws_path)

        linked: list[str] = []
        if bot_cfg.extra_skill_dirs:
            linked = sync_skills(
                bot_cfg.workspace,
                bot_cfg.extra_skill_dirs,
                bot_cfg.ai_backend,
            )
            logger.info("Bot '%s' synced %d skill(s): %s", name, len(linked), linked)

        display_name = bot_cfg.display_name or name

        primary_channel = None

        if bot_cfg.telegram_token:
            from boxagent.transports.telegram import TelegramChannel
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
            has_peer_channel=False,
            telegram_token=bot_cfg.telegram_token,
        )

        if name in self._channels:
            router._channels["telegram"] = self._channels[name]
            self._channels[name].on_message = router.handle_message
            await self._channels[name].start()

        if bot_cfg.web_enabled:
            web_channel = WebChannel(bot_name=name)
            web_channel.on_message = router.handle_message
            self._web_channels[name] = web_channel
            router._channels["web"] = web_channel
            logger.info("Bot '%s' web channel enabled", name)

        self._routers[name] = router

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
            yolo=True,
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
        from boxagent import gateway as _gw_pkg
        name = "raw"
        bot_cfg = BotConfig(
            name=name,
            ai_backend="claude-cli",
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

        stub = ClaudeProcess(
            workspace="/tmp",
            session_id=None,
            model="",
            agent="",
            bot_name=name,
            yolo=True,
        )
        self._cli_processes[name] = stub

        router = _gw_pkg.Router(
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

        if bot_cfg.extra_skill_dirs:
            sync_skills(
                bot_cfg.workspace,
                bot_cfg.extra_skill_dirs,
                bot_cfg.ai_backend,
            )

        if name in self._routers:
            self._routers[name].cli_process = new_cli

        if self._scheduler and name in self._scheduler.bot_refs:
            self._scheduler.bot_refs[name].cli_process = new_cli
            self._scheduler.bot_refs[name].telegram_token = bot_cfg.telegram_token

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
