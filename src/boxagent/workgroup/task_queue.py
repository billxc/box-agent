"""SpecialistTaskQueue — owns the {task_id → state, task_id → asyncio.Task}
maps for workgroup specialist dispatches.

Extracted from WorkgroupManager so the manager can focus on lifecycle
orchestration; this dataclass owns the dispatch ledger and exposes the
narrow set of operations callers actually need.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class SpecialistTaskQueue:
    _tasks: dict[str, asyncio.Task] = field(default_factory=dict, repr=False)
    _results: dict[str, dict] = field(default_factory=dict, repr=False)
    _counter: int = 0

    def alloc_id(self, target: str) -> str:
        self._counter += 1
        return f"{target}-{self._counter}"

    def start(self, task_id: str, target: str) -> None:
        self._results[task_id] = {
            "status": "running",
            "target": target,
            "started_at": time.time(),
        }

    def finish(self, task_id: str, result: str) -> None:
        self._results[task_id] = {
            "status": "done",
            "result": result,
            "finished_at": time.time(),
        }

    def fail(self, task_id: str, error: str) -> None:
        self._results[task_id] = {
            "status": "error",
            "error": error,
            "finished_at": time.time(),
        }

    def register(self, task_id: str, async_task: asyncio.Task) -> None:
        self._tasks[task_id] = async_task

    def get(self, task_id: str) -> dict | None:
        return self._results.get(task_id)

    def all_for_target(self, target: str) -> list[tuple[str, dict]]:
        return [(tid, info) for tid, info in self._results.items() if info.get("target") == target]

    def running_targets(self) -> list[tuple[str, dict]]:
        """Return (task_id, info) pairs for tasks currently in 'running' status."""
        return [(tid, info) for tid, info in self._results.items() if info.get("status") == "running"]

    async def cancel(
        self,
        task_id: str,
        cancel_specialist: Callable[[str], None] | None = None,
    ) -> dict:
        """Cancel a running task; ``cancel_specialist(target)`` is invoked
        before the asyncio task is cancelled so the underlying CLI process
        gets a chance to terminate cleanly."""
        info = self._results.get(task_id)
        if info is None:
            return {"ok": False, "error": f"task '{task_id}' not found"}
        if info.get("status") != "running":
            return {"ok": False, "error": f"task '{task_id}' is not running (status={info.get('status')})"}

        target = info.get("target", "")
        if cancel_specialist is not None:
            try:
                result = cancel_specialist(target)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning("cancel_specialist(%s) raised: %s", target, e)

        async_task = self._tasks.get(task_id)
        if async_task and not async_task.done():
            async_task.cancel()

        self._results[task_id] = {
            "status": "cancelled",
            "target": target,
            "finished_at": time.time(),
        }
        logger.info("Task %s cancelled (specialist=%s)", task_id, target)
        return {"ok": True, "task_id": task_id, "specialist": target}
