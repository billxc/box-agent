"""Bot orchestration — per-bot lifecycle.

``AgentManager`` (composition) owns the per-bot lifecycle. Gateway holds
one as ``self._bots`` and drives it via ``start_bot`` / ``start_raw_bot``
/ ``restart_bot`` / ``on_backend_switched``.

Backend-factory and workspace helpers live in sibling modules
(``backend_factory.py`` / ``workspace.py``) — both AgentManager and
WorkgroupManager import them directly.
"""

import asyncio
import datetime
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from boxagent.agent.backend_factory import create_backend
from boxagent.agent.claude_process import ClaudeProcess
from boxagent.agent.protocol import AgentBackend, BACKEND_KINDS
from boxagent.agent.workspace import ensure_git_repo, sync_skills
from boxagent.config import AppConfig, BotConfig
from boxagent.router import Router
from boxagent.sessions import RawSessionPool, SessionPool, Storage
from boxagent.transports.base import Channel
from boxagent.transports.web import WebChannel
from boxagent.watchdog import Watchdog

if TYPE_CHECKING:
    from boxagent.scheduler import Scheduler

logger = logging.getLogger(__name__)


def _supports_persistent_session(ai_backend: str) -> bool:
    """Whether a backend can resume a saved session after restart."""
    return ai_backend in BACKEND_KINDS


# ── Composition: AgentManager ──
#
# Two-phase DI:
#   Phase 1 (constructor): infrastructure — config, storage, shared dicts.
#                          Shared dicts are passed by reference because other
#                          managers (web/server, topology, peer) read them.
#   Phase 2 (set_scheduler): cross-manager refs that don't exist at __init__
#                            time (Scheduler is created after bots are up).
#   Phase 3 (start_bot/start_raw_bot): driven by Gateway.start().

