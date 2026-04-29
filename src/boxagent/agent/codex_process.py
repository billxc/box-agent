"""CodexProcess — Codex CLI backend (codex exec --json)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from boxagent.agent.base_cli import BaseCLIProcess
from boxagent.agent.callback import AgentCallback

logger = logging.getLogger(__name__)


@dataclass
class CodexProcess(BaseCLIProcess):
    """Codex CLI backend.

    Spawns ``codex exec --json -`` per turn, piping the prompt via stdin
    to avoid the "Reading additional input from stdin" warning.
    Session continuity via ``codex exec resume <thread_id>``.

    JSONL event types (Codex CLI 0.116+):
    - thread.started   → {thread_id}
    - turn.started
    - item.started     → command_execution / mcp_tool_call in progress
    - item.completed   → agent_message (text), command_execution, or mcp_tool_call
    - turn.completed   → {usage}
    """

    def _stdin_input(self, message: str) -> str | None:
        """Pipe the prompt via stdin (``codex exec -`` mode)."""
        return message

    @property
    def _backend_label(self) -> str:
        return "Codex CLI"

    def _mcp_args(self, chat_id: str, env=None) -> list[str]:
        """MCP args — currently no-op (MCP is served via HTTP by Gateway)."""
        return []

    def _extra_env(self, chat_id: str, env=None) -> dict[str, str] | None:
        """Environment variables for the subprocess."""
        return None

    def _build_args(self, message: str, model: str, chat_id: str, append_system_prompt: str = "", env=None) -> list[str]:
        # Inject system-level context via Codex's developer_instructions config
        dev_instr_args: list[str] = []
        if append_system_prompt:
            escaped = (append_system_prompt
                       .replace('\\', '\\\\')
                       .replace('"', '\\"')
                       .replace('\n', '\\n')
                       .replace('\r', '\\r'))
            dev_instr_args = ["-c", f'developer_instructions="{escaped}"']

        if self.session_id:
            # Resume: restricted flag set (no --color, --sandbox, -C)
            args = [
                "codex", "exec", "resume",
                "--json",
                "--skip-git-repo-check",
            ]
            if self.yolo:
                args.append("--dangerously-bypass-approvals-and-sandbox")
            args += self._mcp_args(chat_id, env=env)
            args += dev_instr_args
            if model:
                args += ["--model", model]
            args += [self.session_id, "-"]
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
            args += self._mcp_args(chat_id, env=env)
            args += dev_instr_args
            if model:
                args += ["--model", model]
            args.append("-")

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
