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
    # Exact match or text contains NO_REPLY / HEARTBEAT_OK anywhere
    if t in ("NO_REPLY", "HEARTBEAT_OK", ""):
        return True
    return "NO_REPLY" in t or "HEARTBEAT_OK" in t


def _build_heartbeat_prompt(wg_name: str, content: str) -> str:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        "[HEARTBEAT CHECK]\n"
        f"time: {now}\n"
        f"bot: {wg_name}\n"
        "\n"
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

    wg_name: str
    admin_pool: object  # SessionPool
    admin_router: object  # Router
    workspace: str
    interval_seconds: int
    ai_backend: str = "claude-cli"
    model: str = ""
    yolo: bool = False
    discord_channel: object | None = None  # DiscordChannel
    discord_chat_id: str = ""  # actual text channel ID (not category)
    display_heartbeat: bool = False
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
            "Heartbeat started for workgroup '%s' (every %ds, chat_id=%s)",
            self.wg_name, self.interval_seconds, self.discord_chat_id,
        )

    def stop(self) -> None:
        """Stop the heartbeat loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None
        logger.info("Heartbeat stopped for workgroup '%s'", self.wg_name)

    async def _loop(self) -> None:
        """Main loop: tick then sleep."""
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error("Heartbeat tick error for '%s': %s", self.wg_name, e)
            await asyncio.sleep(self.interval_seconds)
            if not self._running:
                break

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
            # Display heartbeat prompt (if configured)
            if self.display_heartbeat and self.discord_channel and self.discord_chat_id:
                now = datetime.datetime.now().strftime("%H:%M")
                await self.discord_channel.send_text(
                    self.discord_chat_id,
                    f"**[Heartbeat {now}]**\n```\n{content.strip()}\n```",
                )

            # Phase 1: Fork session to decide (doesn't pollute main session)
            decision = await self._fork_and_decide(content)

            if is_silent_reply(decision):
                logger.debug("Heartbeat silent reply from '%s'", self.wg_name)
                if self.display_heartbeat and self.discord_channel and self.discord_chat_id:
                    await self.discord_channel.send_text(
                        self.discord_chat_id, "_Heartbeat: nothing to do._",
                    )
                return

            # Phase 2: Send decision to admin session for execution
            logger.info(
                "Heartbeat action for '%s': %s",
                self.wg_name, decision[:200],
            )
            if self.display_heartbeat and self.discord_channel and self.discord_chat_id:
                preview = decision[:500] + "..." if len(decision) > 500 else decision
                await self.discord_channel.send_text(
                    self.discord_chat_id,
                    f"**[Heartbeat decision]**\n{preview}",
                )

            chat_id = self.discord_chat_id or f"heartbeat:{self.wg_name}"
            await self.admin_router.dispatch_sync(
                decision, chat_id, from_bot="heartbeat",
            )
        finally:
            self._is_ticking = False

    async def _fork_and_decide(self, content: str) -> str:
        """Fork admin session to evaluate heartbeat without polluting main context."""
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
            return _extract_action(collector.text)
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
