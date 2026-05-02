"""RawSessionPool — per-chat lazy-spawn pool for the virtual ``raw`` bot.

Unlike SessionPool which pre-spawns N processes sharing one backend, the
raw bot can serve any backend (claude / codex / acp) per chat. We therefore
spawn one process per chat_id on first access, keyed by what's stored in
sessions.yaml for that chat. The interface mirrors SessionPool so Router
and Gateway code paths don't need to branch.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class _ChatState:
    backend: str = ""             # "claude-cli" / "codex-cli" / "codex-acp"
    session_id: str | None = None
    model: str = ""
    workspace: str = ""
    proc: object = None           # the lazy-spawned cli process


@dataclass
class RawSessionPool:
    """Per-chat backend processes for the raw bot.

    ``backend_factory(backend, workspace, model, session_id)`` returns a
    started cli process for the requested backend. The pool calls .start()
    on it before returning from acquire().
    """

    storage: object = None
    bot_name: str = "raw"
    default_model: str = ""
    default_workspace: str = ""
    backend_factory: Callable[..., object] | None = None
    _chats: dict[str, _ChatState] = field(default_factory=dict, repr=False)
    _active: dict[str, object] = field(default_factory=dict, repr=False)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, repr=False)

    def start(self, factory=None) -> None:
        """Compat with SessionPool — RawSessionPool spawns lazily."""
        # ``factory`` is ignored: raw bot's backend is per-chat, set via
        # backend_factory at construction time.
        logger.info("RawSessionPool ready (lazy per-chat spawn)")

    # ---- internal ----

    def _get_state(self, chat_id: str) -> _ChatState:
        st = self._chats.get(chat_id)
        if st is None:
            st = _ChatState(model=self.default_model, workspace=self.default_workspace)
            if self.storage and self.bot_name:
                saved = self.storage.load_session(self.bot_name, chat_id=chat_id)
                if isinstance(saved, dict):
                    st.session_id = saved.get("session_id")
                    if saved.get("model"):
                        st.model = saved["model"]
                    if saved.get("workspace"):
                        st.workspace = saved["workspace"]
                    if saved.get("backend"):
                        st.backend = saved["backend"]
                elif isinstance(saved, str):
                    st.session_id = saved
            self._chats[chat_id] = st
        return st

    async def _ensure_proc(self, chat_id: str) -> object:
        st = self._get_state(chat_id)
        if st.proc is not None:
            return st.proc
        if not self.backend_factory:
            raise RuntimeError("RawSessionPool: backend_factory not set")
        backend = st.backend or "claude-cli"
        proc = self.backend_factory(
            backend=backend,
            workspace=st.workspace,
            model=st.model,
            session_id=st.session_id,
            bot_name=self.bot_name,
        )
        proc.start()
        st.proc = proc
        logger.info("RawSessionPool spawned %s for chat_id=%s", backend, chat_id)
        return proc

    # ---- SessionPool-compatible surface ----

    async def acquire(self, chat_id: str) -> object:
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        await lock.acquire()
        try:
            proc = await self._ensure_proc(chat_id)
            st = self._get_state(chat_id)
            proc.session_id = st.session_id
            proc.model = st.model
            proc.workspace = st.workspace
            self._active[chat_id] = proc
            return proc
        except BaseException:
            lock.release()
            raise

    def release(self, chat_id: str, proc: object) -> None:
        st = self._get_state(chat_id)
        st.session_id = proc.session_id
        st.model = proc.model
        st.workspace = proc.workspace
        self._active.pop(chat_id, None)
        lock = self._locks.get(chat_id)
        if lock and lock.locked():
            lock.release()

    def get_active(self, chat_id: str) -> object | None:
        return self._active.get(chat_id)

    def get_session_id(self, chat_id: str) -> str | None:
        active = self._active.get(chat_id)
        if active:
            return active.session_id
        st = self._chats.get(chat_id)
        return st.session_id if st else None

    def set_session_id(self, chat_id: str, session_id: str | None) -> None:
        active = self._active.get(chat_id)
        if active:
            active.session_id = session_id
        self._get_state(chat_id).session_id = session_id

    def get_model(self, chat_id: str) -> str:
        active = self._active.get(chat_id)
        if active:
            return active.model
        st = self._chats.get(chat_id)
        return st.model if st else self.default_model

    def set_model(self, chat_id: str, model: str) -> None:
        active = self._active.get(chat_id)
        if active:
            active.model = model
        self._get_state(chat_id).model = model

    def get_workspace(self, chat_id: str) -> str:
        active = self._active.get(chat_id)
        if active:
            return active.workspace
        st = self._chats.get(chat_id)
        return st.workspace if st else self.default_workspace

    def set_workspace(self, chat_id: str, workspace: str) -> None:
        active = self._active.get(chat_id)
        if active:
            active.workspace = workspace
        self._get_state(chat_id).workspace = workspace

    def set_backend(self, chat_id: str, backend: str) -> None:
        st = self._get_state(chat_id)
        st.backend = backend

    def get_backend(self, chat_id: str) -> str:
        st = self._chats.get(chat_id)
        return st.backend if st else ""

    def clear_session(self, chat_id: str) -> None:
        st = self._chats.get(chat_id)
        if st:
            st.session_id = None
        active = self._active.get(chat_id)
        if active:
            active.session_id = None

    def has_session(self, chat_id: str) -> bool:
        st = self._chats.get(chat_id)
        return bool(st and st.session_id)

    @property
    def all_processes(self) -> list[object]:
        return [st.proc for st in self._chats.values() if st.proc is not None]

    async def stop(self) -> None:
        for chat_id, st in list(self._chats.items()):
            if st.proc is not None:
                try:
                    await st.proc.stop()
                except Exception as e:
                    logger.warning("Error stopping raw proc for %s: %s", chat_id, e)
                st.proc = None
        self._active.clear()
        logger.info("RawSessionPool stopped")

    async def restart_dead(self) -> int:
        """Drop dead per-chat processes; they'll respawn on next acquire."""
        restarted = 0
        for chat_id, st in self._chats.items():
            proc = st.proc
            if proc is None:
                continue
            if getattr(proc, "state", "idle") == "dead":
                try:
                    await proc.stop()
                except Exception:
                    pass
                st.proc = None
                restarted += 1
                logger.info("Dropped dead raw proc for chat_id=%s", chat_id)
        return restarted
