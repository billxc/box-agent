"""BaseCLIProcess — shared subprocess-per-turn infrastructure."""

import asyncio
import json
import logging
import os
import shutil
import signal
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Literal

from boxagent.agent.callback import AgentCallback

logger = logging.getLogger(__name__)


@dataclass
class BaseCLIProcess:
    """Base class for CLI-based AI backends.

    Provides the serial message queue, state machine, cancel/reset/stop
    lifecycle, and subprocess management. Subclasses implement:

    - ``_build_args()`` — construct the CLI command for a turn
    - ``_parse_event()`` — interpret one parsed JSON event from stdout
    - ``_on_thread_id()`` — extract and store the session/thread id
    """

    workspace: str
    session_id: str | None = None
    model: str = ""
    agent: str = ""
    bot_token: str = ""
    copilot_api_port: int = 0
    yolo: bool = False
    state: Literal["idle", "busy", "dead"] = "idle"
    supports_session_persistence: bool = field(
        default=True, init=False, repr=False
    )
    _process: asyncio.subprocess.Process | None = field(
        default=None, repr=False
    )
    _cancelled: bool = field(default=False, repr=False)
    _idle_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _queue: asyncio.Queue = field(default_factory=asyncio.Queue, repr=False)
    _queue_task: asyncio.Task | None = field(default=None, repr=False)
    last_turn_failed: bool = field(default=False, init=False, repr=False)
    last_turn_error: str = field(default="", init=False, repr=False)
    _turn_error_detail: str = field(default="", init=False, repr=False)

    def __post_init__(self):
        self._idle_event.set()

    def start(self):
        """Start the message processing loop."""
        self._queue_task = asyncio.create_task(self._process_queue())

    async def send(self, message: str, callback: AgentCallback, model: str = "", chat_id: str = "", append_system_prompt: str = ""):
        """Enqueue a message. Returns when the turn completes."""
        done = asyncio.Event()
        await self._queue.put((message, callback, done, model, chat_id, append_system_prompt))
        await done.wait()

    async def wait_idle(self):
        """Wait until no turn is in progress."""
        await self._idle_event.wait()

    async def cancel(self):
        """Cancel the current turn by killing the subprocess tree."""
        self._cancelled = True
        if self._process and self._process.returncode is None:
            pid = self._process.pid
            if sys.platform == "win32":
                # On Windows, terminate()/kill() only kills the direct process,
                # leaving child processes (e.g. codex.exe under node.exe) alive.
                # Use taskkill /T /F to kill the entire process tree.
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "taskkill", "/PID", str(pid), "/T", "/F",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except Exception:
                    # Fallback to regular kill
                    self._process.kill()
            else:
                # Unix: kill the entire process group (created by start_new_session)
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    self._process.terminate()
            try:
                await asyncio.wait_for(self._wait_process(), timeout=3.0)
            except asyncio.TimeoutError:
                if sys.platform == "win32":
                    self._process.kill()
                else:
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        self._process.kill()
        self.state = "idle"
        self._idle_event.set()

    async def reset_session(self):
        """Cancel any active turn and drop session continuity."""
        await self.cancel()
        self.session_id = None

    async def stop(self):
        """Graceful shutdown."""
        await self.cancel()
        if self._queue_task:
            self._queue_task.cancel()
            try:
                await self._queue_task
            except asyncio.CancelledError:
                pass
        self.state = "dead"

    async def _wait_process(self):
        if self._process:
            await self._process.wait()

    async def _process_queue(self):
        """Consume messages serially, spawning a process per turn."""
        while True:
            try:
                message, callback, done, model_override, chat_id, append_system_prompt = await self._queue.get()
            except asyncio.CancelledError:
                return

            self._idle_event.clear()
            self.state = "busy"
            self._cancelled = False
            self.last_turn_failed = False
            self.last_turn_error = ""
            self._turn_error_detail = ""

            try:
                await self._execute_turn(message, callback, model_override, chat_id, append_system_prompt)
            except Exception as e:
                self.last_turn_failed = True
                self.last_turn_error = f"Turn failed: {e}"
                if not self._cancelled:
                    await callback.on_error(self.last_turn_error)
                logger.exception("Error during turn execution")
            finally:
                self.state = "idle"
                self._process = None
                self._idle_event.set()
                done.set()

    # --- Subclass hooks ---

    def _build_args(self, message: str, model: str, chat_id: str, append_system_prompt: str = "") -> list[str]:
        """Return the full argv list for this turn. Must be overridden."""
        raise NotImplementedError

    async def _parse_event(self, event: dict, callback: AgentCallback) -> None:
        """Handle one parsed JSON event from stdout. Must be overridden."""
        raise NotImplementedError

    @property
    def _backend_label(self) -> str:
        """Short name for log messages."""
        return "cli"

    def _extra_env(self, chat_id: str) -> dict[str, str] | None:
        """Extra environment variables for the subprocess. Override in subclass."""
        return None

    def _record_turn_error_detail(self, detail: str) -> None:
        cleaned = detail.strip()
        if not cleaned:
            return
        if not self._turn_error_detail:
            self._turn_error_detail = cleaned
            return
        if cleaned not in self._turn_error_detail:
            self._turn_error_detail = f"{self._turn_error_detail}\n{cleaned}"

    @staticmethod
    def _resolve_windows_node_shim(resolved: str) -> list[str] | None:
        """Bypass npm .CMD shims for known Node CLIs on Windows.

        npm-generated batch shims forward args via ``%*``. Multi-line prompt
        arguments get truncated at the first newline, which breaks BoxAgent's
        injected context blocks. Resolve known shims to their underlying
        ``node <cli.js>`` entrypoint instead.
        """
        path = Path(resolved)
        name = path.name.lower()

        rel_cli_parts: tuple[str, ...] | None = None
        if name == "claude.cmd":
            rel_cli_parts = ("node_modules", "@anthropic-ai", "claude-code", "cli.js")
        elif name == "codex.cmd":
            rel_cli_parts = ("node_modules", "@openai", "codex", "bin", "codex.js")
        else:
            return None

        cli_js = path.parent.joinpath(*rel_cli_parts)
        if not cli_js.exists():
            return None

        node_exe = path.parent / "node.exe"
        if node_exe.exists():
            node = str(node_exe)
        else:
            node = shutil.which("node") or "node"

        return [node, str(cli_js)]

    @staticmethod
    def _resolve_args(args: list[str]) -> list[str]:
        """Resolve the command in args[0] to a full path on Windows.

        For npm Node CLIs, avoid ``.CMD`` shims and invoke ``node cli.js``
        directly so multi-line prompt arguments survive intact.
        """
        if sys.platform == "win32" and args:
            resolved = shutil.which(args[0])
            if resolved:
                shim_args = BaseCLIProcess._resolve_windows_node_shim(resolved)
                if shim_args:
                    return shim_args + args[1:]
                return [resolved] + args[1:]
        return args

    # --- Shared execution ---

    async def _execute_turn(self, message: str, callback: AgentCallback, model_override: str = "", chat_id: str = "", append_system_prompt: str = ""):
        """Spawn a CLI process for one turn, stream-parse JSONL/NDJSON output."""
        effective_model = model_override or self.model
        args = self._build_args(message, effective_model, chat_id, append_system_prompt=append_system_prompt)

        logger.debug("%s args: %s", self._backend_label, args)

        env = None
        extra = self._extra_env(chat_id)
        if self.copilot_api_port:
            from boxagent.copilot_api import copilot_env_for_backend
            # Map display label to config backend key
            _label_to_backend = {"Claude CLI": "claude-cli", "Codex CLI": "codex-cli"}
            backend_key = _label_to_backend.get(self._backend_label, "claude-cli")
            copilot_env = copilot_env_for_backend(backend_key, self.copilot_api_port)
            extra = {**(extra or {}), **copilot_env}
        if extra:
            import os
            env = {**os.environ, **extra}

        self._process = await asyncio.create_subprocess_exec(
            *self._resolve_args(args),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.workspace,
            env=env,
            limit=10 * 1024 * 1024,
            start_new_session=(sys.platform != "win32"),
        )

        async for line in self._process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            await self._parse_event(event, callback)

        await self._process.wait()

        if self._cancelled:
            return

        stderr_out = b""
        try:
            stderr_out = await self._process.stderr.read()
        except Exception:
            pass

        stderr_text = stderr_out.decode(errors="replace").strip()
        detail_parts: list[str] = []
        if self._turn_error_detail:
            detail_parts.append(self._turn_error_detail.strip())
        if stderr_text:
            compact_stderr = stderr_text[:500]
            if not any(
                compact_stderr == part
                or compact_stderr in part
                or part in compact_stderr
                for part in detail_parts
            ):
                detail_parts.append(compact_stderr)

        error_message = ""
        if self._process.returncode and self._process.returncode != 0:
            error_message = f"{self._backend_label} exit code {self._process.returncode}"
            if detail_parts:
                error_message += f": {' | '.join(detail_parts)}"
        elif detail_parts:
            error_message = f"{self._backend_label} error: {' | '.join(detail_parts)}"

        if error_message:
            self.last_turn_failed = True
            self.last_turn_error = error_message
            await callback.on_error(error_message)
