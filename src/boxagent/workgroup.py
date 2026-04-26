"""WorkgroupManager — manages workgroup lifecycle, specialists, and delegation."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from boxagent.config import BotConfig, SpecialistConfig, WorkgroupConfig
from boxagent.heartbeat import HeartbeatManager
from boxagent.router import Router
from boxagent.session_pool import SessionPool
from boxagent.workspace_templates import seed_admin_workspace, seed_specialist_workspace

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
    # Async task tracking
    _tasks: dict[str, asyncio.Task] = field(default_factory=dict, repr=False)
    _task_results: dict[str, dict] = field(default_factory=dict, repr=False)
    _task_counter: int = field(default=0, repr=False)
    _heartbeats: dict[str, HeartbeatManager] = field(default_factory=dict, repr=False)
    _builtin_specialists: dict[str, set[str]] = field(default_factory=dict, repr=False)  # wg → names from config.yaml

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

        # Seed specialist workspace templates (CLAUDE.md, skills)
        seed_specialist_workspace(syn_cfg.workspace, sp_name, wg_cfg.name)

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

        # Seed admin workspace templates (CLAUDE.md, skills, HEARTBEAT.md)
        seed_admin_workspace(admin_ws, wg_name, list(wg_cfg.specialists.keys()))

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
        self._builtin_specialists[wg_name] = set(wg_cfg.specialists.keys())
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

        # --- Start heartbeat (if configured) ---
        if wg_cfg.heartbeat_interval_seconds > 0:
            hb = HeartbeatManager(
                wg_name=wg_name,
                admin_pool=admin_pool,
                admin_router=admin_router,
                workspace=admin_ws,
                interval_seconds=wg_cfg.heartbeat_interval_seconds,
                ai_backend=wg_cfg.ai_backend,
                model=wg_cfg.model,
                yolo=wg_cfg.yolo,
                discord_channel=dc_channel,
                discord_chat_id=str(wg_cfg.admin_discord_category) if wg_cfg.admin_discord_category else "",
            )
            hb.start()
            self._heartbeats[wg_name] = hb

    async def send_to_specialist(
        self, target: str, text: str, from_bot: str = "",
        reply_chat_id: str = "",
    ) -> dict:
        """Dispatch a task to a specialist asynchronously.

        Returns immediately with a task_id. The specialist processes in the
        background; results are visible in the Discord channel (if configured).
        When done, a summary is posted back to reply_chat_id (admin's channel).
        """
        router = self.routers.get(target)
        if router is None:
            return {"ok": False, "error": f"specialist '{target}' not found"}

        self._task_counter += 1
        task_id = f"{target}-{self._task_counter}"

        # Resolve Discord channel info
        sp_discord_channel = 0
        dc_channel = None
        wg_display = from_bot or "admin"
        wg_name = ""
        for name, wg_cfg in self.config.items():
            if target in wg_cfg.specialists:
                sp_discord_channel = wg_cfg.specialists[target].discord_channel
                if wg_cfg.discord_bot_id:
                    dc_channel = self.discord_channels.get(wg_cfg.discord_bot_id)
                wg_display = wg_cfg.display_name or from_bot or "admin"
                wg_name = name
                break

        chat_id = str(sp_discord_channel) if sp_discord_channel else f"wg:{target}"

        async def _run():
            result = ""
            try:
                # Post task in specialist's channel via webhook
                if sp_discord_channel and dc_channel:
                    try:
                        await dc_channel.send_via_webhook(sp_discord_channel, wg_display, text)
                    except Exception as e:
                        logger.warning("Failed to post task to specialist channel: %s", e)

                result = await router.dispatch_sync(text, chat_id, from_bot=from_bot)
                self._task_results[task_id] = {
                    "status": "done",
                    "result": result,
                    "finished_at": time.time(),
                }
                logger.info("Task %s completed (%d chars)", task_id, len(result))
            except Exception as e:
                self._task_results[task_id] = {
                    "status": "error",
                    "error": str(e),
                    "finished_at": time.time(),
                }
                logger.error("Task %s failed: %s", task_id, e)
                result = f"Error: {e}"

            # Callback: notify admin's channel via allowed webhook
            if reply_chat_id and dc_channel:
                preview = result[:200] + "..." if len(result) > 200 else result
                callback_text = f"**[{target}]** task done:\n{preview}"
                try:
                    wh = await dc_channel.ensure_allowed_webhook(
                        "TaskNotification", reply_chat_id,
                    )
                    if wh:
                        await wh.send(callback_text, wait=True)
                    else:
                        await dc_channel.send_text(reply_chat_id, callback_text)
                except Exception as e:
                    logger.warning("Failed to send task callback: %s", e)

        self._task_results[task_id] = {"status": "running"}
        task = asyncio.create_task(_run())
        self._tasks[task_id] = task

        return {"ok": True, "task_id": task_id, "specialist": target}

    def get_task_result(self, task_id: str) -> dict:
        """Check the status/result of an async task."""
        info = self._task_results.get(task_id)
        if info is None:
            return {"ok": False, "error": f"task '{task_id}' not found"}
        return {"ok": True, **info}

    def reset_specialist(self, target: str) -> dict:
        """Clear a specialist's session so the next task starts fresh."""
        pool = self.pools.get(target)
        if pool is None:
            return {"ok": False, "error": f"specialist '{target}' not found"}
        # chat_id used by send_to_specialist
        for wg_cfg in self.config.values():
            if target in wg_cfg.specialists:
                sp = wg_cfg.specialists[target]
                chat_id = str(sp.discord_channel) if sp.discord_channel else f"wg:{target}"
                pool.clear_session(chat_id)
                logger.info("Reset session for specialist '%s' (chat_id=%s)", target, chat_id)
                return {"ok": True}
        return {"ok": False, "error": f"specialist '{target}' not in any workgroup"}

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

    async def delete_specialist(self, sp_name: str) -> dict:
        """Delete a dynamically created specialist.

        Stops its process and pool, removes from routing and persistence.
        Built-in specialists (defined in config.yaml) cannot be deleted.
        """
        if sp_name not in self.routers:
            return {"ok": False, "error": f"specialist '{sp_name}' not found"}

        # Check if built-in
        for wg_name, builtin_names in self._builtin_specialists.items():
            if sp_name in builtin_names:
                return {
                    "ok": False,
                    "error": f"specialist '{sp_name}' is built-in (defined in config.yaml) and cannot be deleted",
                }

        # Find which workgroup owns this specialist
        wg_name = ""
        for name, wg_cfg in self.config.items():
            if sp_name in wg_cfg.specialists:
                wg_name = name
                break

        # Stop process
        cli = self.procs.pop(sp_name, None)
        if cli:
            try:
                await cli.stop()
            except Exception as e:
                logger.warning("Error stopping specialist CLI '%s': %s", sp_name, e)

        # Stop pool
        pool = self.pools.pop(sp_name, None)
        if pool:
            try:
                await pool.stop()
            except Exception as e:
                logger.warning("Error stopping specialist pool '%s': %s", sp_name, e)

        # Remove router
        self.routers.pop(sp_name, None)

        # Remove from config and saved file
        if wg_name:
            wg_cfg = self.config[wg_name]
            wg_cfg.specialists.pop(sp_name, None)
            self._remove_saved_specialist(wg_name, sp_name)

            # Update admin's specialist list
            admin_router = self.routers.get(wg_name)
            if admin_router:
                admin_router.workgroup_agents = list(wg_cfg.specialists.keys())

        logger.info("Deleted specialist '%s' from workgroup '%s'", sp_name, wg_name)
        return {"ok": True}

    def _remove_saved_specialist(self, wg_name: str, sp_name: str) -> None:
        """Remove a specialist from the saved workgroup_specialists.yaml."""
        path = self._specialists_file()
        if not path.is_file():
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            wg_data = data.get(wg_name, {})
            if sp_name in wg_data:
                del wg_data[sp_name]
                with open(path, "w", encoding="utf-8") as f:
                    yaml.dump(data, f, default_flow_style=False)
        except Exception as e:
            logger.warning("Failed to remove saved specialist '%s': %s", sp_name, e)

    async def stop(self) -> None:
        """Stop all workgroup processes, pools, and heartbeats."""
        for name, hb in self._heartbeats.items():
            hb.stop()
        self._heartbeats.clear()
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
