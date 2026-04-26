"""HeartbeatManager — periodic wake-up for workgroup admin agents.

Reads HEARTBEAT.md from the admin workspace, forks the admin session to
decide whether action is needed, and dispatches actionable responses back
to the original admin session for execution.
"""

import asyncio
import datetime
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

HEARTBEAT_FILE = "HEARTBEAT.md"


def is_silent_reply(text: str) -> bool:
    """Return True if the agent response means 'nothing to do'."""
    t = text.strip().upper()
    return t in ("NO_REPLY", "HEARTBEAT_OK", "")


def _build_heartbeat_prompt(wg_name: str, content: str) -> str:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        "[HEARTBEAT]\n"
        f"time: {now}\n"
        f"bot: {wg_name}\n"
        "\n"
        "Your HEARTBEAT.md says:\n"
        "---\n"
        f"{content.strip()}\n"
        "---\n"
        "\n"
        "Review your tasks and decide if any action is needed right now.\n"
        "If you decide to act, describe what needs to be done clearly and concisely — your\n"
        "response will be sent to your main session for execution.\n"
        "If nothing to do, respond with only: NO_REPLY"
    )


@dataclass
class HeartbeatManager:
    """Manages periodic heartbeat for a workgroup admin."""

    wg_name: str
    admin_pool: object  # SessionPool
    admin_router: object  # Router
    workspace: str
    interval_seconds: int
    ai_backend: str = "claude-cli"
    model: str = ""
    yolo: bool = False
    discord_channel: object | None = None  # DiscordChannel
    discord_chat_id: str = ""
    _running: bool = field(default=False, repr=False)
    _is_ticking: bool = field(default=False, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)

    def start(self) -> None:
        """Start the heartbeat loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._loop())
        logger.info(
            "Heartbeat started for workgroup '%s' (every %ds)",
            self.wg_name, self.interval_seconds,
        )

    def stop(self) -> None:
        """Stop the heartbeat loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None
        logger.info("Heartbeat stopped for workgroup '%s'", self.wg_name)

    async def _loop(self) -> None:
        """Main loop: sleep then tick."""
        while self._running:
            await asyncio.sleep(self.interval_seconds)
            if not self._running:
                break
            try:
                await self._tick()
            except Exception as e:
                logger.error("Heartbeat tick error for '%s': %s", self.wg_name, e)

    async def _tick(self) -> None:
        """Single heartbeat cycle."""
        if self._is_ticking:
            logger.debug("Heartbeat skipped for '%s' (previous still running)", self.wg_name)
            return

        content = self._read_heartbeat_md()
        if content is None:
            logger.debug("No HEARTBEAT.md for '%s' — skipping", self.wg_name)
            return

        self._is_ticking = True
        logger.info("Heartbeat triggered for '%s'", self.wg_name)

        try:
            # Phase 1: Fork session to decide
            decision = await self._fork_and_decide(content)

            if is_silent_reply(decision):
                logger.debug("Heartbeat silent reply from '%s'", self.wg_name)
                return

            # Phase 2: Send decision to original admin session for execution
            logger.info(
                "Heartbeat action for '%s': %s",
                self.wg_name, decision[:200],
            )
            chat_id = self.discord_chat_id or f"heartbeat:{self.wg_name}"
            await self.admin_router.dispatch_sync(
                decision, chat_id, from_bot="heartbeat",
            )
        finally:
            self._is_ticking = False

    async def _fork_and_decide(self, content: str) -> str:
        """Fork admin session, send heartbeat prompt, return the decision text."""
        from boxagent.agent.claude_process import ClaudeProcess
        from boxagent.router_callback import TextCollector

        session_id = self._find_fork_session_id()
        prompt = _build_heartbeat_prompt(self.wg_name, content)

        proc = ClaudeProcess(
            workspace=self.workspace,
            session_id=session_id,
            fork_session=bool(session_id),
            yolo=self.yolo,
        )
        proc.start()

        try:
            collector = TextCollector()
            await proc.send(prompt, collector, model=self.model)
            return collector.text.strip()
        finally:
            await proc.stop()

    def _find_fork_session_id(self) -> str | None:
        """Find a session_id from the admin pool to fork from."""
        pool = self.admin_pool
        if pool is None:
            return None
        for ctx in pool._chat_contexts.values():
            if ctx.session_id:
                return ctx.session_id
        return None

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
