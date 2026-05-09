"""AgentBackend protocol — interface every AI backend must satisfy.

The Router, Scheduler, and Watchdog hold an ``AgentBackend`` reference and
talk to it through this surface. Any concrete backend (Claude CLI, Codex
CLI, or a test mock) implements this Protocol.

This is a structural Protocol — implementations don't need to inherit;
matching shape is enough for type-checkers. Use ``runtime_checkable`` so
``isinstance(obj, AgentBackend)`` works for sanity checks.

Design rules:
- Methods/attributes here are the **stable surface** every backend must
  honour. Adding to this Protocol is a coordinated change.
- Backend-specific knobs (Claude's ``--agent`` flag, Codex's MCP injection
  hooks, etc.) live on concrete classes, not the Protocol.
- Per-turn diagnostics (``last_turn_failed``, ``last_turn_error``) are
  exposed so callers can react to failures without needing exception
  handling around every ``send``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from boxagent.agent.callback import AgentCallback
    from boxagent.agent_env import AgentEnv


BackendState = Literal["idle", "busy", "dead"]

# Single source of truth for valid `ai_backend` strings. Used by the
# /backend command's allow-list, the create_backend factory, and
# _supports_persistent_session.
BACKEND_KINDS: frozenset[str] = frozenset({
    "claude-cli",
    "codex-cli",
    "agent-sdk-claude",
    "agent-sdk-copilot",
})


@runtime_checkable
class AgentBackend(Protocol):
    """Single AI backend instance owned by one bot.

    Lifecycle:
        backend = make_backend(...)
        backend.start()                          # spin up worker loop
        await backend.send(msg, callback, ...)   # one turn (callback streams)
        await backend.cancel()                   # interrupt current turn
        await backend.reset_session()            # cancel + drop session_id
        await backend.stop()                     # graceful shutdown

    State machine: idle → busy (during send) → idle | dead.
    """

    # ── Identity / config (mutable: /model, /cd, resume can rewrite) ──
    bot_name: str
    workspace: str
    model: str
    agent: str
    session_id: str | None
    state: BackendState

    # ── Capability flags ──
    # ``supports_session_persistence``: backend can resume the same
    #   conversation after process restart by re-using ``session_id``.
    # ``yolo``: skip permission prompts (where the backend supports such
    #   a notion). Read by env_builder; defaults to False on backends
    #   that don't expose it.
    supports_session_persistence: bool
    yolo: bool

    # ── Per-turn diagnostics (set by send, read by callers) ──
    # After ``await send(...)`` returns:
    #   - last_turn_failed=True if the turn raised or the subprocess
    #     died mid-turn
    #   - last_turn_error carries a human-readable error message
    last_turn_failed: bool
    last_turn_error: str

    # ── Lifecycle ──
    def start(self) -> None:
        """Start the backend's message-processing loop. Sync; non-blocking."""
        ...

    async def stop(self) -> None:
        """Graceful shutdown: cancel current turn, drain queue, mark dead."""
        ...

    # ── Per-turn ──
    async def send(
        self,
        message: str,
        callback: "AgentCallback",
        model: str = "",
        chat_id: str = "",
        append_system_prompt: str = "",
        env: "AgentEnv | None" = None,
    ) -> None:
        """Run one turn: deliver ``message`` to the backend, stream output
        through ``callback``. Returns when the turn completes (or is
        cancelled / errors). Errors are reported via:

        - ``callback.on_error(...)`` for in-stream errors the backend
          recovered from
        - ``self.last_turn_failed`` + ``self.last_turn_error`` for the
          final disposition of this turn (also raised exceptions are
          captured here, not propagated)

        Optional overrides:
        - ``model``: per-turn model override (e.g. ``@opus`` prefix).
        - ``chat_id``: routing tag for per-chat context (workspace, session).
        - ``append_system_prompt``: additional system prompt text appended
          to the backend's default for this turn.
        - ``env``: rich agent environment (peers, channel info, etc.).
        """
        ...

    async def cancel(self) -> None:
        """Cancel the in-flight turn. Idempotent. Returns when the worker
        has acknowledged the cancellation and is back to idle."""
        ...

    async def reset_session(self) -> None:
        """Drop session continuity: cancel any active turn and clear
        ``session_id`` so the next ``send`` starts a fresh conversation."""
        ...

    async def wait_idle(self) -> None:
        """Block until the backend is back to idle (no turn in flight)."""
        ...
