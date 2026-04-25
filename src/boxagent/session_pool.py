"""SessionPool — connection-pool style manager for CLI backend processes."""

import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DEFAULT_POOL_SIZE = 3


@dataclass
class SessionPool:
    """Pool of cli_processes shared across chats.

    Each chat gets its own session_id but borrows a process from the pool
    for the duration of a turn.  Different chats can run concurrently up
    to ``size`` simultaneous turns.
    """

    size: int = DEFAULT_POOL_SIZE
    _factory: object = None  # Callable[[], cli_process]
    _pool: asyncio.Queue = field(default=None, repr=False)
    _chat_sessions: dict[str, str | None] = field(default_factory=dict, repr=False)
    _active: dict[str, object] = field(default_factory=dict, repr=False)
    _all: list[object] = field(default_factory=list, repr=False)

    def __post_init__(self):
        self._pool = asyncio.Queue(maxsize=self.size)

    def start(self, factory) -> None:
        """Create pool members and start their queues.

        ``factory`` is a callable returning a fresh cli_process (already
        configured with workspace/model/etc but no session_id).
        """
        self._factory = factory
        for _ in range(self.size):
            proc = factory()
            proc.start()
            self._all.append(proc)
            self._pool.put_nowait(proc)
        logger.info("SessionPool started with %d processes", self.size)

    async def acquire(self, chat_id: str) -> object:
        """Borrow a process for *chat_id*, setting its session_id."""
        proc = await self._pool.get()
        proc.session_id = self._chat_sessions.get(chat_id)
        self._active[chat_id] = proc
        return proc

    def release(self, chat_id: str, proc: object) -> None:
        """Return a process to the pool, saving its session_id."""
        self._chat_sessions[chat_id] = proc.session_id
        self._active.pop(chat_id, None)
        proc.session_id = None
        self._pool.put_nowait(proc)

    def get_active(self, chat_id: str) -> object | None:
        """Return the process currently serving *chat_id*, if any."""
        return self._active.get(chat_id)

    def get_session_id(self, chat_id: str) -> str | None:
        """Return the stored session_id for *chat_id*."""
        active = self._active.get(chat_id)
        if active:
            return active.session_id
        return self._chat_sessions.get(chat_id)

    def set_session_id(self, chat_id: str, session_id: str | None) -> None:
        """Directly set the session_id for *chat_id*."""
        active = self._active.get(chat_id)
        if active:
            active.session_id = session_id
        self._chat_sessions[chat_id] = session_id

    def clear_session(self, chat_id: str) -> None:
        """Drop session continuity for *chat_id*."""
        self._chat_sessions.pop(chat_id, None)
        active = self._active.get(chat_id)
        if active:
            active.session_id = None

    def has_session(self, chat_id: str) -> bool:
        """Whether *chat_id* has a stored session_id."""
        return bool(self._chat_sessions.get(chat_id))

    @property
    def all_processes(self) -> list[object]:
        """All processes in the pool (for watchdog monitoring)."""
        return list(self._all)

    async def stop(self) -> None:
        """Stop all processes in the pool."""
        for proc in self._all:
            try:
                await proc.stop()
            except Exception as e:
                logger.warning("Error stopping pool process: %s", e)
        self._all.clear()
        # Drain the queue
        while not self._pool.empty():
            try:
                self._pool.get_nowait()
            except asyncio.QueueEmpty:
                break
        logger.info("SessionPool stopped")

    async def restart_dead(self) -> int:
        """Replace any dead processes in the pool. Returns count restarted."""
        if not self._factory:
            return 0
        restarted = 0
        new_all = []
        for proc in self._all:
            if getattr(proc, "state", "idle") == "dead":
                try:
                    await proc.stop()
                except Exception:
                    pass
                new_proc = self._factory()
                new_proc.start()
                new_all.append(new_proc)
                # If the dead process was in the pool queue, we need to swap it
                # We'll rebuild the queue after
                restarted += 1
                logger.info("Replaced dead pool process")
            else:
                new_all.append(proc)
        if restarted:
            self._all = new_all
            # Rebuild pool queue with only idle (non-active) processes
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
