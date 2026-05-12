"""Periodic retention sweeper for the event store.

Local retention: 30 days. Older events are deleted to bound disk growth.
Sync window (3 days) is enforced by the syncer separately — this is purely
a local cleanup job.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .storage import EventStore

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_SECONDS = 30 * 86400
DEFAULT_INTERVAL_SECONDS = 3600  # sweep every hour


class RetentionSweeper:
    def __init__(
        self,
        store: EventStore,
        *,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._store = store
        self._retention = retention_seconds
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = False

    def sweep_once(self) -> int:
        cutoff = time.time() - self._retention
        deleted = self._store.delete_older_than(cutoff)
        if deleted:
            logger.info("retention: deleted %d events older than %ds", deleted, int(self._retention))
        return deleted

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop = False
        self._task = asyncio.create_task(self._loop(), name="event-retention")

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop:
            try:
                self.sweep_once()
            except Exception:
                logger.exception("retention sweep failed")
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break
