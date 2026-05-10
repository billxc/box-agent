"""Scheduler HTTP routes — POST /api/schedule/run.

Composition class. Held by the scheduler subsystem (not Gateway). Built
right after the ``Scheduler`` instance, then handed to ``InternalApiServer``
for route registration.

Single-phase DI: needs config (for ``node_id``), config_dir (for
``schedules.yaml`` lookup), and the live scheduler.
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

logger = logging.getLogger(__name__)


class SchedulerHttpRoutes:
    def __init__(
        self,
        *,
        config: "AppConfig",
        config_dir: Path,
        scheduler: "Scheduler",
    ) -> None:
        self.config = config
        self.config_dir = config_dir
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
            asyncio.ensure_future(self._run_bg(task_id, task))
            return web.json_response({"ok": True, "status": "scheduled"})

        try:
            output = await self.scheduler.execute_once(task)
            return web.json_response({"ok": True, "output": output})
        except Exception as e:
            logger.error("API schedule/run '%s' failed: %s", task_id, e)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _run_bg(self, task_id: str, task) -> None:
        try:
            await self.scheduler.execute_once(task)
            logger.info("Async schedule/run '%s' completed", task_id)
        except Exception as e:
            logger.error("Async schedule/run '%s' failed: %s", task_id, e)
