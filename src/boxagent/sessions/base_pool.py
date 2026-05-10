"""Shared base for session pools.

A session pool maps ``chat_id`` → backend instance. Two concrete shapes
exist on top of this base:

- ``SessionPool``: a fixed-size queue of pre-spawned backends shared across
  chats (one backend kind per pool).
- ``RawSessionPool``: per-chat lazy spawn, where each chat can request a
  different backend kind.

This base owns:

- Per-chat state (``ChatState``) — session_id, model, workspace, plus
  optional ``backend`` (kind) for raw pools.
- Lazy load from ``Storage`` on first access to a new chat_id.
- Restore state to a borrowed backend (``_restore_to``) and capture it
  back on release (``_capture_from``).
- All eight get/set pairs (session_id, model, workspace) — they all share
  the same "look in active first, else look in saved state" pattern.

Subclasses override how a backend is borrowed/returned (``_borrow``,
``_return``), how the pool starts and stops, and how dead procs are
recycled.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from boxagent.agent.protocol import AgentBackend
    from boxagent.sessions.storage import Storage

logger = logging.getLogger(__name__)


@dataclass
class ChatState:
    """Per-chat state restored onto a borrowed backend on each turn.

    ``backend`` (the kind string, e.g. "claude-cli") is only meaningful
    for raw pools where each chat picks its own backend kind.
    """

    session_id: str | None = None
    model: str = ""
    workspace: str = ""
    backend: str = ""  # kind string — used by RawSessionPool only


class BaseSessionPool(ABC):
    """Shared per-chat state management for session pools.

    Concrete subclasses implement how a backend is borrowed and returned;
    this base handles everything else: state loading, get/set fan-out
    (active backend + saved state), and the storage round-trip.
    """

    def __init__(
        self,
        *,
        storage: "Storage | None" = None,
        bot_name: str = "",
        default_model: str = "",
        default_workspace: str = "",
    ) -> None:
        self.storage = storage
        self.bot_name = bot_name
        self.default_model = default_model
        self.default_workspace = default_workspace
        self._chat_states: dict[str, ChatState] = {}
        self._active: dict[str, AgentBackend] = {}

    # ── State access (lazy load from storage) ──

    def _get_state(self, chat_id: str) -> ChatState:
        chat_state = self._chat_states.get(chat_id)
        if chat_state is None:
            chat_state = ChatState(
                model=self.default_model,
                workspace=self.default_workspace,
            )
            if self.storage and self.bot_name:
                saved = self.storage.load_session(self.bot_name, chat_id=chat_id)
                if isinstance(saved, dict):
                    chat_state.session_id = saved.get("session_id")
                    if saved.get("model"):
                        chat_state.model = saved["model"]
                    if saved.get("workspace"):
                        chat_state.workspace = saved["workspace"]
                    if saved.get("backend"):
                        chat_state.backend = saved["backend"]
                elif isinstance(saved, str):
                    chat_state.session_id = saved
            self._chat_states[chat_id] = chat_state
        return chat_state

    def _restore_to(self, backend: AgentBackend, chat_state: ChatState) -> None:
        """Apply a saved chat state onto a borrowed backend."""
        backend.session_id = chat_state.session_id
        backend.model = chat_state.model
        backend.workspace = chat_state.workspace

    def _capture_from(self, chat_state: ChatState, backend: AgentBackend) -> None:
        """Capture the post-turn state of a backend back into the chat record."""
        chat_state.session_id = backend.session_id
        chat_state.model = backend.model
        chat_state.workspace = backend.workspace

    # ── Get / set fan-out (active backend + saved state stay in sync) ──

    def get_active(self, chat_id: str) -> AgentBackend | None:
        return self._active.get(chat_id)

    def get_session_id(self, chat_id: str) -> str | None:
        active = self._active.get(chat_id)
        if active:
            return active.session_id
        chat_state = self._chat_states.get(chat_id)
        return chat_state.session_id if chat_state else None

    def set_session_id(self, chat_id: str, session_id: str | None) -> None:
        active = self._active.get(chat_id)
        if active:
            active.session_id = session_id
        self._get_state(chat_id).session_id = session_id

    def get_model(self, chat_id: str) -> str:
        active = self._active.get(chat_id)
        if active:
            return active.model
        chat_state = self._chat_states.get(chat_id)
        return chat_state.model if chat_state else self.default_model

    def set_model(self, chat_id: str, model: str) -> None:
        active = self._active.get(chat_id)
        if active:
            active.model = model
        self._get_state(chat_id).model = model

    def get_workspace(self, chat_id: str) -> str:
        active = self._active.get(chat_id)
        if active:
            return active.workspace
        chat_state = self._chat_states.get(chat_id)
        return chat_state.workspace if chat_state else self.default_workspace

    def set_workspace(self, chat_id: str, workspace: str) -> None:
        active = self._active.get(chat_id)
        if active:
            active.workspace = workspace
        self._get_state(chat_id).workspace = workspace

    def clear_session(self, chat_id: str) -> None:
        """Drop session continuity (keeps model/workspace)."""
        chat_state = self._chat_states.get(chat_id)
        if chat_state:
            chat_state.session_id = None
        active = self._active.get(chat_id)
        if active:
            active.session_id = None

    def has_session(self, chat_id: str) -> bool:
        chat_state = self._chat_states.get(chat_id)
        return bool(chat_state and chat_state.session_id)

    # ── acquire / release wrap subclass-specific borrow/return ──

    async def acquire(self, chat_id: str) -> AgentBackend:
        backend = await self._borrow(chat_id)
        self._restore_to(backend, self._get_state(chat_id))
        self._active[chat_id] = backend
        return backend

    def release(self, chat_id: str, backend: AgentBackend) -> None:
        self._capture_from(self._get_state(chat_id), backend)
        self._active.pop(chat_id, None)
        self._return(chat_id, backend)

    # ── Subclass hooks ──

    @abstractmethod
    async def _borrow(self, chat_id: str) -> AgentBackend:
        """Return a backend instance ready to serve ``chat_id``.

        Implementations decide whether to block on a shared queue, spawn
        lazily per-chat, etc.
        """

    @abstractmethod
    def _return(self, chat_id: str, backend: AgentBackend) -> None:
        """Return a backend after a turn — requeue, release lock, etc."""

    @property
    @abstractmethod
    def all_processes(self) -> list[AgentBackend]:
        """All currently-spawned backends (for watchdog scanning)."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop every spawned backend and clear pool state."""

    @abstractmethod
    async def restart_dead(self) -> int:
        """Recycle dead backends. Return how many were restarted."""
