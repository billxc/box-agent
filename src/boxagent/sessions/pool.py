"""SessionPool — fixed-size queue of pre-spawned backends shared across chats."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable

from boxagent.sessions.base_pool import BaseSessionPool

if TYPE_CHECKING:
    from boxagent.agent.protocol import AgentBackend

logger = logging.getLogger(__name__)

DEFAULT_POOL_SIZE = 3


class SessionPool(BaseSessionPool):
    """Pool of pre-spawned backends shared across chats.

    All chats served by this pool share a single backend kind (set by the
    ``factory`` passed to :meth:`start`). Up to ``size`` chats can be
    running a turn concurrently; further acquires block until one
    releases.

    Per-chat session_id / model / workspace are restored onto the
    borrowed backend on each acquire — see ``BaseSessionPool``.
    """

    def __init__(
        self,
        *,
        size: int = DEFAULT_POOL_SIZE,
        default_model: str = "",
        default_workspace: str = "",
        storage: object | None = None,
        bot_name: str = "",
    ) -> None:
        super().__init__(
            storage=storage,
            bot_name=bot_name,
            default_model=default_model,
            default_workspace=default_workspace,
        )
        self.size = size
        self._factory: Callable[[], AgentBackend] | None = None
        self._pool: asyncio.Queue = asyncio.Queue(maxsize=size)
        self._all: list[AgentBackend] = []

    def start(self, factory: Callable[[], AgentBackend]) -> None:
        """Spawn ``size`` backends from ``factory`` and seed the queue."""
        self._factory = factory
        for _ in range(self.size):
            proc = factory()
            proc.start()
            self._all.append(proc)
            self._pool.put_nowait(proc)
        logger.info("SessionPool started with %d processes", self.size)

    # ── BaseSessionPool hooks ──

    async def _borrow(self, chat_id: str) -> AgentBackend:
        return await self._pool.get()

    def _return(self, chat_id: str, proc: AgentBackend) -> None:
        # Clear session_id before returning — next borrower restores its own.
        proc.session_id = None
        self._pool.put_nowait(proc)

    @property
    def all_processes(self) -> list[AgentBackend]:
        return list(self._all)

    async def stop(self) -> None:
        for proc in self._all:
            try:
                await proc.stop()
            except Exception as e:
                logger.warning("Error stopping pool process: %s", e)
        self._all.clear()
        while not self._pool.empty():
            try:
                self._pool.get_nowait()
            except asyncio.QueueEmpty:
                break
        logger.info("SessionPool stopped")

    async def restart_dead(self) -> int:
        if not self._factory:
            return 0
        restarted = 0
        new_all: list[AgentBackend] = []
        for proc in self._all:
            if getattr(proc, "state", "idle") == "dead":
                try:
                    await proc.stop()
                except Exception:
                    pass
                new_proc = self._factory()
                new_proc.start()
                new_all.append(new_proc)
                restarted += 1
                logger.info("Replaced dead pool process")
            else:
                new_all.append(proc)
        if restarted:
            self._all = new_all
            # Rebuild queue with only idle (non-active) processes.
            while not self._pool.empty():
                try:
                    self._pool.get_nowait()
                except asyncio.QueueEmpty:
                    break
            active_set = set(id(p) for p in self._active.values())
            for proc in self._all:
                if id(proc) not in active_set:
                    self._pool.put_nowait(proc)
        return restarted
