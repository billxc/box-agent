"""ACPProcess — communicates with coding agents via ACP protocol."""

import asyncio
import json
import logging
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any, Literal

from acp import (
    Client,
    PROTOCOL_VERSION,
    RequestError,
    SessionNotification,
    spawn_agent_process,
    text_block,
)
from acp.interfaces import param_model
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    AllowedOutcome,
    Implementation,
    PermissionOption,
    RequestPermissionRequest,
    RequestPermissionResponse,
    TextContentBlock,
    ToolCall,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UsageUpdate,
)

from boxagent.agent.callback import AgentCallback
from boxagent.agent.codex_process import build_mcp_args

logger = logging.getLogger(__name__)

DEFAULT_ACP_COMMAND = "codex-acp"
ACP_STDIO_LIMIT = 8 * 1024 * 1024


def _resolve_command(cmd: str) -> str:
    """Resolve a command name to its full path.

    On Windows, subprocess cannot run .cmd/.ps1 shims directly via
    create_subprocess_exec. Use shutil.which to find the real path.
    """
    if sys.platform == "win32":
        resolved = shutil.which(cmd)
        if resolved:
            return resolved
    return cmd


class _BoxAgentACPClient(Client):
    """ACP client implementation that bridges ACP events to AgentCallback."""

    def __init__(self) -> None:
        self._callback: AgentCallback | None = None
        self._tool_titles: dict[str, str] = {}
        self._streamed_any = False
        self._update_lock = asyncio.Lock()
        self._pending_updates = 0
        self._updates_drained = asyncio.Event()
        self._updates_drained.set()
        self._suppress_updates = False

    def set_callback(self, cb: AgentCallback | None) -> None:
        self._callback = cb
        self._streamed_any = False
        self._pending_updates = 0
        self._updates_drained.set()

    def _remember_tool_title(
        self, tool_call_id: str, title: str | None
    ) -> None:
        if title and not title.startswith("tool:"):
            self._tool_titles[tool_call_id] = title

    def _display_tool_title(
        self, tool_call_id: str, title: str | None
    ) -> str:
        self._remember_tool_title(tool_call_id, title)
        if title and not title.startswith("tool:"):
            return title
        return self._tool_titles.get(tool_call_id, title or f"tool:{tool_call_id}")

    @param_model(RequestPermissionRequest)
    async def request_permission(
        self,
        options: list[PermissionOption],
        session_id: str,
        tool_call: ToolCallUpdate,
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        """Auto-approve ACP permission requests."""
        title = self._display_tool_title(
            tool_call.tool_call_id, tool_call.title or "unknown"
        )
        if self._callback:
            await self._callback.on_tool_update(
                tool_call_id=tool_call.tool_call_id,
                title=title,
                status="pending",
                input=getattr(tool_call, "raw_input", None),
                output=None,
            )

        option_ids = [getattr(opt, "option_id", None) for opt in options]
        if "approved-execpolicy-amendment" in option_ids:
            option_id = "approved-execpolicy-amendment"
        elif "approved" in option_ids:
            option_id = "approved"
        else:
            option_id = options[0].option_id if options else "allow"

        return RequestPermissionResponse(
            outcome=AllowedOutcome(option_id=option_id, outcome="selected")
        )

    async def drain_updates(self) -> None:
        while True:
            await self._updates_drained.wait()
            await asyncio.sleep(0)
            if self._pending_updates == 0:
                return

    @param_model(SessionNotification)
    async def session_update(
        self, session_id: str, update: Any, **kwargs: Any
    ) -> None:
        self._pending_updates += 1
        self._updates_drained.clear()
        try:
            async with self._update_lock:
                if self._suppress_updates:
                    return
                cb = self._callback
                if not cb:
                    return

                if isinstance(update, AgentMessageChunk):
                    content = update.content
                    if isinstance(content, TextContentBlock) and content.text:
                        self._streamed_any = True
                        await cb.on_stream(content.text)
                elif isinstance(update, AgentThoughtChunk):
                    content = update.content
                    if isinstance(content, TextContentBlock) and content.text:
                        await cb.on_stream(f"💭 {content.text}")
                elif isinstance(update, ToolCallStart):
                    await cb.on_tool_update(
                        tool_call_id=update.tool_call_id,
                        title=self._display_tool_title(
                            update.tool_call_id, update.title or "tool"
                        ),
                        status=(update.status or "in_progress"),
                        input=getattr(update, "raw_input", None),
                        output=getattr(update, "raw_output", None),
                    )
                elif isinstance(update, ToolCallProgress):
                    await cb.on_tool_update(
                        tool_call_id=update.tool_call_id,
                        title=self._display_tool_title(
                            update.tool_call_id, update.title
                        ),
                        status=update.status,
                        input=getattr(update, "raw_input", None),
                        output=getattr(update, "raw_output", None),
                    )
                elif isinstance(update, UsageUpdate):
                    logger.debug("ACP usage update: %s", update)
                else:
                    logger.debug("ACP update: %s", type(update).__name__)
        finally:
            self._pending_updates -= 1
            if self._pending_updates == 0:
                self._updates_drained.set()

    async def write_text_file(self, **kw: Any) -> Any:
        raise RequestError.method_not_found("fs/write_text_file")

    async def read_text_file(self, **kw: Any) -> Any:
        raise RequestError.method_not_found("fs/read_text_file")

    async def create_terminal(self, **kw: Any) -> Any:
        raise RequestError.method_not_found("terminal/create")

    async def terminal_output(self, **kw: Any) -> Any:
        raise RequestError.method_not_found("terminal/output")

    async def release_terminal(self, **kw: Any) -> Any:
        raise RequestError.method_not_found("terminal/release")

    async def wait_for_terminal_exit(self, **kw: Any) -> Any:
        raise RequestError.method_not_found("terminal/wait_for_exit")

    async def kill_terminal(self, **kw: Any) -> Any:
        raise RequestError.method_not_found("terminal/kill")

    async def ext_method(self, **kw: Any) -> Any:
        raise RequestError.method_not_found("ext")

    async def ext_notification(self, **kw: Any) -> None:
        pass


@dataclass
class ACPProcess:
    """Manages a coding agent via ACP protocol."""

    workspace: str
    session_id: str | None = None
    model: str = ""
    agent: str = ""
    acp_command: str = DEFAULT_ACP_COMMAND
    state: Literal["idle", "busy", "dead"] = "idle"
    supports_session_persistence: bool = field(
        default=True, init=False, repr=False
    )

    _client: _BoxAgentACPClient = field(
        default_factory=_BoxAgentACPClient, repr=False
    )
    _conn: Any = field(default=None, repr=False)
    _proc: Any = field(default=None, repr=False)
    _acp_session_id: str | None = field(default=None, repr=False)
    _ctx_manager: Any = field(default=None, repr=False)

    _cancelled: bool = field(default=False, repr=False)
    _cancel_requested: asyncio.Event = field(
        default_factory=asyncio.Event, repr=False
    )
    _idle_event: asyncio.Event = field(
        default_factory=asyncio.Event, repr=False
    )
    _queue: asyncio.Queue = field(default_factory=asyncio.Queue, repr=False)
    _queue_task: asyncio.Task | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._idle_event.set()

    def start(self) -> None:
        self._queue_task = asyncio.create_task(self._process_queue())

    async def send(
        self,
        message: str,
        callback: AgentCallback,
        model: str = "",
        chat_id: str = "",
        append_system_prompt: str = "",
        env=None,
    ) -> None:
        done = asyncio.Event()
        await self._queue.put((message, callback, done, model, chat_id, append_system_prompt, env))
        await done.wait()

    async def wait_idle(self) -> None:
        await self._idle_event.wait()

    async def drain_output(self) -> None:
        await self._client.drain_updates()

    async def cancel(self) -> None:
        if self.state != "busy":
            return

        self._cancelled = True
        self._cancel_requested.set()

        if self._conn is not None and self._acp_session_id is not None:
            try:
                await self._conn.cancel(session_id=self._acp_session_id)
            except Exception:
                logger.warning(
                    "ACP cancel failed; disconnecting transport",
                    exc_info=True,
                )
                await self._disconnect()

    async def reset_session(self) -> None:
        await self.cancel()
        await self._disconnect()
        self._acp_session_id = None
        self.session_id = None

    async def stop(self) -> None:
        if self._queue_task:
            self._queue_task.cancel()
            self._queue_task = None
        await self._disconnect()
        self.state = "dead"

    async def _ensure_connected(self, chat_id: str = "", env=None) -> None:
        if self._conn is not None:
            return

        # Build extra args for MCP injection
        token = env.telegram_token if env else ""
        extra_args = build_mcp_args(token, chat_id)
        extra_env: dict[str, str] | None = None
        if token and chat_id:
            import os
            extra_env = {
                **os.environ,
                "BOXAGENT_BOT_TOKEN": token,
                "BOXAGENT_CHAT_ID": chat_id,
            }

        self._ctx_manager = spawn_agent_process(
            self._client,
            _resolve_command(self.acp_command),
            *extra_args,
            cwd=self.workspace,
            env=extra_env,
            transport_kwargs={"limit": ACP_STDIO_LIMIT},
        )
        self._conn, self._proc = await self._ctx_manager.__aenter__()

        init_resp = await self._conn.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_info=Implementation(name="boxagent", version="0.1.0"),
        )
        logger.info("ACP initialized, protocol=%s", init_resp.protocol_version)

        self._client._tool_titles.clear()
        target_session_id = self.session_id
        if target_session_id:
            # Suppress session_update notifications during load_session to
            # prevent replaying the entire conversation history to Telegram.
            # A flag is used instead of swapping the callback because the ACP
            # server may deliver history-replay notifications *after* the
            # load_session response returns — a callback swap would restore
            # the real callback too early.
            self._client._suppress_updates = True
            try:
                await self._conn.load_session(
                    session_id=target_session_id,
                    cwd=self.workspace,
                )
                self._acp_session_id = target_session_id
                self.session_id = target_session_id
                logger.info("ACP session loaded: %s", target_session_id)

                # Drain any in-flight session_update notifications that were
                # already dispatched, then yield a few times so notifications
                # still in the transport buffer get processed and dropped.
                await self._client.drain_updates()
                for _ in range(5):
                    await asyncio.sleep(0)
                await self._client.drain_updates()
            except Exception:
                logger.warning(
                    "ACP load_session failed for %s; falling back to new_session",
                    target_session_id,
                    exc_info=True,
                )
            finally:
                self._client._suppress_updates = False

            if self._acp_session_id:
                return

        session_resp = await self._conn.new_session(cwd=self.workspace)
        self._acp_session_id = session_resp.session_id
        self.session_id = session_resp.session_id

    async def _disconnect(self) -> None:
        if self._ctx_manager is not None:
            try:
                await self._ctx_manager.__aexit__(None, None, None)
            except Exception:
                logger.debug("ACP disconnect error", exc_info=True)
        self._conn = None
        self._proc = None
        self._ctx_manager = None

    async def _process_queue(self) -> None:
        while True:
            try:
                message, callback, done, model, chat_id, append_system_prompt, env = await self._queue.get()
            except asyncio.CancelledError:
                return

            self._idle_event.clear()
            self.state = "busy"
            self._cancelled = False
            self._cancel_requested = asyncio.Event()
            self._client.set_callback(callback)

            # Inject system-level context via Codex's developer_instructions config
            if append_system_prompt:
                try:
                    await self._conn.set_config_option(
                        config_id="developer_instructions",
                        session_id=self._acp_session_id,
                        value=append_system_prompt,
                    )
                except Exception:
                    # Fallback: prepend to user message if config option unsupported
                    logger.debug("ACP set_config_option failed; prepending to message", exc_info=True)
                    message = f"{append_system_prompt}\n{message}"

            stop_reason = None
            try:
                await self._ensure_connected(chat_id=chat_id, env=env)
                response = await self._conn.prompt(
                    session_id=self._acp_session_id,
                    prompt=[text_block(message)],
                )
                stop_reason = getattr(response, "stop_reason", None)
                if response and not self._client._streamed_any:
                    for block in getattr(response, "content", []) or []:
                        if isinstance(block, TextContentBlock) and block.text:
                            await callback.on_stream(block.text)
                await self._client.drain_updates()
                logger.info(
                    "ACP turn complete: session=%s stop_reason=%s streamed_any=%s",
                    self._acp_session_id,
                    stop_reason,
                    self._client._streamed_any,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._cancelled:
                    await callback.on_error(f"ACP error: {exc}")
                await self._disconnect()
            finally:
                self._client.set_callback(None)
                self.state = "idle"
                self._idle_event.set()
                done.set()
