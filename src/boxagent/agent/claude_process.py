"""ClaudeProcess — Claude CLI backend (claude --output-format stream-json)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from boxagent.agent.base_cli import BaseCLIProcess
from boxagent.agent.callback import AgentCallback

if TYPE_CHECKING:
    from boxagent.agent_env import AgentEnv

logger = logging.getLogger(__name__)


@dataclass
class ClaudeProcess(BaseCLIProcess):
    """Claude CLI backend.

    Spawns ``claude --output-format stream-json -p <message>`` per turn.
    Session continuity via ``--resume <session_id>``.

    Handles two output formats:
    - Batch: ``{"type": "assistant", "message": {"content": [...]}}``
    - Streaming: ``content_block_start/delta/stop`` events
    """

    # Internal state for streaming tool input accumulation
    _tool_inputs: dict[int, list[str]] = field(default_factory=dict, repr=False)
    _tool_names: dict[int, str] = field(default_factory=dict, repr=False)
    _tool_ids: dict[int, str] = field(default_factory=dict, repr=False)
    fork_session: bool = False
    supports_fork: bool = field(default=True, init=False, repr=False)

    async def fork_and_send(
        self, source_session_id, message, callback,
        *, model="", env=None,
    ) -> str:
        """Spawn a one-shot Claude subprocess that forks ``source_session_id``.

        Uses a separate ClaudeProcess instance so this Claude's session_id
        isn't mutated by the fork's --fork-session result. The fork inherits
        the source's transcript but its turn doesn't write back into the
        source.
        """
        fork_proc = ClaudeProcess(
            workspace=self.workspace,
            session_id=source_session_id,
            model=self.model,
            agent=self.agent,
            bot_name=self.bot_name,
            yolo=self.yolo,
            fork_session=True,
        )
        fork_proc.start()
        try:
            await fork_proc.send(message, callback, model=model, env=env)
            return fork_proc.session_id or ""
        finally:
            await fork_proc.stop()

    def _format_result_error(self, event: dict) -> str:
        parts: list[str] = []
        subtype = event.get("subtype")
        if isinstance(subtype, str) and subtype:
            parts.append(subtype.replace("_", " "))

        errors = event.get("errors")
        if isinstance(errors, list):
            for item in errors:
                if isinstance(item, str) and item.strip():
                    parts.append(item.strip())
        elif isinstance(errors, str) and errors.strip():
            parts.append(errors.strip())

        result = event.get("result")
        if isinstance(result, str) and result.strip():
            parts.append(result.strip())

        return ": ".join(parts)

    @property
    def _backend_label(self) -> str:
        return "Claude CLI"

    def _build_args(self, message: str, model: str, chat_id: str, append_system_prompt: str = "", env: AgentEnv | None = None) -> list[str]:
        args = [
            "claude",
            "--output-format", "stream-json",
            "--verbose",
        ]

        if self.yolo:
            args.append("--dangerously-skip-permissions")

        if append_system_prompt:
            args += ["--append-system-prompt", append_system_prompt]

        if model:
            args += ["--model", model]
        if self.agent:
            args += ["--agent", self.agent]
        if self.session_id:
            args += ["--resume", self.session_id]
            if self.fork_session:
                args.append("--fork-session")
                self.fork_session = False  # only fork on the first turn

        # MCP servers — HTTP endpoints, selected by bot capabilities.
        # passthrough bots (raw) skip MCP injection so the backend behaves
        # identically to running ``claude --resume`` from a terminal.
        if env is None:
            from boxagent.agent_env import AgentEnv as _AE
            env = _AE(bot_name=self.bot_name)

        from boxagent.agent.mcp_endpoints import pick_mcp_endpoints
        endpoints = pick_mcp_endpoints(env, chat_id)
        if endpoints:
            mcp_servers = {
                endpoint["name"]: {
                    "type": "http",
                    "url": endpoint["url"],
                    "headers": endpoint["headers"],
                }
                for endpoint in endpoints
            }
            args += ["--mcp-config", json.dumps({"mcpServers": mcp_servers})]

        # -p (print mode) is a boolean flag; message is a positional arg.
        # Use "--" to stop option parsing so messages starting with "-"
        # are not mistaken for unknown CLI options by Commander.js.
        args += ["-p", "--", message]

        return args

    async def _parse_event(self, event: dict, callback: AgentCallback) -> None:
        event_type = event.get("type")

        # --- Batch message format ---
        if event_type == "assistant":
            msg = event.get("message", {})
            for block in msg.get("content", []):
                block_type = block.get("type")
                if block_type == "text":
                    await callback.on_stream(block["text"])
                elif block_type == "tool_use":
                    await callback.on_tool_call(
                        block.get("name", ""),
                        block.get("input", {}),
                        "",
                        tool_id=block.get("id", ""),
                    )
            if "session_id" in event:
                self.session_id = event["session_id"]

        # --- Streaming format ---
        elif event_type == "content_block_delta":
            delta = event.get("delta", {})
            delta_type = delta.get("type")
            index = event.get("index", 0)

            if delta_type == "text_delta":
                await callback.on_stream(delta["text"])
            elif delta_type == "input_json_delta":
                if index not in self._tool_inputs:
                    self._tool_inputs[index] = []
                self._tool_inputs[index].append(delta.get("partial_json", ""))

        elif event_type == "content_block_start":
            block = event.get("content_block", {})
            index = event.get("index", 0)
            if block.get("type") == "tool_use":
                self._tool_names[index] = block.get("name", "")
                self._tool_inputs[index] = []
                self._tool_ids[index] = block.get("id", "")

        elif event_type == "content_block_stop":
            index = event.get("index", 0)
            if index in self._tool_names:
                raw = "".join(self._tool_inputs.get(index, []))
                try:
                    parsed_input = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    parsed_input = {}
                await callback.on_tool_call(
                    self._tool_names[index], parsed_input, "",
                    tool_id=self._tool_ids.get(index, ""),
                )
                del self._tool_names[index]
                self._tool_inputs.pop(index, None)
                self._tool_ids.pop(index, None)

        elif event_type == "user":
            # Tool-result feedback: Claude streams a `user` message after
            # each tool runs, carrying tool_result blocks keyed by
            # tool_use_id. Without this the web UI's tool_call card stays
            # stuck "in progress" forever (yait #14 / #25).
            msg = event.get("message", {}) or {}
            blocks = msg.get("content")
            if isinstance(blocks, list):
                for block in blocks:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    tool_use_id = block.get("tool_use_id", "") or ""
                    raw_content = block.get("content")
                    if isinstance(raw_content, list):
                        # Newer schema: list of {type:"text", text:...} blocks.
                        output = "".join(
                            (b.get("text", "") or "")
                            for b in raw_content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    else:
                        output = str(raw_content) if raw_content is not None else ""
                    is_error = bool(block.get("is_error"))
                    await callback.on_tool_update(
                        tool_call_id=tool_use_id,
                        title="",  # already shown via on_tool_call
                        status="failed" if is_error else "completed",
                        output=output,
                    )

        elif event_type == "result":
            if event.get("is_error"):
                detail = self._format_result_error(event)
                if detail:
                    self._record_turn_error_detail(detail)
                return
            if "session_id" in event:
                self.session_id = event["session_id"]
