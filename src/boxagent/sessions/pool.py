"""SessionPool — connection-pool style manager for CLI backend processes."""

import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DEFAULT_POOL_SIZE = 3


@dataclass
class ChatContext:
    """Per-chat state restored onto a borrowed process."""

    session_id: str | None = None
    model: str = ""
    workspace: str = ""


@dataclass
class SessionPool:
    """Pool of cli_processes shared across chats.

    Each chat gets its own session_id, model, and workspace but borrows
    a process from the pool for the duration of a turn.  Different chats
    can run concurrently up to ``size`` simultaneous turns.
    """

    size: int = DEFAULT_POOL_SIZE
    default_model: str = ""
    default_workspace: str = ""
    storage: object = None  # Storage instance for lazy-loading saved sessions
    bot_name: str = ""
    _factory: object = None  # Callable[[], cli_process]
    _pool: asyncio.Queue = field(default=None, repr=False)
    _chat_contexts: dict[str, ChatContext] = field(default_factory=dict, repr=False)
    _active: dict[str, object] = field(default_factory=dict, repr=False)
    _all: list[object] = field(default_factory=list, repr=False)

    def __post_init__(self):
        self._pool = asyncio.Queue(maxsize=self.size)

    def _get_ctx(self, chat_id: str) -> ChatContext:
        """Get or create context for a chat.

        On first access, tries to restore session state from storage.
        """
        ctx = self._chat_contexts.get(chat_id)
        if ctx is None:
            ctx = ChatContext(
                model=self.default_model,
                workspace=self.default_workspace,
            )
            if self.storage and self.bot_name:
                saved = self.storage.load_session(self.bot_name, chat_id=chat_id)
                if isinstance(saved, dict):
                    ctx.session_id = saved.get("session_id")
                    if saved.get("model"):
                        ctx.model = saved["model"]
                    if saved.get("workspace"):
                        ctx.workspace = saved["workspace"]
                elif isinstance(saved, str):
                    # Legacy format: plain session_id string
                    ctx.session_id = saved
            self._chat_contexts[chat_id] = ctx
        return ctx

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
        """Borrow a process for *chat_id*, restoring its context."""
        proc = await self._pool.get()
        ctx = self._get_ctx(chat_id)
        proc.session_id = ctx.session_id
        proc.model = ctx.model
        proc.workspace = ctx.workspace
        self._active[chat_id] = proc
        return proc

    def release(self, chat_id: str, proc: object) -> None:
        """Return a process to the pool, saving its context."""
        ctx = self._get_ctx(chat_id)
        ctx.session_id = proc.session_id
        ctx.model = proc.model
        ctx.workspace = proc.workspace
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
        ctx = self._chat_contexts.get(chat_id)
        return ctx.session_id if ctx else None

    def set_session_id(self, chat_id: str, session_id: str | None) -> None:
        """Directly set the session_id for *chat_id*."""
        active = self._active.get(chat_id)
        if active:
            active.session_id = session_id
        self._get_ctx(chat_id).session_id = session_id

    def get_model(self, chat_id: str) -> str:
        """Return the model for *chat_id*."""
        active = self._active.get(chat_id)
        if active:
            return active.model
        ctx = self._chat_contexts.get(chat_id)
        return ctx.model if ctx else self.default_model

    def set_model(self, chat_id: str, model: str) -> None:
        """Set the model for *chat_id*."""
        active = self._active.get(chat_id)
        if active:
            active.model = model
        self._get_ctx(chat_id).model = model

    def get_workspace(self, chat_id: str) -> str:
        """Return the workspace for *chat_id*."""
        active = self._active.get(chat_id)
        if active:
            return active.workspace
        ctx = self._chat_contexts.get(chat_id)
        return ctx.workspace if ctx else self.default_workspace

    def set_workspace(self, chat_id: str, workspace: str) -> None:
        """Set the workspace for *chat_id*."""
        active = self._active.get(chat_id)
        if active:
            active.workspace = workspace
        self._get_ctx(chat_id).workspace = workspace

    def clear_session(self, chat_id: str) -> None:
        """Drop session continuity for *chat_id* (keeps model/workspace)."""
        ctx = self._chat_contexts.get(chat_id)
        if ctx:
            ctx.session_id = None
        active = self._active.get(chat_id)
        if active:
            active.session_id = None

    def has_session(self, chat_id: str) -> bool:
        """Whether *chat_id* has a stored session_id."""
        ctx = self._chat_contexts.get(chat_id)
        return bool(ctx and ctx.session_id)

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
