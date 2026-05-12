"""HeartbeatManager — periodic wake-up for workgroup admin agents.

Reads HEARTBEAT.md from the admin workspace, forks the admin session to
decide whether action is needed, and dispatches actionable responses back
to the original admin session for execution.
"""

import asyncio
import datetime
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from boxagent.workgroup.formatting import format_running_tasks

if TYPE_CHECKING:
    from boxagent.router import Router
    from boxagent.sessions import SessionPool
    from boxagent.transports.web import WebChannel

logger = logging.getLogger(__name__)

HEARTBEAT_FILE = "HEARTBEAT.md"


def is_silent_reply(text: str) -> bool:
    """Return True if the agent response means 'nothing to do'."""
    t = text.strip().upper()
    # Exact match or text contains NO_REPLY / HEARTBEAT_OK anywhere
    if t in ("NO_REPLY", "HEARTBEAT_OK", ""):
        return True
    return "NO_REPLY" in t or "HEARTBEAT_OK" in t


def _build_heartbeat_prompt(
    workgroup_name: str, content: str,
    uptime_seconds: float = 0,
    running_tasks: list[dict] | None = None,
) -> str:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    # Format uptime
    uptime_str = ""
    if uptime_seconds > 0:
        hours, rem = divmod(int(uptime_seconds), 3600)
        minutes, seconds = divmod(rem, 60)
        if hours > 0:
            uptime_str = f"{hours}h {minutes}m"
        else:
            uptime_str = f"{minutes}m {seconds}s"

    tasks_block = format_running_tasks(running_tasks)

    return (
        "[HEARTBEAT CHECK]\n"
        f"time: {now}\n"
        f"bot: {workgroup_name}\n"
        f"uptime: {uptime_str}\n"
        f"{tasks_block}\n"
        "You are in a HEARTBEAT CHECK session — a read-only environment.\n"
        "You have NO execution permissions here: you cannot call tools, run\n"
        "commands, send messages to specialists, or modify any files.\n"
        "\n"
        "Your ONLY job is to DECIDE whether your main session needs to take\n"
        "action. If yes, describe the action clearly — your response will be\n"
        "forwarded to your main session which HAS full permissions.\n"
        "\n"
        "Your HEARTBEAT.md says:\n"
        "---\n"
        f"{content.strip()}\n"
        "---\n"
        "\n"
        "Respond in ONE of these two formats:\n"
        "\n"
        "If nothing to do:\n"
        "<heartbeat_action>NO_REPLY</heartbeat_action>\n"
        "\n"
        "If action is needed:\n"
        "<heartbeat_action>\n"
        "Clear, concise description of what your main session should do.\n"
        "</heartbeat_action>\n"
        "\n"
        "Do NOT attempt to execute anything yourself. Just decide and describe.\n"
        "You MUST wrap your response in <heartbeat_action> tags."
    )


