"""Workgroup HTTP handlers — thin adapters around WorkgroupManager.

Composition class. Owned by ``WorkgroupManager`` itself (built in
``__post_init__`` so the manager and its routes ship together). Gateway
wiring just reads ``workgroup_manager.routes``; the wiring stays inside the
workgroup module.

Single-phase DI: needs the ``WorkgroupManager`` instance — bind that and
all 7 handlers can dispatch.
"""

import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from boxagent.workgroup.manager import WorkgroupManager

logger = logging.getLogger(__name__)


class WorkgroupHttpRoutes:
    def __init__(self, *, workgroup_manager: "WorkgroupManager") -> None:
        self.workgroup_manager = workgroup_manager

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
            result = await self.workgroup_manager.send_to_specialist(
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

        result = await self.workgroup_manager.create_specialist(
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

        result = self.workgroup_manager.reset_specialist(target)
        status = 200 if result.get("ok") else 400
        return web.json_response(result, status=status)

    async def handle_list_specialists(self, request: web.Request) -> web.Response:
        """GET /api/workgroup/specialists — list all specialists with details."""
        workgroup_name = request.query.get("workgroup", "")
        result = self.workgroup_manager.list_specialists(workgroup_name)
        return web.json_response(result)

    async def handle_specialist_status(self, request: web.Request) -> web.Response:
        """GET /api/workgroup/specialist_status — status + recent chat."""
        name = request.query.get("name", "")
        if not name:
            return web.json_response({"ok": False, "error": "missing 'name'"}, status=400)
        result = self.workgroup_manager.get_specialist_status(name)
        return web.json_response(result)

    async def handle_delete_specialist(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        target = body.get("name", "")
        if not target:
            return web.json_response({"ok": False, "error": "missing 'name'"}, status=400)

        result = await self.workgroup_manager.delete_specialist(target)
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

        result = await self.workgroup_manager.cancel_task(task_id)
        status = 200 if result.get("ok") else 400
        return web.json_response(result, status=status)
