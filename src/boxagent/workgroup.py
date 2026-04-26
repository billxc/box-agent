"""WorkgroupManager — manages workgroup lifecycle, specialists, and delegation."""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from boxagent.config import BotConfig, SpecialistConfig, WorkgroupConfig
from boxagent.router import Router
from boxagent.session_pool import SessionPool

logger = logging.getLogger(__name__)


@dataclass
class WorkgroupManager:
    """Manages workgroup admin + specialist agents.

    Created by Gateway, holds all workgroup-specific state.
    """

    config: dict[str, WorkgroupConfig]  # wg_name → config
    config_dir: str = ""
    node_id: str = ""
    local_dir: Path | None = None
    start_time: float = 0.0
    storage: object = None
    discord_channels: dict[str, object] = field(default_factory=dict)  # bot_id → DiscordChannel
    # Internal state
    routers: dict[str, Router] = field(default_factory=dict)    # name → Router
    pools: dict[str, SessionPool] = field(default_factory=dict)  # name → Pool
    procs: dict[str, object] = field(default_factory=dict)       # name → CLI process

    # Injected by Gateway
    _create_backend: object = None  # Callable[[BotConfig, str|None], object]
    _ensure_git_repo: object = None  # Callable[[Path], bool]
    _sync_skills: object = None      # Callable[[str, list, str], list]

    def _specialists_file(self) -> Path:
        return self.local_dir / "workgroup_specialists.yaml"

    def _load_saved_specialists(self, wg_name: str) -> dict[str, SpecialistConfig]:
        """Load dynamically created specialists from local storage."""
        path = self._specialists_file()
        if not path.is_file():
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            entries = data.get(wg_name, {})
            result = {}
            for sp_name, sp_raw in entries.items():
                result[sp_name] = SpecialistConfig(
                    name=sp_name,
                    model=sp_raw.get("model", ""),
                    workspace=sp_raw.get("workspace", ""),
                    ai_backend=sp_raw.get("ai_backend", ""),
                    display_name=sp_raw.get("display_name", sp_name),
                    discord_channel=int(sp_raw.get("discord_channel", 0)),
                )
            return result
        except Exception as e:
            logger.warning("Failed to load saved specialists: %s", e)
            return {}

    def _save_specialist(self, wg_name: str, sp: SpecialistConfig) -> None:
        """Persist a dynamically created specialist to local storage."""
        path = self._specialists_file()
        data = {}
        if path.is_file():
            try:
                with open(path, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
            except Exception:
                pass
        data.setdefault(wg_name, {})[sp.name] = {
            "model": sp.model,
            "workspace": sp.workspace,
            "ai_backend": sp.ai_backend,
            "display_name": sp.display_name,
            "discord_channel": sp.discord_channel,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False)

    def _make_backend(self, bot_cfg: BotConfig, session_id=None):
        return self._create_backend(bot_cfg, session_id)

    def _create_specialist_agent(
        self, sp_name: str, sp_cfg, wg_cfg: WorkgroupConfig, dc_channel,
    ) -> Router:
        """Create backend, pool, router for a single specialist. Returns the Router."""
        syn_cfg = BotConfig(
            name=sp_name,
            ai_backend=sp_cfg.ai_backend,
            workspace=sp_cfg.workspace,
            model=sp_cfg.model,
            yolo=wg_cfg.yolo,
            extra_skill_dirs=sp_cfg.extra_skill_dirs,
            display_name=sp_cfg.display_name,
        )

        cli = self._make_backend(syn_cfg)
        cli.start()
        self.procs[sp_name] = cli

        def _factory(cfg=syn_cfg):
            return self._make_backend(cfg)

        pool = SessionPool(
            size=2,
            default_model=syn_cfg.model,
            default_workspace=syn_cfg.workspace,
            storage=self.storage,
            bot_name=sp_name,
        )
        pool.start(_factory)
        self.pools[sp_name] = pool

        if syn_cfg.workspace and self._ensure_git_repo:
            self._ensure_git_repo(Path(syn_cfg.workspace))
        if syn_cfg.extra_skill_dirs and self._sync_skills:
            self._sync_skills(syn_cfg.workspace, syn_cfg.extra_skill_dirs, syn_cfg.ai_backend)

        sp_router = Router(
            cli_process=cli,
            channel=dc_channel,
            allowed_users=wg_cfg.allowed_users,
            storage=self.storage,
            pool=pool,
            bot_name=sp_name,
            display_name=syn_cfg.display_name,
            config_dir=self.config_dir,
            node_id=self.node_id,
            local_dir=self.local_dir,
            start_time=self.start_time,
            workspace=syn_cfg.workspace,
            extra_skill_dirs=syn_cfg.extra_skill_dirs,
            ai_backend=syn_cfg.ai_backend,
        )
        if dc_channel:
            sp_router._channels["discord"] = dc_channel
        self.routers[sp_name] = sp_router
        return sp_router

    async def start_workgroup(self, wg_name: str, wg_cfg: WorkgroupConfig) -> None:
        """Initialize a standalone workgroup: create admin + specialist agents."""
        dc_channel = None
        if wg_cfg.discord_bot_id:
            dc_channel = self.discord_channels.get(wg_cfg.discord_bot_id)

        # --- Create admin agent ---
        admin_ws = wg_cfg.admin_workspace
        admin_bot_cfg = BotConfig(
            name=wg_name,
            ai_backend=wg_cfg.ai_backend,
            workspace=admin_ws,
            model=wg_cfg.model,
            yolo=wg_cfg.yolo,
            allowed_users=wg_cfg.allowed_users,
            display_name=wg_cfg.display_name,
            display_tool_calls=wg_cfg.display_tool_calls,
            extra_skill_dirs=wg_cfg.extra_skill_dirs,
        )

        admin_cli = self._make_backend(admin_bot_cfg)
        admin_cli.is_workgroup_admin = True
        admin_cli.start()
        self.procs[wg_name] = admin_cli

        def _admin_factory(cfg=admin_bot_cfg):
            proc = self._make_backend(cfg)
            proc.is_workgroup_admin = True
            return proc

        admin_pool = SessionPool(
            size=3,
            default_model=wg_cfg.model,
            default_workspace=admin_ws,
            storage=self.storage,
            bot_name=wg_name,
        )
        admin_pool.start(_admin_factory)
        self.pools[wg_name] = admin_pool

        if admin_ws and self._ensure_git_repo:
            self._ensure_git_repo(Path(admin_ws))
        if wg_cfg.extra_skill_dirs and self._sync_skills:
            self._sync_skills(admin_ws, wg_cfg.extra_skill_dirs, wg_cfg.ai_backend)

        admin_router = Router(
            cli_process=admin_cli,
            channel=dc_channel,
            allowed_users=wg_cfg.allowed_users,
            storage=self.storage,
            pool=admin_pool,
            bot_name=wg_name,
            display_name=wg_cfg.display_name,
            config_dir=self.config_dir,
            node_id=self.node_id,
            local_dir=self.local_dir,
            start_time=self.start_time,
            workspace=admin_ws,            extra_skill_dirs=wg_cfg.extra_skill_dirs,
            ai_backend=wg_cfg.ai_backend,
        )
        self.routers[wg_name] = admin_router

        # Register admin on Discord category
        if dc_channel and wg_cfg.admin_discord_category:
            dc_channel.register_route(
                admin_router.handle_message,
                [wg_cfg.admin_discord_category],
            )
            admin_router._channels["discord"] = dc_channel
            logger.info(
                "Workgroup '%s': admin registered on Discord category %d",
                wg_name, wg_cfg.admin_discord_category,
            )

        # --- Create specialists (config + saved dynamic ones) ---
        saved = self._load_saved_specialists(wg_name)
        for sp_name, sp_cfg in saved.items():
            if sp_name not in wg_cfg.specialists:
                wg_cfg.specialists[sp_name] = sp_cfg
                logger.info("Workgroup '%s': restored saved specialist '%s'", wg_name, sp_name)

        specialist_names = []
        for sp_name, sp_cfg in wg_cfg.specialists.items():
            self._create_specialist_agent(sp_name, sp_cfg, wg_cfg, dc_channel)
            specialist_names.append(sp_name)
            logger.info(
                "Workgroup '%s': specialist '%s' started (model=%s)",
                wg_name, sp_name, sp_cfg.model,
            )

        admin_router.workgroup_agents = specialist_names
        logger.info("Workgroup '%s' ready: specialists=%s", wg_name, specialist_names)

    async def send_to_specialist(
        self, target: str, text: str, from_bot: str = "",
    ) -> str:
        """Send a message to a specialist and return the response."""
        router = self.routers.get(target)
        if router is None:
            return f"Error: specialist '{target}' not found"

        # Find specialist's Discord channel (if configured)
        sp_discord_channel = 0
        dc_channel = None
        wg_display = from_bot or "admin"
        for wg_cfg in self.config.values():
            if target in wg_cfg.specialists:
                sp_discord_channel = wg_cfg.specialists[target].discord_channel
                if wg_cfg.discord_bot_id:
                    dc_channel = self.discord_channels.get(wg_cfg.discord_bot_id)
                wg_display = wg_cfg.display_name or from_bot or "admin"
                break

        if sp_discord_channel and dc_channel:
            try:
                await dc_channel.send_via_webhook(sp_discord_channel, wg_display, text)
            except Exception as e:
                logger.warning("Failed to post task to specialist channel: %s", e)
            chat_id = str(sp_discord_channel)
        else:
            chat_id = f"wg:{target}"

        return await router.dispatch_sync(text, chat_id, from_bot=from_bot)

    async def create_specialist(
        self, wg_name: str, sp_name: str,
        model: str = "", workspace: str = "",
    ) -> dict:
        """Dynamically create a specialist agent in a workgroup."""
        wg_cfg = self.config.get(wg_name)
        if wg_cfg is None:
            return {"ok": False, "error": f"workgroup '{wg_name}' not found"}

        if sp_name in self.routers:
            return {"ok": False, "error": f"specialist '{sp_name}' already exists"}

        # Create Discord channel (if workgroup has Discord)
        discord_channel_id = 0
        dc_channel = None
        if wg_cfg.discord_bot_id and wg_cfg.admin_discord_category:
            dc_channel = self.discord_channels.get(wg_cfg.discord_bot_id)
            if dc_channel:
                try:
                    discord_channel_id = await dc_channel.create_text_channel(
                        wg_cfg.admin_discord_category, sp_name,
                    )
                except Exception as e:
                    logger.warning("Failed to create Discord channel for '%s': %s", sp_name, e)

        sp_cfg = SpecialistConfig(
            name=sp_name,
            model=model or wg_cfg.model,
            workspace=workspace or wg_cfg.specialist_workspace(sp_name),
            ai_backend=wg_cfg.ai_backend,
            display_name=sp_name,
            discord_channel=discord_channel_id,
        )

        self._create_specialist_agent(sp_name, sp_cfg, wg_cfg, dc_channel)

        # Persist
        wg_cfg.specialists[sp_name] = sp_cfg
        self._save_specialist(wg_name, sp_cfg)

        # Update admin's agent list
        admin_router = self.routers.get(wg_name)
        if admin_router:
            admin_router.workgroup_agents = list(wg_cfg.specialists.keys())

        logger.info(
            "Workgroup '%s': dynamically created specialist '%s' (channel=%d, model=%s)",
            wg_name, sp_name, discord_channel_id, sp_cfg.model,
        )
        return {"ok": True, "channel_id": discord_channel_id}

    async def stop(self) -> None:
        """Stop all workgroup processes and pools."""
        for name, cli in self.procs.items():
            try:
                await cli.stop()
            except Exception as e:
                logger.error("Error stopping workgroup CLI %s: %s", name, e)
        for name, pool in self.pools.items():
            try:
                await pool.stop()
            except Exception as e:
                logger.error("Error stopping workgroup pool %s: %s", name, e)
