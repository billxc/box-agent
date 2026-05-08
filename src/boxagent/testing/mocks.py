"""Mock backend for tests.

Implements ``boxagent.agent.protocol.AgentBackend``. Records every
``send`` / ``cancel`` / ``reset_session`` / ``stop`` call so tests can
assert. Lets tests script the callback events emitted during a turn.

Example:

    backend = MockBackend(bot_name="test")
    backend.start()
    backend.script(["hello", "world"])           # two stream chunks
    await backend.send("hi", callback)
    assert backend.sends == [SendCall(message="hi", ...)]
    assert callback.stream_chunks == ["hello", "world"]
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal

if TYPE_CHECKING:
    from boxagent.agent.callback import AgentCallback
    from boxagent.agent_env import AgentEnv


@dataclass
class SendCall:
    """Recorded ``send`` invocation."""

    message: str
    model: str
    chat_id: str
    append_system_prompt: str
    env: "AgentEnv | None"


@dataclass
class MockBackend:
    """Test double for ``AgentBackend``.

    Drop-in for any ``backend: AgentBackend`` field. State transitions
    mimic the real backend: idle → busy (during ``send``) → idle (or dead
    after ``stop``).

    Scripting (one of three modes per turn):
    - ``script(chunks)``: emit each str as ``callback.on_stream(chunk)``,
      then return.
    - ``script_handler(fn)``: full control — pass an async fn that
      receives ``(message, callback, **kwargs)`` and emits whatever.
    - default (no script): emit a single ``on_stream("ok")`` then return.

    Failure simulation:
    - ``fail_next_turn(error)``: the next ``send`` will leave
      ``last_turn_failed=True`` and ``last_turn_error=error`` after it
      finishes (callbacks still fire; this models post-turn detection).
    """

    bot_name: str = "mock"
    workspace: str = "/tmp/mock-workspace"
    model: str = ""
    agent: str = ""
    session_id: str | None = None
    state: Literal["idle", "busy", "dead"] = "idle"
    supports_session_persistence: bool = True
    yolo: bool = False
    last_turn_failed: bool = False
    last_turn_error: str = ""

    started: bool = field(default=False, init=False)
    stopped: bool = field(default=False, init=False)

    sends: list[SendCall] = field(default_factory=list, init=False)
    cancel_count: int = field(default=0, init=False)
    reset_session_count: int = field(default=0, init=False)

    _scripted_chunks: list[str] | None = field(default=None, init=False, repr=False)
    _scripted_handler: Any = field(default=None, init=False, repr=False)
    _next_failure: tuple[bool, str] | None = field(default=None, init=False, repr=False)
    _idle_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    def __post_init__(self) -> None:
        self._idle_event.set()

    # ── Test scripting API ──

    def script(self, chunks: list[str]) -> None:
        """Have the next ``send`` emit each str via ``on_stream``."""
        self._scripted_chunks = list(chunks)
        self._scripted_handler = None

    def script_handler(
        self,
        handler: Callable[..., Any],
    ) -> None:
        """Have the next ``send`` delegate to a custom async fn.

        The fn signature: ``async def(message, callback, *, model, chat_id,
        append_system_prompt, env) -> None``.
        """
        self._scripted_handler = handler
        self._scripted_chunks = None

    def fail_next_turn(self, error: str = "mock error") -> None:
        """The next ``send`` ends with ``last_turn_failed=True`` and the
        given error message."""
        self._next_failure = (True, error)

    # ── AgentBackend protocol ──

    def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        await self.cancel()
        self.stopped = True
        self.state = "dead"

    async def send(
        self,
        message: str,
        callback: "AgentCallback",
        model: str = "",
        chat_id: str = "",
        append_system_prompt: str = "",
        env: "AgentEnv | None" = None,
    ) -> None:
        self.sends.append(SendCall(
            message=message,
            model=model,
            chat_id=chat_id,
            append_system_prompt=append_system_prompt,
            env=env,
        ))
        # Reset per-turn diagnostics — set again at end if the turn failed.
        self.last_turn_failed = False
        self.last_turn_error = ""
        self.state = "busy"
        self._idle_event.clear()
        try:
            handler = self._scripted_handler
            chunks = self._scripted_chunks
            self._scripted_handler = None
            self._scripted_chunks = None
            if handler is not None:
                await handler(
                    message, callback,
                    model=model,
                    chat_id=chat_id,
                    append_system_prompt=append_system_prompt,
                    env=env,
                )
            elif chunks is not None:
                for chunk in chunks:
                    await callback.on_stream(chunk)
            else:
                await callback.on_stream("ok")
        finally:
            failure = self._next_failure
            self._next_failure = None
            if failure is not None:
                self.last_turn_failed, self.last_turn_error = failure
            self.state = "idle"
            self._idle_event.set()

    async def cancel(self) -> None:
        self.cancel_count += 1
        self.state = "idle"
        self._idle_event.set()

    async def reset_session(self) -> None:
        self.reset_session_count += 1
        await self.cancel()
        self.session_id = None

    async def wait_idle(self) -> None:
        await self._idle_event.wait()
