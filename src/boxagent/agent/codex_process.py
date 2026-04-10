"""CodexProcess — Codex CLI backend (codex exec --json)."""

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from boxagent.agent.base_cli import BaseCLIProcess
from boxagent.agent.callback import AgentCallback

logger = logging.getLogger(__name__)

_MCP_SERVER_PATH = str(Path(__file__).parent.parent / "mcp_server.py")


def _toml_literal(value: str) -> str:
    """Return a TOML literal string.

    Uses single quotes so Windows backslashes are not treated as escape
    sequences by Codex's TOML parser.
    """
    return "'" + value.replace("'", "''") + "'"


def build_mcp_args(bot_token: str, chat_id: str) -> list[str]:
    """Build -c flags to inject Telegram MCP server for a Codex backend.

    Shared by CodexProcess and ACPProcess.
    """
    if not bot_token or not chat_id:
        return []
    python = sys.executable.replace('\\', '/')
    mcp_path = _MCP_SERVER_PATH.replace('\\', '/')
    args_toml = "[" + ",".join([
        _toml_literal(mcp_path),
        _toml_literal(bot_token),
        _toml_literal(chat_id),
    ]) + "]"
    return [
        "-c", f"mcp_servers.boxagent-telegram.command={_toml_literal(python)}",
        "-c", f"mcp_servers.boxagent-telegram.args={args_toml}",
        "-c", 'mcp_servers.boxagent-telegram.enabled=true',
    ]


@dataclass
class CodexProcess(BaseCLIProcess):
    """Codex CLI backend.

    Spawns ``codex exec --json <message>`` per turn.
    Session continuity via ``codex exec resume <thread_id>``.

    JSONL event types (Codex CLI 0.116+):
    - thread.started   → {thread_id}
    - turn.started
    - item.started     → command_execution / mcp_tool_call in progress
    - item.completed   → agent_message (text), command_execution, or mcp_tool_call
    - turn.completed   → {usage}
    """

    @property
    def _backend_label(self) -> str:
        return "Codex CLI"

    def _mcp_args(self, chat_id: str) -> list[str]:
        """Build -c flags to inject Telegram MCP server for this turn."""
        return build_mcp_args(self.bot_token, chat_id)

    def _extra_env(self, chat_id: str) -> dict[str, str] | None:
        """Environment variables for the MCP server subprocess."""
        if not self.bot_token or not chat_id:
            return None
        return {
            "BOXAGENT_BOT_TOKEN": self.bot_token,
            "BOXAGENT_CHAT_ID": chat_id,
        }

    def _build_args(self, message: str, model: str, chat_id: str) -> list[str]:
        copilot_args = []
        if self.copilot_api_port:
            from boxagent.copilot_api import copilot_args_for_codex
            copilot_args = copilot_args_for_codex(self.copilot_api_port)

        if self.session_id:
            # Resume: restricted flag set (no --color, --sandbox, -C)
            args = [
                "codex", "exec", "resume",
                "--json",
                "--skip-git-repo-check",
            ]
            if self.yolo:
                args.append("--dangerously-bypass-approvals-and-sandbox")
            args += self._mcp_args(chat_id)
            args += copilot_args
            if model:
                args += ["--model", model]
            args += [self.session_id, message]
        else:
            # Fresh session
            args = [
                "codex", "exec",
                "--json",
                "--color", "never",
                "--skip-git-repo-check",
                "-C", self.workspace,
            ]
            if self.yolo:
                args.append("--dangerously-bypass-approvals-and-sandbox")
            args += self._mcp_args(chat_id)
            args += copilot_args
            if model:
                args += ["--model", model]
            args.append(message)

        return args

    async def _parse_event(self, event: dict, callback: AgentCallback) -> None:
        event_type = event.get("type")

        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if thread_id:
                self.session_id = thread_id

        elif event_type == "item.completed":
            item = event.get("item", {})
            item_type = item.get("type")

            if item_type == "agent_message":
                text = item.get("text", "")
                if text:
                    await callback.on_stream(text)

            elif item_type == "command_execution":
                command = item.get("command", "")
                output = item.get("aggregated_output", "")
                exit_code = item.get("exit_code")
                await callback.on_tool_call(
                    "shell",
                    {"command": command},
                    f"exit={exit_code}\n{output}" if output else f"exit={exit_code}",
                )

            elif item_type == "mcp_tool_call":
                tool_name = item.get("tool", "")
                server = item.get("server", "")
                arguments = item.get("arguments", {})
                result = item.get("result")
                result_text = ""
                if isinstance(result, dict):
                    for block in result.get("content", []):
                        if isinstance(block, dict) and block.get("type") == "text":
                            result_text += block.get("text", "")
                display_name = f"{server}/{tool_name}" if server else tool_name
                await callback.on_tool_call(display_name, arguments, result_text)

        elif event_type == "item.started":
            item = event.get("item", {})
            item_type = item.get("type")
            if item_type == "command_execution":
                command = item.get("command", "")
                await callback.on_tool_update(
                    tool_call_id=item.get("id", ""),
                    title=f"$ {command}" if command else "shell",
                    status="in_progress",
                )
            elif item_type == "mcp_tool_call":
                tool_name = item.get("tool", "")
                server = item.get("server", "")
                display_name = f"{server}/{tool_name}" if server else tool_name
                await callback.on_tool_update(
                    tool_call_id=item.get("id", ""),
                    title=display_name,
                    status="in_progress",
                )
