"""ClaudeProcess — Claude CLI backend (claude --output-format stream-json)."""

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from boxagent.agent.base_cli import BaseCLIProcess
from boxagent.agent.callback import AgentCallback

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
    fork_session: bool = False

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

    def _build_args(self, message: str, model: str, chat_id: str) -> list[str]:
        args = [
            "claude",
            "--output-format", "stream-json",
            "--verbose",
        ]

        if self.yolo:
            args.append("--dangerously-skip-permissions")

        # When using copilot-api proxy, inject --settings to override
        # user's ~/.claude/settings.json env (which would otherwise
        # clobber our ANTHROPIC_BASE_URL).
        if self.copilot_api_port:
            from boxagent.copilot_api import copilot_env_for_backend
            settings_env = copilot_env_for_backend("claude-cli", self.copilot_api_port)
            if settings_env:
                args += [
                    "--setting-sources", "",
                    "--settings", json.dumps({"env": settings_env}),
                ]

        args += ["-p", message]

        if model:
            args += ["--model", model]
        if self.agent:
            args += ["--agent", self.agent]
        if self.session_id:
            args += ["--resume", self.session_id]
            if self.fork_session:
                args.append("--fork-session")
                self.fork_session = False  # only fork on the first turn

        # MCP server config for Telegram media tools
        if self.bot_token and chat_id:
            mcp_server_path = str(
                Path(__file__).parent.parent / "mcp_server.py"
            )
            mcp_config = json.dumps({"mcpServers": {
                "boxagent-telegram": {
                    "command": sys.executable,
                    "args": [mcp_server_path],
                    "env": {
                        "BOXAGENT_BOT_TOKEN": self.bot_token,
                        "BOXAGENT_CHAT_ID": chat_id,
                    },
                }
            }})
            args += ["--mcp-config", mcp_config]

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

        elif event_type == "content_block_stop":
            index = event.get("index", 0)
            if index in self._tool_names:
                raw = "".join(self._tool_inputs.get(index, []))
                try:
                    parsed_input = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    parsed_input = {}
                await callback.on_tool_call(
                    self._tool_names[index], parsed_input, ""
                )
                del self._tool_names[index]
                self._tool_inputs.pop(index, None)

        elif event_type == "result":
            if event.get("is_error"):
                detail = self._format_result_error(event)
                if detail:
                    self._record_turn_error_detail(detail)
                return
            if "session_id" in event:
                self.session_id = event["session_id"]
