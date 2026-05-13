"""Router — auth, command parsing, dispatch to agent."""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass, field

from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from boxagent.transports.base import Channel, IncomingMessage
from boxagent.agent.protocol import AgentBackend
from boxagent.router.callback import ChannelCallback, log_turn
from boxagent.router.commands.registry import COMMAND_REGISTRY

if TYPE_CHECKING:
    from boxagent.agent_env import AgentEnv
    from boxagent.sessions import BaseSessionPool, Storage

logger = logging.getLogger(__name__)


@dataclass
class Router:
    backend: AgentBackend
    channel: Channel | None
    allowed_users: list[int]
    storage: "Storage | None" = None
    pool: "BaseSessionPool | None" = None  # if set, used for per-chat dispatch
    bot_name: str = ""
    display_name: str = ""
    config_dir: str = ""
    node_id: str = ""
    local_dir: Path | None = None
    start_time: float = field(default_factory=time.time)
    workspace: str = ""
    extra_skill_dirs: list[str] = field(default_factory=list)
    ai_backend: str = "claude-cli"
    on_backend_switched: Callable[[str, AgentBackend, str], Awaitable[None]] | None = None
    workgroup_agents: list[str] = field(default_factory=list)  # specialist names for context
    get_running_tasks: Callable[[], list[dict]] | None = None
    get_peers: Callable[[], list[dict]] | None = None  # workgroup admin only
    has_peer_channel: bool = False
    telegram_token: str = ""      # from BotConfig at startup
    workgroup_role: str = ""      # "admin" / "specialist" / ""
    passthrough: bool = False     # raw bot: skip context + MCP injection
    _compact_summaries: dict[str, str] = field(default_factory=dict, repr=False)
    _resume_contexts: dict[str, str] = field(default_factory=dict, repr=False)
    _channels: dict[str, Channel] = field(default_factory=dict, repr=False)
    _pending_messages: dict[str, list] = field(default_factory=dict, repr=False)  # chat_id → buffered IncomingMessages

    def _resolve_channel(self, msg: IncomingMessage) -> Channel:
        """Return the channel that should handle this message's replies.

        Raises ``RuntimeError`` if neither the per-msg channel nor the
        default Router channel is configured — a real construction bug
        rather than a runtime fluke (raw bot's web_channel is always
        registered before it sees its first message).
        """
        channel = self._channels.get(msg.channel, self.channel)
        if channel is None:
            raise RuntimeError(
                f"Router '{self.bot_name}' has no channel for {msg.channel!r}"
            )
        return channel

    @contextlib.asynccontextmanager
    async def _acquire_proc(self, chat_id: str):
        """Borrow a backend process for one turn.

        With a pool: acquire(chat_id) → yield → release(chat_id, backend).
        Without one: yield self.backend; no release. Either way callers
        get the same context-managed shape so the dispatch sites don't
        re-implement try/finally.
        """
        pool = self.pool
        if pool is None:
            yield self.backend
            return
        backend = await pool.acquire(chat_id)
        try:
            yield backend
        finally:
            pool.release(chat_id, backend)

    async def handle_message(self, msg: IncomingMessage) -> None:
        try:
            uid = int(msg.user_id)
        except (ValueError, TypeError):
            uid = -1

        logger.debug(
            "Message from user_id=%s (parsed uid=%d), allowed=%s",
            msg.user_id, uid, self.allowed_users,
        )

        channel = self._resolve_channel(msg)

        if not msg.trusted and uid not in self.allowed_users:
            await channel.send_text(
                msg.chat_id,
                "Unauthorized: you are not allowed to use this bot.",
            )
            return

        text = msg.text.strip()
        if not text and not msg.attachments:
            return  # ignore empty messages
        if text.startswith("/"):
            command = text.split()[0].lower()
            spec = COMMAND_REGISTRY.get(command)
            if spec is not None:
                await spec.fn(self, msg, channel)
                return

        # Buffer if this chat_id already has an active turn
        if self.pool and self.pool.get_active(msg.chat_id):
            self._pending_messages.setdefault(msg.chat_id, []).append(msg)
            logger.debug(
                "Buffered message for busy chat_id=%s (%d pending)",
                msg.chat_id, len(self._pending_messages[msg.chat_id]),
            )
            return

        await self._dispatch(msg)

    # ---- Dispatch ----

    async def _dispatch(self, msg: IncomingMessage) -> str:
        """Dispatch message to AI backend. Returns collected response text.

        After each turn, drains any messages that were buffered while the
        session was busy and processes them in a follow-up turn.
        """
        chat_id = msg.chat_id
        current_msg = msg
        last_collected = ""

        while True:
            last_collected = await self._dispatch_one(current_msg)

            # Drain pending messages that arrived during this turn
            pending = self._pending_messages.pop(chat_id, [])
            if not pending:
                break

            # Combine buffered messages into one follow-up prompt
            lines = ["[Messages arrived while you were working]"]
            for m in pending:
                lines.append(f"- {m.text}")
            combined_text = "\n".join(lines)

            logger.info(
                "Draining %d buffered message(s) for chat_id=%s",
                len(pending), chat_id,
            )
            current_msg = IncomingMessage(
                channel=msg.channel,
                chat_id=chat_id,
                user_id=msg.user_id,
                text=combined_text,
                via_workgroup=msg.via_workgroup,
                trusted=msg.trusted,
                channel_info=msg.channel_info,
            )

        return last_collected

    async def _dispatch_one(self, msg: IncomingMessage) -> str:
        """Run a single dispatch turn."""
        chat_id = msg.chat_id
        env = self._build_env(msg)

        # Build system prompt and user message separately
        system_parts = []
        user_parts = []
        model_override = ""

        # Inject session context every turn via --append-system-prompt;
        # the flag is independent of the conversation so it won't be
        # compressed away by context window management.
        # passthrough bots (e.g. "raw") skip all BoxAgent injection so the
        # backend behaves identically to running its CLI standalone.
        used_compact = False
        if not env.passthrough:
            context = self._build_session_context(chat_id, env=env)
            if context:
                system_parts.append(context)

            resume_ctx = self._resume_contexts.get(chat_id, "")
            if resume_ctx:
                system_parts.append(resume_ctx)
                used_resume_ctx = True
            else:
                used_resume_ctx = False

            # Inject compact summary if available (system-level)
            compact_summary = self._compact_summaries.get(chat_id, "")
            if compact_summary:
                system_parts.append(
                    f"[Previous conversation summary]\n{compact_summary}\n"
                    f"[End of summary]\n"
                )
                used_compact = True

        text = msg.text.strip()

        # Parse @model prefix (e.g. "@opus explain this code")
        if text.startswith("@"):
            first_space = text.find(" ")
            if first_space > 0:
                model_override = text[1:first_space]
                text = text[first_space + 1:].strip()

        if text:
            user_parts.append(text)
        for att in msg.attachments:
            user_parts.append(f"[Attached {att.type}: {att.file_path}]")

        append_system_prompt = "\n".join(system_parts)
        prompt = "\n".join(user_parts)

        callback = ChannelCallback(
            channel=self._resolve_channel(msg),
            chat_id=chat_id,
            webhook_name=env.callback_webhook_name(),
        )

        # Acquire a process from the pool (or use the single backend) for the
        # turn. Capture backend state inside the with-block before release clears
        # it (backend.session_id is reset on release; pool keeps a copy though).
        async with self._acquire_proc(chat_id) as backend:
            await callback.start_typing()
            try:
                await backend.send(prompt, callback, model=model_override, chat_id=chat_id, append_system_prompt=append_system_prompt, env=env)
                drain_output = getattr(backend, "drain_output", None)
                if drain_output is not None:
                    await drain_output()
            finally:
                await callback.close()
            turn_failed = backend.last_turn_failed is True
            turn_error = backend.last_turn_error if isinstance(backend.last_turn_error, str) else ""
            proc_sid = backend.session_id

        if used_compact and not turn_failed:
            self._compact_summaries.pop(chat_id, None)
        if used_resume_ctx and not turn_failed:
            # Mirror compact_summary: only consume on success so a
            # failed send leaves the recovered context for retry
            # (yait #18).
            self._resume_contexts.pop(chat_id, None)

        sid = self.pool.get_session_id(chat_id) if self.pool is not None else proc_sid

        if turn_failed:
            logger.warning(
                "Turn failed: bot=%s chat_id=%s session=%s assistant_len=%d error=%s",
                self.bot_name,
                chat_id,
                sid,
                len(callback.collected_text),
                turn_error,
            )
        else:
            logger.info(
                "Turn complete: bot=%s chat_id=%s session=%s assistant_len=%d",
                self.bot_name,
                chat_id,
                sid,
                len(callback.collected_text),
            )

        # Log transcript
        if self.local_dir:
            assistant_text = callback.collected_text
            if turn_failed and not assistant_text and turn_error:
                assistant_text = f"Error: {turn_error}"
            log_turn(
                self.local_dir / "transcripts" / f"{sid or 'unknown'}.jsonl",
                self.bot_name, chat_id, text,
                assistant_text,
            )

        # Persist session after each turn
        if self.storage and sid:
            try:
                save_model = ""
                save_workspace = ""
                if self.pool:
                    save_model = self.pool.get_model(chat_id)
                    save_workspace = self.pool.get_workspace(chat_id)
                self.storage.save_session(
                    self.bot_name, sid,
                    preview=text, backend=self.ai_backend,
                    chat_id=chat_id,
                    model=save_model,
                    workspace=save_workspace,
                )
            except Exception as e:
                logger.warning("Failed to save session: %s", e)

        return callback.collected_text

    # ---- Workgroup delegation ----

    async def dispatch_sync(self, text: str, chat_id: str, from_bot: str = "") -> str:
        """Process a message internally and return the response text.

        Used by workgroup delegation — skips auth and command handling.
        """
        msg = IncomingMessage(
            channel="internal",
            chat_id=chat_id,
            user_id=from_bot or "workgroup",
            text=text,
            via_workgroup=True,
        )
        return await self._dispatch(msg)

    # ---- Internal helpers ----

    async def _reset_backend_session(self):
        """Reset session state via the backend Protocol's reset_session."""
        await self.backend.reset_session()

    def _build_env(self, msg: IncomingMessage) -> AgentEnv:
        """Create an AgentEnv snapshot for this message."""
        from boxagent.router.env_builder import build_env
        return build_env(msg, self)

    def _build_session_context(self, chat_id: str = "", env: AgentEnv | None = None) -> str:
        """Build a one-time context block for the first message of a session."""
        from boxagent.router.env_builder import build_session_context
        return build_session_context(chat_id, self, env=env)
