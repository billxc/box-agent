"""WorkgroupManager — manages workgroup lifecycle, specialists, and delegation."""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable


from boxagent.config import BotConfig, SpecialistConfig, WorkgroupConfig
from boxagent.agent.backend_factory import create_backend
from boxagent.agent.protocol import AgentBackend
from boxagent.agent.workspace import ensure_git_repo, sync_skills
from boxagent.transports.web import WebChannel
from boxagent.utils import resolve_boxagent_dir
from boxagent.workgroup.channel_adapter import (
    NullWorkgroupChannelAdapter,
    WebWorkgroupAdapter,
    WorkgroupChannelAdapter,
)
from boxagent.workgroup.heartbeat import HeartbeatManager
from boxagent.workgroup.persistence import (
    load_saved_specialists,
    remove_saved_specialist,
    save_specialist,
)
from boxagent.workgroup.specialist_skills import apply_template_skills
from boxagent.workgroup.task_queue import SpecialistTaskQueue
from boxagent.router import Router
from boxagent.sessions import SessionPool
from boxagent.workgroup.template_loader import (
    TemplateInfo,
    discover_templates,
    get_template,
)
from boxagent.workgroup.workspace_templates import (
    read_template_snapshot,
    seed_admin_workspace,
    seed_specialist_workspace,
    write_template_snapshot,
)

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from boxagent.sessions import Storage
    from boxagent.workgroup.workgroup_http_routes import WorkgroupHttpRoutes


# Builtin template root, shipped with the codebase. Empty for v1; users add
# templates under {workgroup_dir}/templates/.
BUILTIN_TEMPLATES_DIR = Path(__file__).parent / "templates" / "builtin_templates"


def _workgroup_templates_dir(workgroup_config: WorkgroupConfig) -> Path:
    return Path(workgroup_config.workgroup_dir) / "templates" if workgroup_config.workgroup_dir else Path()


def format_running_tasks(running_tasks: list[dict] | None) -> str:
    """Format running tasks into a display block. Used by context and heartbeat."""
    if not running_tasks:
        return "No specialist tasks currently running."
    lines = ["Currently running specialist tasks:"]
    for t in running_tasks:
        elapsed = ""
        started = t.get("started_at", 0)
        if started:
            secs = int(time.time() - started)
            mins, s = divmod(secs, 60)
            elapsed = f" (running {mins}m {s}s)"
        active = " [active]" if t.get("active") else " [queued]"
        lines.append(f"  - {t.get('task_id', '?')}: {t.get('target', '?')}{elapsed}{active}")
    return "\n".join(lines)


