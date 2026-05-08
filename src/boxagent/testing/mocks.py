"""Mock backend + channel for tests.

Implements ``boxagent.agent.protocol.AgentBackend`` and
``boxagent.transports.base.Channel``. Records every interaction so tests
can assert on them; lets tests script callback events / channel
behaviour without re-discovering the interface every time.

Example (backend):

    backend = MockBackend(bot_name="test")
    backend.start()
    backend.script(["hello", "world"])           # two stream chunks
    await backend.send("hi", callback)
    assert backend.sends == [SendCall(message="hi", ...)]

Example (channel):

    channel = MockChannel()
    await channel.send_text("123", "hi there")
    assert channel.sent_texts == [("123", "hi there")]
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

from boxagent.agent.protocol import AgentBackend
from boxagent.transports.base import Channel, IncomingMessage, StreamHandle

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
class MockBackend(AgentBackend):
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


# ── MockChannel ────────────────────────────────────────────────────────


@dataclass
class StreamRecord:
    """Recorded stream lifecycle: (message_id, chunks emitted, finalized)."""

    message_id: str
    chat_id: str
    chunks: list[str] = field(default_factory=list)
    final_text: str = ""
    closed: bool = False


@dataclass
class ToolCallRecord:
    """Recorded ``on_tool_call`` invocation."""

    chat_id: str
    tool_id: str
    name: str
    input: dict
    result: str


@dataclass
class ToolUpdateRecord:
    """Recorded ``on_tool_update`` invocation."""

    chat_id: str
    tool_call_id: str
    title: str
    status: str | None
    input: object
    output: object


@dataclass
class MockChannel(Channel):
    """Test double for ``Channel``.

    Drop-in for any ``channel: Channel`` field. Records every outbound
    interaction (``sent_texts``, ``streams``, ``tool_calls``,
    ``tool_updates``, ``typing_calls``) so tests can assert on the wire.

    To simulate an inbound message (Telegram delivery, web /api/send),
    call ``await channel.deliver(IncomingMessage(...))`` — this invokes
    the registered ``on_message`` callback synchronously.

    By default ``on_tool_call`` / ``on_tool_update`` return False (no
    paragraph break needed). Override via ``tool_call_uses_stream`` /
    ``tool_update_uses_stream``.
    """

    on_message: Callable[[IncomingMessage], Awaitable[None]] = field(
        default=None, repr=False  # set via .deliver() / Router wiring
    )

    started: bool = field(default=False, init=False)
    stopped: bool = field(default=False, init=False)

    sent_texts: list[tuple[str, str]] = field(default_factory=list, init=False)
    streams: list[StreamRecord] = field(default_factory=list, init=False)
    tool_calls: list[ToolCallRecord] = field(default_factory=list, init=False)
    tool_updates: list[ToolUpdateRecord] = field(default_factory=list, init=False)
    typing_calls: list[str] = field(default_factory=list, init=False)

    tool_call_uses_stream: bool = False
    tool_update_uses_stream: bool = False

    _next_message_id: int = field(default=1, init=False, repr=False)

    # ── Channel protocol ──

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def send_text(
        self,
        chat_id: str,
        text: str,
        parse_mode: str = "Markdown",
        **kwargs,
    ) -> str:
        self.sent_texts.append((chat_id, text))
        return self._mint_id()

    async def stream_start(self, chat_id: str, **kwargs) -> StreamHandle:
        record = StreamRecord(message_id=self._mint_id(), chat_id=chat_id)
        self.streams.append(record)
        return StreamHandle(message_id=record.message_id, chat_id=chat_id)

    async def stream_update(self, handle: StreamHandle, text: str) -> None:
        record = self._find_stream(handle.message_id)
        if record is not None:
            record.chunks.append(text)

    async def stream_end(self, handle: StreamHandle) -> str:
        record = self._find_stream(handle.message_id)
        if record is not None:
            record.final_text = record.chunks[-1] if record.chunks else ""
            record.closed = True
        return handle.message_id

    async def show_typing(self, chat_id: str) -> None:
        self.typing_calls.append(chat_id)

    async def on_tool_call(
        self,
        chat_id: str,
        tool_id: str,
        name: str,
        input: dict,
        result: str,
        *,
        stream_handle: StreamHandle | None = None,
        webhook_name: str = "",
    ) -> bool:
        self.tool_calls.append(ToolCallRecord(
            chat_id=chat_id, tool_id=tool_id, name=name,
            input=input, result=result,
        ))
        return self.tool_call_uses_stream

    async def on_tool_update(
        self,
        chat_id: str,
        tool_call_id: str,
        title: str,
        *,
        status: str | None = None,
        input: object = None,
        output: object = None,
        stream_handle: StreamHandle | None = None,
        webhook_name: str = "",
    ) -> bool:
        self.tool_updates.append(ToolUpdateRecord(
            chat_id=chat_id, tool_call_id=tool_call_id, title=title,
            status=status, input=input, output=output,
        ))
        return self.tool_update_uses_stream

    # ── Test utilities ──

    async def deliver(self, msg: IncomingMessage) -> None:
        """Simulate an inbound message by invoking the wired ``on_message``.

        The default ``on_message`` is None; raise an explicit error so
        tests don't silently no-op.
        """
        if self.on_message is None:
            raise RuntimeError(
                "MockChannel.on_message is unset — wire it (e.g. via Router) "
                "before calling .deliver()."
            )
        await self.on_message(msg)

    def _mint_id(self) -> str:
        out = f"mock-{self._next_message_id}"
        self._next_message_id += 1
        return out

    def _find_stream(self, message_id: str) -> StreamRecord | None:
        for s in self.streams:
            if s.message_id == message_id:
                return s
        return None
