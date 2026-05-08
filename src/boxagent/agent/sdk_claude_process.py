"""AgentSDKClaude — Claude backend via the official ``claude-agent-sdk``.

This is an alternative to ``ClaudeProcess`` (which spawns the ``claude``
CLI per turn): we call ``claude_agent_sdk.query()`` directly so each turn
runs in-process — no subprocess fork-per-message, fewer startup costs,
typed message stream.

We satisfy ``AgentBackend`` so this drops into Router / Watchdog /
SessionPool exactly like the CLI backends.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import (
    AssistantMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from boxagent.agent.callback import AgentCallback
from boxagent.agent.protocol import AgentBackend

if TYPE_CHECKING:
    from boxagent.agent_env import AgentEnv

logger = logging.getLogger(__name__)


@dataclass
class AgentSDKClaude(AgentBackend):
    """Claude backend powered by ``claude-agent-sdk-python``.

    ``send`` runs one ``query()`` per turn; the async iterator of messages
    is consumed and translated into ``AgentCallback`` events. Session
    continuity is via ``options.resume = self.session_id``.
    """

    workspace: str = ""
    session_id: str | None = None
    model: str = ""
    agent: str = ""
    bot_name: str = ""
    yolo: bool = False
    state: Literal["idle", "busy", "dead"] = "idle"
    supports_session_persistence: bool = field(default=True, init=False, repr=False)
    last_turn_failed: bool = field(default=False, init=False)
    last_turn_error: str = field(default="", init=False)

    _idle_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _current_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _cancelled: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._idle_event.set()

    # ── Lifecycle ──

    def start(self) -> None:
        # No subprocess to start — the SDK invokes the CLI per-turn under
        # the hood, but we don't manage it. This stays a no-op for parity
        # with other backends' lifecycle.
        pass

    async def stop(self) -> None:
        await self.cancel()
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
        self.state = "busy"
        self._idle_event.clear()
        self._cancelled = False
        self.last_turn_failed = False
        self.last_turn_error = ""
        self._current_task = asyncio.current_task()

        effective_model = model or self.model or None
        options = self._build_options(
            effective_model, append_system_prompt,
            chat_id=chat_id, env=env,
        )

        # ToolUseBlock arrives before ToolResultBlock — buffer name+input
        # so we can emit a single on_tool_call(call+result) once the
        # result lands.
        tool_names: dict[str, str] = {}
        tool_inputs: dict[str, dict] = {}

        try:
            async for msg in query(prompt=message, options=options):
                if self._cancelled:
                    break

                if isinstance(msg, AssistantMessage):
                    if msg.session_id:
                        self.session_id = msg.session_id
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            await callback.on_stream(block.text)
                        elif isinstance(block, ToolUseBlock):
                            tool_names[block.id] = block.name
                            tool_inputs[block.id] = dict(block.input)
                            # Surface lifecycle "started" so channels that
                            # render rich tool progress (Telegram summary,
                            # Web cards) can show pending state.
                            await callback.on_tool_update(
                                tool_call_id=block.id,
                                title=block.name,
                                status="in_progress",
                                input=block.input,
                            )

                elif isinstance(msg, UserMessage):
                    # The SDK echoes tool_result blocks back as UserMessage
                    # content (mirroring the API's pairing convention).
                    if isinstance(msg.content, list):
                        for block in msg.content:
                            if isinstance(block, ToolResultBlock):
                                tool_id = block.tool_use_id
                                name = tool_names.pop(tool_id, "")
                                input_dict = tool_inputs.pop(tool_id, {})
                                result_text = self._stringify_tool_result(block.content)
                                await callback.on_tool_call(
                                    name=name,
                                    input=input_dict,
                                    result=result_text,
                                    tool_id=tool_id,
                                )

                elif isinstance(msg, SystemMessage):
                    # subtype="error" / "warning" surface inline; everything
                    # else (init, mcp_tool_listing, etc.) is metadata we
                    # don't need to forward.
                    if msg.subtype in ("error", "warning"):
                        text = str(msg.data.get("message", "")) or msg.subtype
                        if msg.subtype == "error":
                            await callback.on_error(text)

        except asyncio.CancelledError:
            self._cancelled = True
            # Re-raise so the caller (Router) sees the cancellation; the
            # finally block still runs to reset state.
            raise
        except Exception as e:
            self.last_turn_failed = True
            self.last_turn_error = f"Turn failed: {e}"
            logger.exception("AgentSDKClaude turn failed")
            try:
                await callback.on_error(self.last_turn_error)
            except Exception:
                pass
        finally:
            self._current_task = None
            self.state = "idle"
            self._idle_event.set()

    async def cancel(self) -> None:
        self._cancelled = True
        task = self._current_task
        if task is not None and not task.done():
            task.cancel()
        self.state = "idle"
        self._idle_event.set()

    async def reset_session(self) -> None:
        await self.cancel()
        self.session_id = None

    async def wait_idle(self) -> None:
        await self._idle_event.wait()

    # ── Helpers ──

    def _build_options(
        self,
        model: str | None,
        append_system_prompt: str,
        *,
        chat_id: str = "",
        env: "AgentEnv | None" = None,
    ) -> ClaudeAgentOptions:
        # Trigger tool registration side-effect so adapters can see them.
        import boxagent.tools.builtin  # noqa: F401
        from boxagent.tools import ToolContext
        from boxagent.tools.adapters.claude_sdk import build_mcp_servers

        ctx = ToolContext(bot_name=self.bot_name, chat_id=chat_id)
        mcp_servers = build_mcp_servers(ctx=ctx, env=env) if env is not None else {}

        opts = ClaudeAgentOptions(
            cwd=self.workspace or None,
            model=model,
            resume=self.session_id,
            mcp_servers=mcp_servers,
        )
        if self.yolo:
            opts.permission_mode = "bypassPermissions"
        if append_system_prompt:
            opts.system_prompt = {
                "type": "preset",
                "preset": "claude_code",
                "append": append_system_prompt,
            }
        return opts

    @staticmethod
    def _stringify_tool_result(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # API returns a list of content blocks (typically [{type:text, text:...}])
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                        continue
                parts.append(json.dumps(item, ensure_ascii=False))
            return "\n".join(parts)
        return json.dumps(content, ensure_ascii=False)
