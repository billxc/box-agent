"""AgentSDKCopilot — GitHub Copilot backend via ``github-copilot-sdk``.

Parallels :class:`AgentSDKClaude`: in-process bridge to a vendor SDK,
satisfying ``AgentBackend`` so it drops into Router / Watchdog /
SessionPool unchanged.

Lifecycle differs slightly from the Claude SDK:

- The Copilot SDK manages a long-lived ``CopilotClient`` (subprocess to
  the Copilot CLI). We create one client per backend instance and keep
  it across turns.
- A ``CopilotSession`` is created lazily on the first ``send`` (or
  resumed if ``session_id`` is already set) and kept alive for
  subsequent turns until ``reset_session`` destroys it.

Both ``CopilotClient.start`` and ``CopilotSession.send`` are async, so
the synchronous ``AgentBackend.start()`` is a no-op marker — actual
work happens lazily in ``_ensure_started`` from inside ``send``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from copilot import CopilotClient, CopilotSession
from copilot.generated.session_events import (
    AssistantMessageData,
    AssistantMessageDeltaData,
    AssistantStreamingDeltaData,
    SessionErrorData,
    SessionIdleData,
    ToolExecutionCompleteData,
    ToolExecutionStartData,
)
from copilot.session import PermissionHandler, PermissionRequestResult

from boxagent.agent.callback import AgentCallback
from boxagent.agent.protocol import AgentBackend

if TYPE_CHECKING:
    from boxagent.agent_env import AgentEnv

logger = logging.getLogger(__name__)


def _deny_all(request: Any, invocation: dict[str, str]) -> PermissionRequestResult:
    """Permission handler that rejects every tool call.

    Used in non-yolo mode until we wire interactive approval through a
    channel. The agent will just see tool-denied results and adapt.
    """
    return PermissionRequestResult(kind="deny-once")


@dataclass
class AgentSDKCopilot(AgentBackend):
    """GitHub Copilot backend powered by ``github-copilot-sdk``."""

    workspace: str = ""
    session_id: str | None = None
    model: str = ""
    agent: str = ""  # not used by Copilot SDK; kept for AgentBackend symmetry
    bot_name: str = ""
    yolo: bool = False
    state: Literal["idle", "busy", "dead"] = "idle"
    supports_session_persistence: bool = field(default=True, init=False, repr=False)
    last_turn_failed: bool = field(default=False, init=False)
    last_turn_error: str = field(default="", init=False)

    _client: CopilotClient | None = field(default=None, init=False, repr=False)
    _session: CopilotSession | None = field(default=None, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _idle_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    # Per-turn state — reset at start of send.
    _turn_complete: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _active_callback: AgentCallback | None = field(default=None, init=False, repr=False)
    _tool_inputs: dict[str, dict] = field(default_factory=dict, init=False, repr=False)
    _tool_names: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    record_received_stream: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._idle_event.set()

    # ── Lifecycle ──

    def start(self) -> None:
        """Mark intent to start. The actual ``client.start()`` runs lazily
        from ``send`` because it is async (and this hook is sync to match
        the ``AgentBackend`` protocol used by the CLI backends)."""
        self._started = True

    async def stop(self) -> None:
        await self.cancel()
        if self._session is not None:
            try:
                await self._session.disconnect()
            except Exception as e:
                logger.warning("CopilotSession.disconnect failed: %s", e)
            self._session = None
        if self._client is not None:
            try:
                await self._client.stop()
            except Exception as e:
                logger.warning("CopilotClient.stop failed: %s", e)
            self._client = None
        self.state = "dead"

    # ── Per-turn ──

    async def send(
        self,
        message: str,
        callback: AgentCallback,
        model: str = "",
        chat_id: str = "",
        append_system_prompt: str = "",
        env: "AgentEnv | None" = None,
    ) -> None:
        await self._ensure_started()
        # Copilot SDK only accepts ``system_message`` at create_session time
        # (no per-turn override). The first turn's append_system_prompt
        # therefore sticks for the whole session — to refresh it the caller
        # must reset_session() (or do anything that drops the session, e.g.
        # /new, /compact, /backend). Static parts of BoxAgent context (bot
        # name, workspace, BOXAGENT.md, peer list) match this lifecycle
        # well; dynamic parts (time, running_tasks) drift across turns.
        await self._ensure_session(
            model=model or self.model,
            append_system_prompt=append_system_prompt,
        )

        # Allow per-turn model override even after session start.
        if model and model != self.model:
            try:
                self._session.set_model(model)  # type: ignore[union-attr]
            except Exception as e:
                logger.warning("Copilot set_model(%s) failed: %s", model, e)

        self.state = "busy"
        self._idle_event.clear()
        self._turn_complete.clear()
        self._active_callback = callback
        self._tool_inputs.clear()
        self._tool_names.clear()
        self.record_received_stream = False
        self.last_turn_failed = False
        self.last_turn_error = ""

        try:
            await self._session.send(message)  # type: ignore[union-attr]
            await self._turn_complete.wait()
        except asyncio.CancelledError:
            try:
                await self._session.abort()  # type: ignore[union-attr]
            except Exception:
                pass
            raise
        except Exception as e:
            self.last_turn_failed = True
            self.last_turn_error = f"Turn failed: {e}"
            logger.exception("AgentSDKCopilot turn failed")
            try:
                await callback.on_error(self.last_turn_error)
            except Exception:
                pass
        finally:
            self._active_callback = None
            self.state = "idle"
            self._idle_event.set()

    async def cancel(self) -> None:
        if self._session is not None:
            try:
                await self._session.abort()
            except Exception as e:
                logger.warning("CopilotSession.abort failed: %s", e)
        # Unblock any in-flight send waiting on turn completion.
        self._turn_complete.set()
        self.state = "idle"
        self._idle_event.set()

    async def reset_session(self) -> None:
        await self.cancel()
        if self._session is not None:
            try:
                await self._session.disconnect()
            except Exception as e:
                logger.warning("CopilotSession.disconnect on reset failed: %s", e)
            self._session = None
        self.session_id = None

    async def wait_idle(self) -> None:
        await self._idle_event.wait()

    # ── Internal ──

    async def _ensure_started(self) -> None:
        if self._client is not None:
            return
        self._client = CopilotClient()
        await self._client.start()

    async def _ensure_session(self, *, model: str, append_system_prompt: str = "") -> None:
        if self._session is not None:
            return
        kwargs: dict[str, Any] = {
            "on_permission_request": (
                PermissionHandler.approve_all if self.yolo else _deny_all
            ),
            "on_event": self._on_event,
            "working_directory": self.workspace or None,
            # Without streaming=True the SDK only emits the final
            # AssistantMessageData; with it we also get
            # AssistantStreamingDeltaData chunks during generation.
            "streaming": True,
        }
        if model:
            kwargs["model"] = model
        if append_system_prompt:
            kwargs["system_message"] = {
                "mode": "append",
                "content": append_system_prompt,
            }

        if self.session_id:
            try:
                self._session = await self._client.resume_session(  # type: ignore[union-attr]
                    self.session_id, **kwargs,
                )
                logger.info("Copilot resumed session %s", self.session_id)
                return
            except Exception as e:
                logger.warning(
                    "Copilot resume_session(%s) failed (%s); creating fresh",
                    self.session_id, e,
                )
                self.session_id = None

        self._session = await self._client.create_session(**kwargs)  # type: ignore[union-attr]
        self.session_id = self._session.session_id
        logger.info("Copilot created session %s", self.session_id)

    def _on_event(self, event: Any) -> None:
        """Translate one Copilot ``SessionEvent`` into ``AgentCallback`` calls.

        Runs synchronously on the SDK's callback thread. We schedule
        async callbacks via ``asyncio.create_task`` because the AgentCallback
        methods are coroutines. Ordering across awaits is preserved by the
        event loop's task queue (events arrive serialised).
        """
        cb = self._active_callback
        if cb is None:
            return
        data = event.data

        if isinstance(data, AssistantStreamingDeltaData):
            text = getattr(data, "content", "") or getattr(data, "delta", "")
            if text:
                self.record_received_stream = True
                self._schedule(cb.on_stream(text))
            return

        if isinstance(data, AssistantMessageDeltaData):
            text = getattr(data, "content", "") or ""
            if text:
                self.record_received_stream = True
                self._schedule(cb.on_stream(text))
            return

        if isinstance(data, AssistantMessageData):
            # Final assistant message. When streaming=True these arrive
            # *after* the streaming deltas — emit only if we haven't seen
            # any streaming chunks yet (defensive fallback). Otherwise
            # the user would see the full text duplicated at the end.
            if not self.record_received_stream:
                text = getattr(data, "content", "") or ""
                if text:
                    self._schedule(cb.on_stream(text))
            return

        if isinstance(data, ToolExecutionStartData):
            tool_id = getattr(data, "id", "") or getattr(data, "tool_id", "")
            name = getattr(data, "tool_name", "") or getattr(data, "name", "")
            args = getattr(data, "arguments", None) or getattr(data, "input", None) or {}
            if isinstance(args, dict):
                self._tool_inputs[tool_id] = dict(args)
            self._tool_names[tool_id] = name
            self._schedule(cb.on_tool_update(
                tool_call_id=tool_id, title=name, status="in_progress",
                input=args if isinstance(args, dict) else None,
            ))
            return

        if isinstance(data, ToolExecutionCompleteData):
            tool_id = getattr(data, "id", "") or getattr(data, "tool_id", "")
            name = self._tool_names.pop(tool_id, "") or getattr(data, "tool_name", "")
            input_dict = self._tool_inputs.pop(tool_id, {})
            result = getattr(data, "result", None)
            result_text = self._stringify_result(result)
            self._schedule(cb.on_tool_call(
                name=name, input=input_dict, result=result_text, tool_id=tool_id,
            ))
            return

        if isinstance(data, SessionErrorData):
            msg = getattr(data, "message", "") or "session error"
            self._schedule(cb.on_error(msg))
            self.last_turn_failed = True
            self.last_turn_error = msg
            return

        if isinstance(data, SessionIdleData):
            self._turn_complete.set()
            return

    def _schedule(self, coro: Any) -> None:
        """Fire-and-forget an async callback from the SDK's sync event hook."""
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            # No running loop — drop. Shouldn't happen mid-send.
            coro.close()

    @staticmethod
    def _stringify_result(result: Any) -> str:
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        for attr in ("text", "content", "output"):
            v = getattr(result, attr, None)
            if isinstance(v, str):
                return v
        try:
            import json
            return json.dumps(result, default=str, ensure_ascii=False)
        except Exception:
            return str(result)