class AgentManager:
    def __init__(
        self,
        *,
        config: AppConfig,
        config_dir: Path,
        storage: Storage,
        start_time: float,
        gateway: "Any" = None,
    ) -> None:
        self.config = config
        self.config_dir = config_dir
        self.storage = storage
        self.start_time = start_time
        self.gateway = gateway
        # State this manager owns. Other managers that need a read view
        # (TopologyService → web_channels, WebHttpServer → pools/web_channels,
        # WorkgroupManager → web_channels) receive the dict by reference at
        # their own construction time.
        self.backends: dict[str, AgentBackend] = {}
        self.pools: dict = {}
        self.routers: dict[str, "Router"] = {}
        self.channels: dict[str, Channel] = {}
        self.web_channels: dict[str, WebChannel] = {}
        self.watchdogs: dict[str, Watchdog] = {}
        self.watchdog_tasks: list[asyncio.Task] = []
        # Phase 2 deps
        self.scheduler: "Scheduler | None" = None

    def set_scheduler(self, scheduler: "Scheduler") -> None:
        self.scheduler = scheduler

    async def start_all_for_node(self, node_id: str) -> None:
        """Start every bot whose ``enabled_on_nodes`` matches ``node_id``,
        then register the synthetic ``raw`` passthrough bot.

        Skipped bots are logged but not raised; an empty config is fine.
        """
        from boxagent.config import node_matches
        for name, bot_config in self.config.bots.items():
            if not node_matches(bot_config.enabled_on_nodes, node_id):
                logger.info(
                    "Bot '%s' skipped (enabled_on_nodes=%s, current=%s)",
                    name, bot_config.enabled_on_nodes, node_id,
                )
                continue
            await self.start_bot(name, bot_config)
        await self.start_raw_bot()

    def build_scheduler_refs(self) -> dict:
        """Build the per-bot ``BotRef`` map the Scheduler consumes.

        Walks the routers/backends/channels dicts this manager owns. Skips
        the synthetic ``raw`` bot (web-only passthrough — never a scheduler
        target).
        """
        from boxagent.scheduler import BotRef
        refs: dict = {}
        for name in self.routers:
            if name == "raw":
                continue
            bot_config = self.config.bots[name]
            chat_id = str(bot_config.allowed_users[0]) if bot_config.allowed_users else ""
            refs[name] = BotRef(
                backend=self.backends[name],
                channel=self.channels.get(name),
                chat_id=chat_id,
                ai_backend=bot_config.ai_backend,
                telegram_token=bot_config.telegram_token,
            )
        return refs

    async def stop(self) -> None:
        """Tear down everything this manager owns: watchdog tasks, channels,
        web_channels, backends (saving session_id first), pools.

        Errors per resource are logged and swallowed so a single bad stop()
        can't block the rest of teardown.
        """
        for task in self.watchdog_tasks:
            task.cancel()
        if self.watchdog_tasks:
            await asyncio.gather(*self.watchdog_tasks, return_exceptions=True)
        self.watchdog_tasks.clear()

        for name, channel in self.channels.items():
            try:
                await channel.stop()
            except Exception as e:
                logger.error("Error stopping channel %s: %s", name, e)

        for name, channel in self.web_channels.items():
            try:
                await channel.stop()
            except Exception as e:
                logger.error("Error stopping web channel %s: %s", name, e)

        for name, backend in self.backends.items():
            try:
                if self.storage and backend.session_id:
                    self.storage.save_session(name, backend.session_id)
                await backend.stop()
            except Exception as e:
                logger.error("Error stopping backend %s: %s", name, e)

        for name, pool in self.pools.items():
            try:
                await pool.stop()
            except Exception as e:
                logger.error("Error stopping pool %s: %s", name, e)

    async def start_bot(self, name: str, bot_config: BotConfig) -> None:
        session_id = None
        if _supports_persistent_session(bot_config.ai_backend):
            saved = self.storage.load_session(name)
            if isinstance(saved, dict):
                session_id = saved.get("session_id")
            elif isinstance(saved, str):
                session_id = saved

        backend = create_backend(bot_config, session_id, gateway=self.gateway)
        backend.start()
        self.backends[name] = backend

        def _factory():
            return create_backend(bot_config, None, gateway=self.gateway)

        pool = SessionPool(
            size=3,
            default_model=bot_config.model,
            default_workspace=bot_config.workspace,
            storage=self.storage,
            bot_name=name,
        )
        pool.start(_factory)
        self.pools[name] = pool

        ws_path = Path(bot_config.workspace)
        git_created = ensure_git_repo(ws_path)

        linked: list[str] = []
        if bot_config.extra_skill_dirs:
            linked = sync_skills(
                bot_config.workspace,
                bot_config.extra_skill_dirs,
                bot_config.ai_backend,
            )
            logger.info("Bot '%s' synced %d skill(s): %s", name, len(linked), linked)

        display_name = bot_config.display_name or name

        primary_channel = None

        if bot_config.telegram_token:
            from boxagent.transports.telegram import TelegramChannel
            channel = TelegramChannel(
                token=bot_config.telegram_token,
                allowed_users=bot_config.allowed_users,
                tool_calls_display=bot_config.display_tool_calls,
            )
            primary_channel = channel
            self.channels[name] = channel

        router = Router(
            backend=backend,
            channel=primary_channel,
            allowed_users=bot_config.allowed_users,
            storage=self.storage,
            pool=pool,
            bot_name=name,
            display_name=display_name,
            config_dir=str(self.config_dir),
            node_id=self.config.node_id,
            local_dir=self.storage.local_dir if self.storage else None,
            start_time=self.start_time,
            workspace=bot_config.workspace,
            extra_skill_dirs=bot_config.extra_skill_dirs,
            ai_backend=bot_config.ai_backend,
            on_backend_switched=self.on_backend_switched,
            has_peer_channel=False,
            telegram_token=bot_config.telegram_token,
        )

        if name in self.channels:
            router._channels["telegram"] = self.channels[name]
            self.channels[name].on_message = router.handle_message
            await self.channels[name].start()

        if bot_config.web_enabled:
            web_channel = WebChannel(bot_name=name)
            web_channel.on_message = router.handle_message
            self.web_channels[name] = web_channel
            router._channels["web"] = web_channel
            logger.info("Bot '%s' web channel enabled", name)

        self.routers[name] = router

        skill_count = len(linked)
        channels_active = []
        if bot_config.telegram_token:
            channels_active.append("telegram")
        if bot_config.web_enabled:
            channels_active.append("web")
        info_lines = [
            f"\U0001f7e2 *{display_name}* is online",
            f"node: `{self.config.node_id or '(any)'}`",
            f"model: `{bot_config.model or 'default'}`",
            f"backend: `{bot_config.ai_backend}`",
            f"workspace: `{bot_config.workspace}`",
            f"channels: {', '.join(channels_active)}",
            f"skills: {skill_count}",
            f"time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}",
        ]
        if git_created:
            info_lines.append("⚠️ workspace was not a git repo, created .git for skill discovery")
        notify_text = "\n".join(info_lines)

        tg_chat_id = str(bot_config.telegram_allowed_users[0]) if bot_config.telegram_token and bot_config.telegram_allowed_users else ""
        if tg_chat_id and name in self.channels:
            async def _send_tg_notify(channel=self.channels[name], chat_id=tg_chat_id, text=notify_text, bot_name=name):
                try:
                    await channel.send_text(chat_id, text)
                except Exception as e:
                    logger.warning("Failed to send Telegram startup notification for '%s': %s", bot_name, e)
            asyncio.create_task(_send_tg_notify())

        async def restart_bot(n=name, bc=bot_config):
            await self.restart_bot(n, bc)

        wd_chat_id = tg_chat_id

        wd = Watchdog(
            backend=backend,
            channel=primary_channel,
            chat_id=wd_chat_id,
            bot_name=name,
            on_restart=restart_bot,
            pool=pool,
        )
        task = asyncio.create_task(wd.run_forever())
        self.watchdogs[name] = wd
        self.watchdog_tasks.append(task)

        logger.info("Bot '%s' started (session=%s)", name, session_id)

    def _raw_backend_factory(self, *, backend: str, workspace: str, model: str,
                             session_id: str | None, bot_name: str) -> AgentBackend:
        config = BotConfig(
            name=bot_name,
            ai_backend=backend or "claude-cli",
            workspace=workspace or "",
            model=model or "",
            yolo=True,
            passthrough=True,
        )
        return create_backend(config, session_id, gateway=self.gateway)

    async def start_raw_bot(self) -> None:
        name = "raw"
        bot_config = BotConfig(
            name=name,
            ai_backend="claude-cli",
            workspace="",
            display_name="Raw passthrough",
            passthrough=True,
            web_enabled=True,
            yolo=True,
        )
        self.config.bots[name] = bot_config

        pool = RawSessionPool(
            storage=self.storage,
            bot_name=name,
            backend_factory=self._raw_backend_factory,
        )
        pool.start()
        self.pools[name] = pool

        stub = ClaudeProcess(
            workspace="/tmp",
            session_id=None,
            model="",
            agent="",
            bot_name=name,
            yolo=True,
        )
        self.backends[name] = stub

        router = Router(
            backend=stub,
            channel=None,
            allowed_users=[],
            storage=self.storage,
            pool=pool,
            bot_name=name,
            display_name=bot_config.display_name,
            config_dir=str(self.config_dir),
            node_id=self.config.node_id,
            local_dir=self.storage.local_dir if self.storage else None,
            start_time=self.start_time,
            workspace="",
            extra_skill_dirs=[],
            ai_backend="claude-cli",
            on_backend_switched=self.on_backend_switched,
            has_peer_channel=False,
            telegram_token="",
            passthrough=True,
        )

        web_channel = WebChannel(bot_name=name)
        web_channel.on_message = router.handle_message
        self.web_channels[name] = web_channel
        router._channels["web"] = web_channel

        self.routers[name] = router
        logger.info("Bot 'raw' (passthrough, web-only) registered")

    async def restart_bot(self, name: str, bot_config: BotConfig) -> None:
        old_backend = self.backends.get(name)
        session_id = None
        if old_backend and _supports_persistent_session(bot_config.ai_backend):
            session_id = old_backend.session_id
        if old_backend:
            try:
                await old_backend.stop()
            except Exception:
                pass

        new_backend = create_backend(bot_config, session_id, gateway=self.gateway)
        new_backend.start()
        self.backends[name] = new_backend

        if bot_config.extra_skill_dirs:
            sync_skills(
                bot_config.workspace,
                bot_config.extra_skill_dirs,
                bot_config.ai_backend,
            )

        if name in self.routers:
            self.routers[name].backend = new_backend

        if self.scheduler and name in self.scheduler.bot_refs:
            self.scheduler.bot_refs[name].backend = new_backend
            self.scheduler.bot_refs[name].telegram_token = bot_config.telegram_token

        if name in self.watchdogs:
            self.watchdogs[name].backend = new_backend

        logger.info("Bot '%s' backend restarted", name)

    async def on_backend_switched(self, bot_name: str, new_backend: AgentBackend, new_kind: str) -> None:
        self.backends[bot_name] = new_backend
        if self.scheduler and bot_name in self.scheduler.bot_refs:
            self.scheduler.bot_refs[bot_name].backend = new_backend
            self.scheduler.bot_refs[bot_name].ai_backend = new_kind
        if bot_name in self.watchdogs:
            self.watchdogs[bot_name].backend = new_backend
        logger.info("Bot '%s' backend switched to %s (refs synced)", bot_name, new_kind)