def _extract_action(text: str) -> str:
    """Extract content from <heartbeat_action> tags. Falls back to raw text."""
    import re
    m = re.search(r"<heartbeat_action>(.*?)</heartbeat_action>", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


@dataclass
class HeartbeatManager:
    """Manages periodic heartbeat for a workgroup admin."""

    workgroup_name: str
    admin_pool: "SessionPool"
    admin_router: "Router"
    workspace: str
    interval_seconds: int
    ai_backend: str = "claude-cli"
    model: str = ""
    yolo: bool = False
    web_channel: "WebChannel | None" = None  # display target for heartbeat banner
    display_heartbeat: bool = False
    start_time: float = 0.0
    get_running_tasks: Callable[[], list[dict]] | None = None
    # Provider for the dispatch chat_id (the workgroup's "main" session).
    # Manager wires this to a closure over Storage.get/set_main_chat_id; the
    # provider mints + persists a fresh `heartbeat:<workgroup>-<ts>` if none set.
    main_chat_id_provider: Callable[[], str] | None = None
    _running: bool = field(default=False, repr=False)
    _is_ticking: bool = field(default=False, repr=False)
    _warned_no_fork: bool = field(default=False, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)

    def start(self) -> None:
        """Start the heartbeat loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._loop())
        logger.info(
            "Heartbeat started for workgroup '%s' (every %ds)",
            self.workgroup_name, self.interval_seconds,
        )

    def stop(self) -> None:
        """Stop the heartbeat loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None
        logger.info("Heartbeat stopped for workgroup '%s'", self.workgroup_name)

    async def _loop(self) -> None:
        """Main loop: tick then sleep."""
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error("Heartbeat tick error for '%s': %s", self.workgroup_name, e)
            await asyncio.sleep(self.interval_seconds)
            if not self._running:
                break

    async def _send_display(self, text: str) -> None:
        """Publish heartbeat banner to the synthetic ``heartbeat:<workgroup_name>``
        chat_id on the host's WebChannel. Web UI users can open this chat to
        inspect heartbeat history."""
        if self.web_channel is None:
            return
        try:
            await self.web_channel.send_text(f"heartbeat:{self.workgroup_name}", text)
        except Exception as e:
            logger.warning("Heartbeat '%s': web display failed: %s", self.workgroup_name, e)

    async def _tick(self) -> None:
        """Single heartbeat cycle."""
        from boxagent.log import Category, log

        if self._is_ticking:
            logger.debug("Heartbeat skipped for '%s' (previous still running)", self.workgroup_name)
            return

        content = self._read_heartbeat_md()
        if content is None:
            logger.debug("No HEARTBEAT.md for '%s' — skipping", self.workgroup_name)
            return

        self._is_ticking = True
        logger.info("Heartbeat triggered for '%s'", self.workgroup_name)
        log.debug(
            Category.HEARTBEAT_TICK, f"heartbeat tick for {self.workgroup_name}",
            workgroup=self.workgroup_name,
        )

        try:
            # Display heartbeat prompt (if configured)
            if self.display_heartbeat and self.web_channel:
                now = datetime.datetime.now().strftime("%H:%M")
                await self._send_display(
                    f"**[Heartbeat {now}]**\n```\n{content.strip()}\n```",
                )

            # Phase 1: Fork session to decide (doesn't pollute main session)
            decision, meta = await self._fork_and_decide(content)

            # Log to workspace file
            self._write_heartbeat_log(decision, meta)

            if is_silent_reply(decision):
                logger.debug("Heartbeat silent reply from '%s'", self.workgroup_name)
                log.debug(
                    Category.HEARTBEAT_PAUSE, f"heartbeat silent for {self.workgroup_name}",
                    workgroup=self.workgroup_name,
                )
                if self.display_heartbeat and self.web_channel:
                    await self._send_display("_Heartbeat: nothing to do._")
                return

            # Phase 2: Send decision to admin session for execution
            logger.info(
                "Heartbeat action for '%s': %s",
                self.workgroup_name, decision[:200],
            )
            log.info(
                Category.HEARTBEAT_DRIVE,
                f"heartbeat drive for {self.workgroup_name}",
                workgroup=self.workgroup_name,
                decision_preview=decision[:200],
            )
            if self.display_heartbeat and self.web_channel:
                preview = decision[:500] + "..." if len(decision) > 500 else decision
                await self._send_display(f"**[Heartbeat decision]**\n{preview}")

            chat_id = ""
            if self.main_chat_id_provider is not None:
                try:
                    chat_id = self.main_chat_id_provider() or ""
                except Exception as e:
                    logger.warning("Heartbeat '%s': main_chat_id_provider failed: %s", self.workgroup_name, e)
            if not chat_id:
                chat_id = f"heartbeat:{self.workgroup_name}"
            await self.admin_router.dispatch_sync(
                decision, chat_id, from_bot="heartbeat",
            )
        finally:
            self._is_ticking = False

    async def _fork_and_decide(self, content: str) -> tuple[str, dict]:
        """Fork admin session to evaluate heartbeat without polluting main context.

        Returns (extracted_action, metadata) where metadata contains
        session IDs and raw response for logging.

        Skips backends that don't support fork (e.g. codex-cli) — heartbeat
        is opt-in and the alternative would be polluting the main session.
        """
        from boxagent.agent.backend_factory import create_backend
        from boxagent.agent_env import AgentEnv
        from boxagent.config import BotConfig
        from boxagent.router.callback import TextCollector

        source_session_id = self._find_fork_session_id()
        if not source_session_id:
            return "NO_REPLY", {"reason": "no source session to fork from"}

        uptime = time.time() - self.start_time if self.start_time else 0
        running_tasks = self.get_running_tasks() if self.get_running_tasks is not None else []
        prompt = _build_heartbeat_prompt(
            self.workgroup_name, content,
            uptime_seconds=uptime,
            running_tasks=running_tasks,
        )

        env = AgentEnv(
            bot_name=self.workgroup_name,
            workspace=self.workspace,
            ai_backend=self.ai_backend,
            model=self.model,
            yolo=self.yolo,
            workgroup_role="admin",
        )

        # Build a backend that matches admin's ai_backend rather than
        # hard-wiring ClaudeProcess. fork_and_send dispatches to the
        # right per-backend fork mechanism.
        bot_cfg = BotConfig(
            name=self.workgroup_name,
            ai_backend=self.ai_backend,
            workspace=self.workspace,
            model=self.model,
            yolo=self.yolo,
        )
        backend = create_backend(bot_cfg, session_id=None)
        if not backend.supports_fork:
            if not self._warned_no_fork:
                logger.warning(
                    "Heartbeat for '%s' skipped: backend %r does not support fork",
                    self.workgroup_name, self.ai_backend,
                )
                self._warned_no_fork = True
            return "NO_REPLY", {"reason": f"{self.ai_backend} doesn't support fork"}
        backend.start()

        try:
            collector = TextCollector()
            new_session_id = await backend.fork_and_send(
                source_session_id, prompt, collector,
                model=self.model, env=env,
            )
            raw = collector.text.strip()
            action = _extract_action(raw)
            return action, {
                "source_session_id": source_session_id,
                "fork_session_id": new_session_id,
                "raw_response": raw,
                "prompt": prompt,
            }
        finally:
            await backend.stop()

    def _find_fork_session_id(self) -> str | None:
        """Find admin's main-session id to fork from.

        Source of truth = ``main_chat_id_provider()`` (the workgroup's
        persisted main chat). No silent pool scan: if the provider is
        missing or the resolved chat has no session, return None and warn.
        """
        pool = self.admin_pool
        if pool is None:
            logger.warning("Heartbeat '%s': admin_pool is None", self.workgroup_name)
            return None

        chat_id = ""
        if self.main_chat_id_provider is not None:
            try:
                chat_id = self.main_chat_id_provider() or ""
            except Exception as e:
                logger.warning(
                    "Heartbeat '%s': main_chat_id_provider failed: %s",
                    self.workgroup_name, e,
                )
        if not chat_id:
            logger.warning(
                "Heartbeat '%s': no main_chat_id available — fork will start a fresh session",
                self.workgroup_name,
            )
            return None

        ctx = pool._get_state(chat_id)
        if ctx.session_id:
            logger.info(
                "Heartbeat '%s': fork source via main chat_id=%s session=%s",
                self.workgroup_name, chat_id, ctx.session_id,
            )
            return ctx.session_id

        logger.warning(
            "Heartbeat '%s': main chat_id=%s has no session yet — fork will start fresh",
            self.workgroup_name, chat_id,
        )
        return None

    def _write_heartbeat_log(self, decision: str, meta: dict) -> None:
        """Append heartbeat record to workspace/heartbeat.log."""
        if not self.workspace:
            return
        log_path = Path(self.workspace) / "heartbeat.log"
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        silent = is_silent_reply(decision)
        entry = (
            f"=== {now} ===\n"
            f"source_session: {meta.get('source_session_id', 'none')}\n"
            f"fork_session:   {meta.get('fork_session_id', 'none')}\n"
            f"silent: {silent}\n"
            f"\n"
            f"--- prompt ---\n"
            f"{meta.get('prompt', '').strip()}\n"
            f"\n"
            f"--- raw response ---\n"
            f"{meta.get('raw_response', '').strip()}\n"
            f"\n"
            f"--- extracted action ---\n"
            f"{decision}\n"
            f"\n"
        )
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception as e:
            logger.warning("Failed to write heartbeat log: %s", e)

    def _read_heartbeat_md(self) -> str | None:
        """Read HEARTBEAT.md from workspace. Returns None if not found."""
        if not self.workspace:
            return None
        path = Path(self.workspace) / HEARTBEAT_FILE
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8").strip()
            return text if text else None
        except Exception as e:
            logger.warning("Failed to read %s: %s", path, e)
            return None