def _extract_specialist_response(text: str) -> str:
    """Extract content from <specialist_response> tags. Falls back to raw text."""
    m = re.search(r"<specialist_response>(.*?)</specialist_response>", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


@dataclass
class WorkgroupManager:
    """Manages workgroup admin + specialist agents.

    Created by Gateway, holds all workgroup-specific state.
    """

    config: dict[str, WorkgroupConfig]  # workgroup_name → config
    config_dir: str = ""
    node_id: str = ""
    local_dir: Path | None = None
    start_time: float = 0.0
    storage: "Storage | None" = None
    web_channels: dict[str, WebChannel] = field(default_factory=dict)      # name → WebChannel (shared with Gateway)
    # Internal state
    routers: dict[str, Router] = field(default_factory=dict)    # name → Router
    pools: dict[str, SessionPool] = field(default_factory=dict)  # name → Pool
    procs: dict[str, AgentBackend] = field(default_factory=dict)       # name → CLI process
    adapters: dict[str, WorkgroupChannelAdapter] = field(default_factory=dict)  # workgroup_name → adapter
    # Async task tracking — delegated to a dedicated queue.
    tasks: SpecialistTaskQueue = field(default_factory=SpecialistTaskQueue, repr=False)
    _heartbeats: dict[str, HeartbeatManager] = field(default_factory=dict, repr=False)

    # Injected by Gateway
    _peer_provider: Callable[[str], list[dict]] | None = None  # exclude=self_name

    # HTTP route adapter — built lazily on first access so the manager and
    # its HTTP surface ship together. Gateway just reads ``mgr.routes``.
    _routes: "WorkgroupHttpRoutes | None" = field(default=None, init=False, repr=False)

    @property
    def routes(self) -> "WorkgroupHttpRoutes":
        from boxagent.workgroup.workgroup_http_routes import WorkgroupHttpRoutes
        if self._routes is None:
            self._routes = WorkgroupHttpRoutes(workgroup_mgr=self)
        return self._routes

    def _require_local_dir(self) -> Path:
        if self.local_dir is None:
            raise RuntimeError("WorkgroupManager.local_dir not configured")
        return self.local_dir

    def _load_saved_specialists(self, workgroup_name: str) -> dict[str, SpecialistConfig]:
        return load_saved_specialists(self._require_local_dir(), workgroup_name)

    def _save_specialist(self, workgroup_name: str, specialist: SpecialistConfig) -> None:
        save_specialist(self._require_local_dir(), workgroup_name, specialist)

    def _make_backend(self, bot_cfg: BotConfig, session_id=None):
        return create_backend(bot_cfg, session_id)

    def _apply_template_skills(
        self,
        workspace: str,
        template_info: TemplateInfo,
        ai_backend: str,
    ) -> None:
        apply_template_skills(workspace, template_info, ai_backend)

    async def _create_specialist_agent(
        self, specialist_name: str, specialist_config, workgroup_config: WorkgroupConfig,
        adapter: WorkgroupChannelAdapter,
        template_info: TemplateInfo | None = None,
    ) -> Router:
        """Create backend, pool, router for a single specialist. Returns the Router."""
        bot_config = BotConfig(
            name=specialist_name,
            ai_backend=specialist_config.ai_backend,
            workspace=specialist_config.workspace,
            model=specialist_config.model,
            yolo=workgroup_config.yolo,
            extra_skill_dirs=specialist_config.extra_skill_dirs,
            display_name=specialist_config.display_name,
        )

        # Prepare workspace BEFORE starting backend
        if bot_config.workspace:
            ensure_git_repo(Path(bot_config.workspace))
        # User-provided extra_skill_dirs (not subject to template filters).
        if bot_config.extra_skill_dirs:
            sync_skills(bot_config.workspace, bot_config.extra_skill_dirs, bot_config.ai_backend)
        # Template-provided skills (inline + filtered external).
        if template_info is not None:
            self._apply_template_skills(
                bot_config.workspace, template_info, bot_config.ai_backend
            )
            # Snapshot template CLAUDE.md so future restarts replay the same
            # template content even if the source is later modified.
            write_template_snapshot(bot_config.workspace, template_info.read_claude_md())
        seed_specialist_workspace(
            bot_config.workspace, specialist_name, workgroup_config.name,
            template_claude_md_text=read_template_snapshot(bot_config.workspace),
        )

        backend = self._make_backend(bot_config)
        backend.start()
        self.procs[specialist_name] = backend

        def _factory(cfg=bot_config):
            return self._make_backend(cfg)

        pool = SessionPool(
            size=1,
            default_model=bot_config.model,
            default_workspace=bot_config.workspace,
            storage=self.storage,
            bot_name=specialist_name,
        )
        pool.start(_factory)
        self.pools[specialist_name] = pool

        specialist_router = Router(
            backend=backend,
            channel=adapter.primary_channel(),
            allowed_users=workgroup_config.allowed_users,
            storage=self.storage,
            pool=pool,
            bot_name=specialist_name,
            display_name=bot_config.display_name,
            config_dir=self.config_dir,
            node_id=self.node_id,
            local_dir=self.local_dir,
            start_time=self.start_time,
            workspace=bot_config.workspace,
            extra_skill_dirs=bot_config.extra_skill_dirs,
            ai_backend=bot_config.ai_backend,
        )
        # Adapter wires any inbound channel affordances on the specialist.
        await adapter.setup_specialist(specialist_name, specialist_config, workgroup_config, specialist_router)
        self.routers[specialist_name] = specialist_router
        return specialist_router

    def _build_adapter(self, workgroup_config: WorkgroupConfig) -> WorkgroupChannelAdapter:
        """Pick the workgroup's internal message adapter.

        Web is the only workgroup substrate. Falls back to
        NullWorkgroupChannelAdapter if no WebChannel exists yet.
        """
        web_channel = self.web_channels.get(workgroup_config.name)
        if web_channel is not None:
            return WebWorkgroupAdapter(web_channel=web_channel)
        return NullWorkgroupChannelAdapter()

    async def start_all_for_node(self, node_id: str) -> None:
        """Start every workgroup whose ``enabled_on_nodes`` matches ``node_id``."""
        from boxagent.config import node_matches
        for workgroup_name, workgroup_config in self.config.items():
            if not node_matches(workgroup_config.enabled_on_nodes, node_id):
                logger.info(
                    "Workgroup '%s' skipped (enabled_on_nodes=%s, current=%s)",
                    workgroup_name, workgroup_config.enabled_on_nodes, node_id,
                )
                continue
            await self.start_workgroup(workgroup_name, workgroup_config)

    async def start_workgroup(self, workgroup_name: str, workgroup_config: WorkgroupConfig) -> None:
        """Initialize a standalone workgroup: create admin + specialist agents."""
        # Web is the workgroup's substrate — always create the WebChannel
        # (even if web_enabled was set false in yaml; workgroup needs it).
        if workgroup_name not in self.web_channels:
            from boxagent.transports.web import WebChannel
            self.web_channels[workgroup_name] = WebChannel(bot_name=workgroup_name)

        adapter = self._build_adapter(workgroup_config)
        self.adapters[workgroup_name] = adapter

        # --- Create admin agent ---
        admin_ws = workgroup_config.admin_workspace
        admin_bot_cfg = BotConfig(
            name=workgroup_name,
            ai_backend=workgroup_config.ai_backend,
            workspace=admin_ws,
            model=workgroup_config.model,
            yolo=workgroup_config.yolo,
            allowed_users=workgroup_config.allowed_users,
            display_name=workgroup_config.display_name,
            display_tool_calls=workgroup_config.display_tool_calls,
            extra_skill_dirs=workgroup_config.extra_skill_dirs,
        )

        # Load saved dynamic specialists BEFORE seeding workspace so
        # CLAUDE.md lists all specialists.
        saved = self._load_saved_specialists(workgroup_name)
        for specialist_name, specialist_config in saved.items():
            workgroup_config.specialists[specialist_name] = specialist_config
            logger.info("Workgroup '%s': restored saved specialist '%s'", workgroup_name, specialist_name)

        # Prepare admin workspace BEFORE starting backend
        if admin_ws:
            ensure_git_repo(Path(admin_ws))
        if workgroup_config.extra_skill_dirs:
            sync_skills(admin_ws, workgroup_config.extra_skill_dirs, workgroup_config.ai_backend)
        seed_admin_workspace(admin_ws, workgroup_name)

        admin_backend = self._make_backend(admin_bot_cfg)
        admin_backend.start()
        self.procs[workgroup_name] = admin_backend

        def _admin_factory(cfg=admin_bot_cfg):
            proc = self._make_backend(cfg)
            return proc

        admin_pool = SessionPool(
            size=3,
            default_model=workgroup_config.model,
            default_workspace=admin_ws,
            storage=self.storage,
            bot_name=workgroup_name,
        )
        admin_pool.start(_admin_factory)
        self.pools[workgroup_name] = admin_pool

        admin_router = Router(
            backend=admin_backend,
            channel=adapter.primary_channel(),
            allowed_users=workgroup_config.allowed_users,
            storage=self.storage,
            pool=admin_pool,
            bot_name=workgroup_name,
            display_name=workgroup_config.display_name,
            config_dir=self.config_dir,
            node_id=self.node_id,
            local_dir=self.local_dir,
            start_time=self.start_time,
            workspace=admin_ws,
            extra_skill_dirs=workgroup_config.extra_skill_dirs,
            ai_backend=workgroup_config.ai_backend,
            get_running_tasks=lambda workgroup=workgroup_name: self._get_running_tasks(workgroup),
            get_peers=lambda workgroup=workgroup_name: (
                self._peer_provider(workgroup) if callable(self._peer_provider) else []
            ),
            workgroup_role="admin",
            # Workgroup admins always have peer messaging capability via cluster
            # RPC (no per-bot toggle needed — they're always cluster citizens).
            has_peer_channel=True,
        )
        self.routers[workgroup_name] = admin_router

        # --- Web channel inbound wiring (the workgroup substrate). ---
        web_channel = self.web_channels[workgroup_name]
        web_channel.on_message = admin_router.handle_message
        admin_router._channels["web"] = web_channel
        logger.info("Workgroup '%s': web channel enabled", workgroup_name)

        # Cross-admin peer messaging routes via cluster RPC in send_peer.

        # --- Create specialists (already merged above) ---
        specialist_names = []
        for specialist_name, specialist_config in workgroup_config.specialists.items():
            await self._create_specialist_agent(specialist_name, specialist_config, workgroup_config, adapter)
            specialist_names.append(specialist_name)
            logger.info(
                "Workgroup '%s': specialist '%s' started (model=%s)",
                workgroup_name, specialist_name, specialist_config.model,
            )

        admin_router.workgroup_agents = specialist_names
        logger.info("Workgroup '%s' ready: specialists=%s", workgroup_name, specialist_names)

        # --- Start heartbeat (if configured) ---
        if workgroup_config.heartbeat_interval_seconds > 0:
            # Heartbeat is admin context refresh. It forks the admin's main
            # chat (via main_chat_id_provider) and dispatches actionable
            # decisions back to that same chat. Display goes to web only.
            storage = self.storage
            wg_name = workgroup_name

            # Heartbeat needs admin's main chat_id so it forks the same session
            # heartbeat / peer messages dispatch into. Reuse the standard
            # Storage helper instead of inlining the get-or-mint pattern.
            def _main_chat_id_provider(_storage=storage, _name=wg_name):
                if _storage is None:
                    return f"main-{_name}-{int(time.time())}"
                return _storage.get_or_create_main_chat_id(_name)

            heartbeat = HeartbeatManager(
                workgroup_name=workgroup_name,
                admin_pool=admin_pool,
                admin_router=admin_router,
                workspace=admin_ws,
                interval_seconds=workgroup_config.heartbeat_interval_seconds,
                ai_backend=workgroup_config.ai_backend,
                model=workgroup_config.model,
                yolo=workgroup_config.yolo,
                web_channel=self.web_channels.get(workgroup_name),
                display_heartbeat=workgroup_config.display_heartbeat,
                start_time=self.start_time,
                get_running_tasks=lambda workgroup=workgroup_name: self._get_running_tasks(workgroup),
                main_chat_id_provider=_main_chat_id_provider,
            )
            heartbeat.start()
            self._heartbeats[workgroup_name] = heartbeat

    async def send_to_specialist(
        self, target: str, text: str, from_bot: str = "",
        reply_chat_id: str = "",
    ) -> dict:
        """Dispatch a task to a specialist asynchronously.

        Returns immediately with a task_id. The specialist processes in the
        background; results are visible in the specialist's web view.
        When done, a summary is posted back to reply_chat_id (admin's channel).
        """
        router = self.routers.get(target)
        if router is None:
            return {"ok": False, "error": f"specialist '{target}' not found"}

        task_id = self.tasks.alloc_id(target)

        # Resolve the workgroup that owns this specialist + its adapter
        adapter: WorkgroupChannelAdapter = NullWorkgroupChannelAdapter()
        specialist_config = None
        workgroup_display = from_bot or "admin"
        workgroup_name = ""
        for name, workgroup_config in self.config.items():
            if target in workgroup_config.specialists:
                specialist_config = workgroup_config.specialists[target]
                adapter = self.adapters.get(name) or NullWorkgroupChannelAdapter()
                workgroup_display = workgroup_config.display_name or from_bot or "admin"
                workgroup_name = name
                break

        chat_id = adapter.get_specialist_chat_id(target, specialist_config) if specialist_config else f"wg:{target}"

        # Wrap admin's message with system instruction for XML-tagged response
        wrapped_text = (
            f"{text}\n\n"
            "---\n"
            "[SYSTEM] When you are done, wrap your final response/summary in "
            "<specialist_response> tags. Example:\n"
            "<specialist_response>\n"
            "Summary of what was done, results, and any issues.\n"
            "</specialist_response>\n"
            "You MUST include <specialist_response> tags in your reply."
        )

        async def _run():
            result = ""
            try:
                # Post task in specialist's visibility channel (e.g. web)
                if specialist_config is not None:
                    await adapter.post_task(target, specialist_config, text, workgroup_display)

                raw_result = await router.dispatch_sync(wrapped_text, chat_id, from_bot=from_bot)
                result = _extract_specialist_response(raw_result)
                self.tasks.finish(task_id, result)
                logger.info("Task %s completed (%d chars)", task_id, len(result))
            except Exception as e:
                self.tasks.fail(task_id, str(e))
                logger.error("Task %s failed: %s", task_id, e)
                result = f"Error: {e}"

            # Callback: short notification to admin's chat + full result to admin router
            if reply_chat_id:
                # 1. Result notification (web: text post; null: no-op)
                status = "done" if "Error" not in result[:10] else "failed"
                preview = result[:800] + "..." if len(result) > 800 else result
                notify = f"**[{target}]** {status}\n{preview}"
                await adapter.notify_admin(reply_chat_id, notify)

                # 2. Full result to admin router (internal, admin AI processes it)
                admin_router = self.routers.get(workgroup_name)
                if admin_router:
                    from boxagent.transports.base import IncomingMessage
                    callback_msg = IncomingMessage(
                        channel="internal",
                        chat_id=reply_chat_id,
                        user_id="workgroup",
                        text=f"[TaskResult from {target}]\n{result}",
                        trusted=True,
                        via_workgroup=True,
                    )
                    try:
                        await admin_router.handle_message(callback_msg)
                    except Exception as e:
                        logger.warning("Failed to deliver task result to admin: %s", e)

        self.tasks.start(task_id, target)
        self.tasks.register(task_id, asyncio.create_task(_run()))

        return {"ok": True, "task_id": task_id, "specialist": target}

    def list_specialists(self, workgroup_name: str = "") -> dict:
        """List all specialists with their details.

        If *workgroup_name* is given, list only specialists for that workgroup.
        Otherwise list across all workgroups.
        """
        specialists = []
        for name, workgroup_config in self.config.items():
            if workgroup_name and name != workgroup_name:
                continue
            for specialist_name, specialist_config in workgroup_config.specialists.items():
                # Check running tasks
                running_tasks = [
                    tid for tid, info in self.tasks.running_targets()
                    if tid.startswith(f"{specialist_name}-")
                ]
                specialists.append({
                    "name": specialist_name,
                    "workgroup": name,
                    "model": specialist_config.model,
                    "workspace": specialist_config.workspace,
                    "ai_backend": specialist_config.ai_backend,
                    "display_name": specialist_config.display_name,
                    "template": specialist_config.template,
                    "running_tasks": running_tasks,
                })
        return {"ok": True, "specialists": specialists}

    def list_templates(self, workgroup_name: str) -> dict:
        """List available templates (builtin + workgroup) for a workgroup."""
        workgroup_config = self.config.get(workgroup_name)
        if workgroup_config is None:
            return {"ok": False, "error": f"workgroup '{workgroup_name}' not found"}
        try:
            templates = discover_templates(
                _workgroup_templates_dir(workgroup_config),
                BUILTIN_TEMPLATES_DIR,
                resolve_boxagent_dir(),
            )
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        items = [
            {"name": name, "description": info.description}
            for name, info in sorted(templates.items())
        ]
        return {"ok": True, "templates": items}

    def get_task_result(self, task_id: str) -> dict:
        """Check the status/result of an async task."""
        info = self.tasks.get(task_id)
        if info is None:
            return {"ok": False, "error": f"task '{task_id}' not found"}
        return {"ok": True, **info}

    def get_specialist_status(self, target: str, max_lines: int = 20) -> dict:
        """Get specialist's running status and recent chat history."""
        if target not in self.routers:
            return {"ok": False, "error": f"specialist '{target}' not found"}

        pool = self.pools.get(target)

        # Running state
        active = False
        if pool:
            for proc in pool._active.values():
                if getattr(proc, "state", "idle") == "busy":
                    active = True
                    break

        # Recent tasks
        tasks = []
        for tid, info in self.tasks.all_for_target(target):
            entry = {"task_id": tid, "status": info.get("status", "?")}
            if info.get("started_at"):
                entry["started_at"] = info["started_at"]
            if info.get("finished_at"):
                entry["finished_at"] = info["finished_at"]
            if info.get("result"):
                entry["result_preview"] = info["result"][:300]
            if info.get("error"):
                entry["error"] = info["error"]
            tasks.append(entry)

        # Recent transcript from session
        transcript_lines = []
        if pool and self.local_dir:
            # Find session_id for this specialist
            for workgroup_config in self.config.values():
                if target in workgroup_config.specialists:
                    chat_id = f"wg:{target}"
                    sid = pool.get_session_id(chat_id)
                    if sid:
                        transcript_path = self.local_dir / "transcripts" / f"{sid}.jsonl"
                        if transcript_path.is_file():
                            try:
                                import json
                                lines = transcript_path.read_text(encoding="utf-8").strip().split("\n")
                                for line in lines[-max_lines:]:
                                    record = json.loads(line)
                                    event = record.get("event", "")
                                    text = record.get("text", "")
                                    preview = text[:200] + "..." if len(text) > 200 else text
                                    transcript_lines.append(f"[{event}] {preview}")
                            except Exception:
                                pass
                    break

        return {
            "ok": True,
            "specialist": target,
            "active": active,
            "tasks": tasks,
            "recent_chat": transcript_lines,
        }

    async def cancel_task(self, task_id: str) -> dict:
        """Cancel a running specialist task."""
        async def _cancel_specialist(target: str) -> None:
            pool = self.pools.get(target)
            if pool:
                for proc in pool._active.values():
                    await proc.cancel()

        return await self.tasks.cancel(task_id, cancel_specialist=_cancel_specialist)

    def _get_running_tasks(self, workgroup_name: str) -> list[dict]:
        """Return currently running tasks for a workgroup."""
        result = []
        workgroup_config = self.config.get(workgroup_name)
        if workgroup_config is None:
            return result
        for tid, info in self.tasks.running_targets():
            target = info.get("target", "")
            if target not in workgroup_config.specialists:
                continue
            # Check if the specialist's process is actively busy
            pool = self.pools.get(target)
            active = False
            if pool:
                for proc in pool._active.values():
                    if getattr(proc, "state", "idle") == "busy":
                        active = True
                        break
            result.append({
                "task_id": tid,
                "target": target,
                "started_at": info.get("started_at", 0),
                "active": active,
            })
        return result

    def reset_specialist(self, target: str) -> dict:
        """Clear a specialist's session so the next task starts fresh."""
        pool = self.pools.get(target)
        if pool is None:
            return {"ok": False, "error": f"specialist '{target}' not found"}
        # chat_id used by send_to_specialist
        for workgroup_config in self.config.values():
            if target in workgroup_config.specialists:
                chat_id = f"wg:{target}"
                pool.clear_session(chat_id)
                logger.info("Reset session for specialist '%s' (chat_id=%s)", target, chat_id)
                return {"ok": True}
        return {"ok": False, "error": f"specialist '{target}' not in any workgroup"}

    async def create_specialist(
        self, workgroup_name: str, specialist_name: str,
        model: str = "", workspace: str = "",
        template: str = "",
        extra_skill_dirs: list[str] | None = None,
        display_name: str = "",
    ) -> dict:
        """Dynamically create a specialist agent in a workgroup."""
        workgroup_config = self.config.get(workgroup_name)
        if workgroup_config is None:
            return {"ok": False, "error": f"workgroup '{workgroup_name}' not found"}

        if specialist_name in self.routers:
            return {"ok": False, "error": f"specialist '{specialist_name}' already exists"}

        # Resolve template (fail loud if requested but not found).
        template_info: TemplateInfo | None = None
        if template:
            try:
                template_info = get_template(
                    template,
                    _workgroup_templates_dir(workgroup_config),
                    BUILTIN_TEMPLATES_DIR,
                    resolve_boxagent_dir(),
                )
            except ValueError as e:
                return {"ok": False, "error": str(e)}

        # Resolve user-provided extra_skill_dirs against the boxagent dir.
        ba_dir = resolve_boxagent_dir()
        resolved_user_dirs: list[str] = []
        for raw in (extra_skill_dirs or []):
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = ba_dir / p
            resolved_user_dirs.append(str(p))

        # Reuse the workgroup's adapter (built at start_workgroup time).
        adapter = self.adapters.get(workgroup_name) or self._build_adapter(workgroup_config)

        specialist_config = SpecialistConfig(
            name=specialist_name,
            model=model or workgroup_config.model,
            workspace=workspace or workgroup_config.specialist_workspace(specialist_name),
            ai_backend=workgroup_config.ai_backend,
            display_name=display_name or specialist_name,
            extra_skill_dirs=resolved_user_dirs,
            template=template,
        )

        # Adapter hook (web is currently a no-op; reserved for future transports).
        specialist_config = await adapter.provision_specialist(specialist_name, specialist_config, workgroup_config)

        await self._create_specialist_agent(
            specialist_name, specialist_config, workgroup_config, adapter, template_info=template_info,
        )

        # Persist
        workgroup_config.specialists[specialist_name] = specialist_config
        self._save_specialist(workgroup_name, specialist_config)

        # Update admin's agent list
        admin_router = self.routers.get(workgroup_name)
        if admin_router:
            admin_router.workgroup_agents = list(workgroup_config.specialists.keys())

        chat_id = adapter.get_specialist_chat_id(specialist_name, specialist_config)
        logger.info(
            "Workgroup '%s': dynamically created specialist '%s' (chat_id=%s, model=%s, workspace=%s)",
            workgroup_name, specialist_name, chat_id, specialist_config.model, specialist_config.workspace,
        )
        return {"ok": True, "chat_id": chat_id}

    async def delete_specialist(self, specialist_name: str) -> dict:
        """Delete a specialist.

        Stops its process and pool, removes its workspace directory,
        and removes the persisted entry.
        """
        if specialist_name not in self.routers:
            return {"ok": False, "error": f"specialist '{specialist_name}' not found"}

        # Find which workgroup owns this specialist
        workgroup_name = ""
        specialist_config = None
        specialist_workspace = ""
        for name, workgroup_config in self.config.items():
            if specialist_name in workgroup_config.specialists:
                workgroup_name = name
                specialist_config = workgroup_config.specialists[specialist_name]
                specialist_workspace = specialist_config.workspace
                break

        # Adapter teardown hook (web is currently a no-op).
        if specialist_config is not None and workgroup_name:
            adapter = self.adapters.get(workgroup_name) or NullWorkgroupChannelAdapter()
            await adapter.cleanup_specialist(specialist_name, specialist_config)

        # Stop process
        backend = self.procs.pop(specialist_name, None)
        if backend:
            try:
                await backend.stop()
            except Exception as e:
                logger.warning("Error stopping specialist CLI '%s': %s", specialist_name, e)

        # Stop pool
        pool = self.pools.pop(specialist_name, None)
        if pool:
            try:
                await pool.stop()
            except Exception as e:
                logger.warning("Error stopping specialist pool '%s': %s", specialist_name, e)

        # Remove router
        self.routers.pop(specialist_name, None)

        # Remove from config and saved file
        if workgroup_name:
            workgroup_config = self.config[workgroup_name]
            workgroup_config.specialists.pop(specialist_name, None)
            self._remove_saved_specialist(workgroup_name, specialist_name)

            # Update admin's specialist list
            admin_router = self.routers.get(workgroup_name)
            if admin_router:
                admin_router.workgroup_agents = list(workgroup_config.specialists.keys())

        # Remove workspace directory (after process stop, after persistence cleanup).
        if specialist_workspace:
            ws_path = Path(specialist_workspace)
            if ws_path.is_dir():
                import shutil
                try:
                    shutil.rmtree(ws_path)
                    logger.info("Removed specialist workspace: %s", ws_path)
                except Exception as e:
                    logger.warning(
                        "Failed to remove specialist workspace '%s': %s", ws_path, e
                    )

        logger.info("Deleted specialist '%s' from workgroup '%s'", specialist_name, workgroup_name)
        return {"ok": True}

    def _remove_saved_specialist(self, workgroup_name: str, specialist_name: str) -> None:
        remove_saved_specialist(self._require_local_dir(), workgroup_name, specialist_name)

    async def stop(self) -> None:
        """Stop all workgroup processes, pools, and heartbeats."""
        for name, heartbeat in self._heartbeats.items():
            heartbeat.stop()
        self._heartbeats.clear()
        for name, backend in self.procs.items():
            try:
                await backend.stop()
            except Exception as e:
                logger.error("Error stopping workgroup CLI %s: %s", name, e)
        for name, pool in self.pools.items():
            try:
                await pool.stop()
            except Exception as e:
                logger.error("Error stopping workgroup pool %s: %s", name, e)
