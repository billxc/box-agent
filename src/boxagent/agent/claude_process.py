"""ClaudeProcess — Claude CLI backend (claude --output-format stream-json)."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
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

        # MCP servers
        mcp_pkg = Path(__file__).parent.parent
        mcp_servers = {}

        # Resolve MCP parameters — prefer env, fall back to instance attrs
        if env is not None:
            mcp_bot_name = env.bot_name
            mcp_is_admin = env.is_workgroup_admin
            mcp_telegram_token = env.telegram_token
            mcp_server_names = env.mcp_server_names() if chat_id else []
        else:
            mcp_bot_name = self.bot_name
            mcp_is_admin = self.is_workgroup_admin
            mcp_telegram_token = self.bot_token
            mcp_server_names = []
            if chat_id:
                mcp_server_names.append("boxagent")
                if mcp_is_admin:
                    mcp_server_names.append("boxagent-admin")
                if mcp_telegram_token:
                    mcp_server_names.append("boxagent-telegram")

        if "boxagent" in mcp_server_names:
            agent_env = {"BOXAGENT_BOT_NAME": mcp_bot_name}
            for key in ("BOXAGENT_CONFIG_DIR", "BOXAGENT_LOCAL_DIR", "BOXAGENT_NODE_ID"):
                val = os.environ.get(key, "")
                if val:
                    agent_env[key] = val
            mcp_servers["boxagent"] = {
                "command": sys.executable,
                "args": [str(mcp_pkg / "mcp_server.py")],
                "env": agent_env,
            }

        if "boxagent-admin" in mcp_server_names:
            admin_env = {
                "BOXAGENT_BOT_NAME": mcp_bot_name,
                "BOXAGENT_CHAT_ID": chat_id,
            }
            local_dir = os.environ.get("BOXAGENT_LOCAL_DIR", "")
            if local_dir:
                admin_env["BOXAGENT_LOCAL_DIR"] = local_dir
            mcp_servers["boxagent-admin"] = {
                "command": sys.executable,
                "args": [str(mcp_pkg / "workgroup" / "mcp_admin.py")],
                "env": admin_env,
            }

        if "boxagent-telegram" in mcp_server_names:
            mcp_servers["boxagent-telegram"] = {
                "command": sys.executable,
                "args": [str(mcp_pkg / "mcp_telegram.py")],
                "env": {
                    "BOXAGENT_BOT_TOKEN": mcp_telegram_token,
                    "BOXAGENT_CHAT_ID": chat_id,
                },
            }

        if mcp_servers:
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
