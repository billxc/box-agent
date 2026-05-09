"""Workgroup + scheduler HTTP handlers.

Composition class. Held by Gateway as ``self._workgroup_routes``.
Two-phase DI:

- Phase 1 (ctor): config + config_dir (for schedules.yaml lookup).
- Phase 2 (setters): ``set_workgroup_mgr`` / ``set_scheduler`` after
  WorkgroupManager and Scheduler exist.

Public surface: 8 aiohttp handlers registered by HttpApiServer (the
internal API port). All are normal ``async def handle_*(request)``
methods — leading underscores dropped vs the old WorkgroupApiMixin.
"""

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from boxagent.scheduler import load_schedules

if TYPE_CHECKING:
    from boxagent.config import AppConfig
    from boxagent.scheduler import Scheduler
    from boxagent.workgroup import WorkgroupManager

logger = logging.getLogger(__name__)


class WorkgroupHttpRoutes:
    def __init__(self, *, config: "AppConfig", config_dir: Path) -> None:
        self.config = config
        self.config_dir = config_dir
        # Phase 2 deps
        self.workgroup_mgr: "WorkgroupManager | None" = None
        self.scheduler: "Scheduler | None" = None

    def set_workgroup_mgr(self, workgroup_mgr: "WorkgroupManager") -> None:
        self.workgroup_mgr = workgroup_mgr

    def set_scheduler(self, scheduler: "Scheduler") -> None:
        self.scheduler = scheduler

    async def handle_schedule_run(self, request: web.Request) -> web.Response:
        """POST /api/schedule/run — execute a schedule once."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        task_id = body.get("id")
        if not task_id:
            return web.json_response({"ok": False, "error": "missing 'id'"}, status=400)

        schedules_file = self.config_dir / "schedules.yaml"
        all_tasks = load_schedules(schedules_file, node_id=self.config.node_id)
        if task_id not in all_tasks:
            return web.json_response({"ok": False, "error": f"schedule '{task_id}' not found"}, status=404)

        task = all_tasks[task_id]
        run_async = body.get("async", False)

        if run_async:
            asyncio.ensure_future(self._schedule_run_bg(task_id, task))
            return web.json_response({"ok": True, "status": "scheduled"})

        try:
            output = await self.scheduler.execute_once(task)
            return web.json_response({"ok": True, "output": output})
        except Exception as e:
            logger.error("API schedule/run '%s' failed: %s", task_id, e)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _schedule_run_bg(self, task_id: str, task) -> None:
        try:
            await self.scheduler.execute_once(task)
            logger.info("Async schedule/run '%s' completed", task_id)
        except Exception as e:
            logger.error("Async schedule/run '%s' failed: %s", task_id, e)

    async def handle_workgroup_send(self, request: web.Request) -> web.Response:
        """POST /api/workgroup/send — dispatch task to a specialist (async)."""
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
            result = await self.workgroup_mgr.send_to_specialist(
                target, message, from_bot=from_bot, reply_chat_id=reply_chat_id,
            )
            return web.json_response(result)
        except Exception as e:
            logger.error("Workgroup send to '%s' failed: %s", target, e)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def handle_create_specialist(self, request: web.Request) -> web.Response:
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

        result = await self.workgroup_mgr.create_specialist(
            workgroup_name, specialist_name,
            model=body.get("model", ""),
            workspace=body.get("workspace", ""),
        )
        status = 200 if result.get("ok") else 400
        return web.json_response(result, status=status)

    async def handle_reset_specialist(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        target = body.get("name", "")
        if not target:
            return web.json_response({"ok": False, "error": "missing 'name'"}, status=400)

        result = self.workgroup_mgr.reset_specialist(target)
        status = 200 if result.get("ok") else 400
        return web.json_response(result, status=status)

    async def handle_list_specialists(self, request: web.Request) -> web.Response:
        """GET /api/workgroup/specialists — list all specialists with details."""
        workgroup_name = request.query.get("workgroup", "")
        result = self.workgroup_mgr.list_specialists(workgroup_name)
        return web.json_response(result)

    async def handle_specialist_status(self, request: web.Request) -> web.Response:
        """GET /api/workgroup/specialist_status — status + recent chat."""
        name = request.query.get("name", "")
        if not name:
            return web.json_response({"ok": False, "error": "missing 'name'"}, status=400)
        result = self.workgroup_mgr.get_specialist_status(name)
        return web.json_response(result)

    async def handle_delete_specialist(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        target = body.get("name", "")
        if not target:
            return web.json_response({"ok": False, "error": "missing 'name'"}, status=400)

        result = await self.workgroup_mgr.delete_specialist(target)
        status = 200 if result.get("ok") else 400
        return web.json_response(result, status=status)

    async def handle_cancel_task(self, request: web.Request) -> web.Response:
        """POST /api/workgroup/cancel_task — cancel a running specialist task."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        task_id = body.get("task_id", "")
        if not task_id:
            return web.json_response({"ok": False, "error": "missing 'task_id'"}, status=400)

        result = await self.workgroup_mgr.cancel_task(task_id)
        status = 200 if result.get("ok") else 400
        return web.json_response(result, status=status)
