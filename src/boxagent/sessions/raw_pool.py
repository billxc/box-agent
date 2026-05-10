"""RawSessionPool — per-chat lazy spawn for the virtual ``raw`` bot.

Unlike ``SessionPool`` which pre-spawns N backends sharing one kind, the
raw bot can serve any backend kind (claude / codex) per chat. We spawn
one backend per chat_id on first access, keyed by what's stored in
sessions.yaml for that chat.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable

from boxagent.sessions.base_pool import BaseSessionPool

if TYPE_CHECKING:
    from boxagent.agent.protocol import AgentBackend
    from boxagent.sessions.storage import Storage

logger = logging.getLogger(__name__)


class RawSessionPool(BaseSessionPool):
    """Per-chat lazy-spawned backends for the raw passthrough bot.

    ``backend_factory(backend, workspace, model, session_id, bot_name)``
    returns a fresh backend for the requested kind. The pool calls
    ``.start()`` on it before returning from acquire.

    Per-chat acquires are serialised with an ``asyncio.Lock`` so the same
    chat doesn't run two turns concurrently on its single backend.
    """

    def __init__(
        self,
        *,
        storage: "Storage | None" = None,
        bot_name: str = "raw",
        default_model: str = "",
        default_workspace: str = "",
        backend_factory: Callable[..., AgentBackend] | None = None,
    ) -> None:
        super().__init__(
            storage=storage,
            bot_name=bot_name,
            default_model=default_model,
            default_workspace=default_workspace,
        )
        self.backend_factory = backend_factory
        self._procs: dict[str, AgentBackend] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def start(self, factory=None) -> None:  # noqa: ARG002 — interface parity with SessionPool
        """No-op: raw pool spawns lazily per chat."""
        logger.info("RawSessionPool ready (lazy per-chat spawn)")

    # ── Per-chat backend kind (raw-only API) ──

    def set_backend(self, chat_id: str, backend: str) -> None:
        self._get_state(chat_id).backend = backend

    def get_backend(self, chat_id: str) -> str:
        chat_state = self._chat_states.get(chat_id)
        return chat_state.backend if chat_state else ""

    # ── BaseSessionPool hooks ──

    async def _borrow(self, chat_id: str) -> AgentBackend:
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        await lock.acquire()
        try:
            return await self._ensure_proc(chat_id)
        except BaseException:
            lock.release()
            raise

    def _return(self, chat_id: str, proc: AgentBackend) -> None:
        lock = self._locks.get(chat_id)
        if lock and lock.locked():
            lock.release()

    async def _ensure_proc(self, chat_id: str) -> AgentBackend:
        proc = self._procs.get(chat_id)
        if proc is not None:
            return proc
        if not self.backend_factory:
            raise RuntimeError("RawSessionPool: backend_factory not set")
        chat_state = self._get_state(chat_id)
        backend_kind = chat_state.backend or "claude-cli"
        proc = self.backend_factory(
            backend=backend_kind,
            workspace=chat_state.workspace,
            model=chat_state.model,
            session_id=chat_state.session_id,
            bot_name=self.bot_name,
        )
        proc.start()
        self._procs[chat_id] = proc
        logger.info("RawSessionPool spawned %s for chat_id=%s", backend_kind, chat_id)
        return proc

    @property
    def all_processes(self) -> list[AgentBackend]:
        return list(self._procs.values())

    async def stop(self) -> None:
        for chat_id, proc in list(self._procs.items()):
            try:
                await proc.stop()
            except Exception as e:
                logger.warning("Error stopping raw proc for %s: %s", chat_id, e)
        self._procs.clear()
        self._active.clear()
        logger.info("RawSessionPool stopped")

    async def restart_dead(self) -> int:
        """Drop dead per-chat processes; they'll respawn on next acquire."""
        restarted = 0
        for chat_id, proc in list(self._procs.items()):
            if getattr(proc, "state", "idle") == "dead":
                try:
                    await proc.stop()
                except Exception:
                    pass
                del self._procs[chat_id]
                restarted += 1
                logger.info("Dropped dead raw proc for chat_id=%s", chat_id)
        return restarted
