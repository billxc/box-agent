"""Router — auth, command parsing, dispatch to agent."""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass, field

from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from boxagent.transports.base import Channel, IncomingMessage
from boxagent.agent.protocol import AgentBackend, BACKEND_KINDS
from boxagent.router.callback import ChannelCallback, TextCollector, log_turn
from boxagent.router.commands import (
    cmd_exec,
    cmd_help,
    cmd_schedule,
    cmd_sessions,
    cmd_start,
    cmd_status,
    cmd_sync_skills,
    cmd_trust_workspace,
    cmd_verbose,
    cmd_version,
)

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
        ch = self._channels.get(msg.channel, self.channel)
        if ch is None:
            raise RuntimeError(
                f"Router '{self.bot_name}' has no channel for {msg.channel!r}"
            )
        return ch

    @contextlib.asynccontextmanager
    async def _acquire_proc(self, chat_id: str):
        """Borrow a backend process for one turn.

        With a pool: acquire(chat_id) → yield → release(chat_id, proc).
        Without one: yield self.backend; no release. Either way callers
        get the same context-managed shape so the dispatch sites don't
        re-implement try/finally.
        """
        pool = self.pool
        if pool is None:
            yield self.backend
            return
        proc = await pool.acquire(chat_id)
        try:
            yield proc
        finally:
            pool.release(chat_id, proc)

    async def handle_message(self, msg: IncomingMessage) -> None:
        try:
            uid = int(msg.user_id)
        except (ValueError, TypeError):
            uid = -1

        logger.debug(
            "Message from user_id=%s (parsed uid=%d), allowed=%s",
            msg.user_id, uid, self.allowed_users,
        )

        ch = self._resolve_channel(msg)

        if not msg.trusted and uid not in self.allowed_users:
            await ch.send_text(
                msg.chat_id,
                "Unauthorized: you are not allowed to use this bot.",
            )
            return

        text = msg.text.strip()
        if not text and not msg.attachments:
            return  # ignore empty messages
        if text.startswith("/"):
            command = text.split()[0].lower()
            if command in self._command_handlers():
                await self._handle_command(command, msg)
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

    def _command_handlers(self):
        """Return ``command → async callable(msg, channel)`` dispatch map.

        Built per-call (cheap, ~17 entries) so handlers can close over
        ``self``. ``SYSTEM_COMMANDS`` is derived from the keys, so adding
        a command means one entry instead of two list edits.
        """
        return {
            # Core commands (touch session state — defined on Router)
            "/new":     lambda msg, ch: self._cmd_new(msg),
            "/cancel":  lambda msg, ch: self._cmd_cancel(msg),
            "/resume":  lambda msg, ch: self._cmd_resume(msg),
            "/compact": lambda msg, ch: self._cmd_compact(msg),
            "/model":   lambda msg, ch: self._cmd_model(msg),
            "/cd":      lambda msg, ch: self._cmd_cd(msg),
            "/backend": lambda msg, ch: self._cmd_backend(msg),
            # Auxiliary commands (free functions in router/commands.py)
            "/status":   lambda msg, ch: cmd_status(
                msg, channel=ch, bot_name=self.display_name or self.bot_name,
                backend=self.backend, start_time=self.start_time,
                display_name=self.display_name, ai_backend=self.ai_backend,
                workspace=(self.pool.get_workspace(msg.chat_id) if self.pool else "") or self.workspace,
                node_id=self.node_id, pool=self.pool, chat_id=msg.chat_id,
            ),
            "/start":    lambda msg, ch: cmd_start(msg, channel=ch, bot_name=self.display_name or self.bot_name),
            "/help":     lambda msg, ch: cmd_help(msg, channel=ch),
            "/verbose":  lambda msg, ch: cmd_verbose(msg, channel=ch),
            "/sync_skills": lambda msg, ch: cmd_sync_skills(
                msg, channel=ch, workspace=self.workspace,
                extra_skill_dirs=self.extra_skill_dirs, ai_backend=self.ai_backend,
            ),
            "/exec":     lambda msg, ch: cmd_exec(msg, channel=ch, workspace=self.workspace),
            "/version":  lambda msg, ch: cmd_version(msg, channel=ch),
            "/trust_workspace": lambda msg, ch: cmd_trust_workspace(msg, channel=ch, workspace=self.workspace),
            "/sessions": lambda msg, ch: cmd_sessions(msg, channel=ch, storage=self.storage, workspace=self.workspace),
            "/schedule": lambda msg, ch: cmd_schedule(
                msg, channel=ch, config_dir=self.config_dir,
                local_dir=self.local_dir, node_id=self.node_id,
            ),
        }

    async def _handle_command(self, command: str, msg: IncomingMessage):
        handler = self._command_handlers().get(command)
        if handler is None:
            return
        ch = self._resolve_channel(msg)
        await handler(msg, ch)

    # ---- Core session commands ----

    async def _cmd_new(self, msg: IncomingMessage):
        ch = self._resolve_channel(msg)
        chat_id = msg.chat_id
        if self.pool:
            self.pool.clear_session(chat_id)
        else:
            await self._reset_backend_session()
        self._compact_summaries.pop(chat_id, None)
        self._resume_contexts.pop(chat_id, None)
        if self.storage:
            self.storage.clear_session(self.bot_name, chat_id=chat_id)
        await ch.send_text(
            chat_id, "Started a fresh conversation."
        )

    async def _cmd_cancel(self, msg: IncomingMessage):
        ch = self._resolve_channel(msg)
        chat_id = msg.chat_id
        if self.pool:
            active = self.pool.get_active(chat_id)
            if active:
                await active.cancel()
                await ch.send_text(chat_id, "Cancelled current task.")
            else:
                await ch.send_text(chat_id, "No active task to cancel.")
        else:
            await self.backend.cancel()
            await ch.send_text(chat_id, "Cancelled current task.")

    async def _cmd_resume(self, msg: IncomingMessage):
        ch = self._resolve_channel(msg)
        if not self.storage:
            await ch.send_text(
                msg.chat_id, "Resume history is unavailable (storage is disabled)."
            )
            return

        arg = msg.text.strip().partition(" ")[2].strip()

        # Use unified 3-source loader (Claude CLI + BoxAgent + Codex)
        from boxagent.sessions.cli import _load_all_unified_sessions

        all_sessions = _load_all_unified_sessions(
            storage=self.storage, workspace=self.workspace,
        )

        if not arg:
            await self._resume_list(msg, all_sessions)
            return

        # Look up by session ID
        target = None
        for entry in all_sessions:
            if str(entry.get("sessionId", "")) == arg:
                target = entry
                break

        if target is None:
            await ch.send_text(
                msg.chat_id,
                f"Resume target not found: `{arg}`. Send `/resume` to list available sessions.",
            )
            return

        await self._do_resume_native(msg, target)

    async def _resume_list(
        self,
        msg: IncomingMessage,
        sessions: list[dict[str, object]],
    ):
        ch = self._resolve_channel(msg)
        if not sessions:
            await ch.send_text(
                msg.chat_id, "No saved sessions found.",
            )
            return

        # Group by backend, keep up to 10 per group
        groups: dict[str, list[dict[str, object]]] = {}
        for entry in sessions:
            backend = str(entry.get("backend", "")) or "other"
            groups.setdefault(backend, []).append(entry)

        lines = ["**Resume Sessions**"]
        buttons = []
        idx = 0
        for backend in sorted(groups):
            lines.append(f"\n**{backend}**")
            for entry in groups[backend][:10]:
                idx += 1
                session_id = str(entry.get("sessionId", ""))
                modified_ts = entry.get("modified_ts")
                time_str = ""
                if isinstance(modified_ts, int | float) and modified_ts:
                    time_str = time.strftime("%m-%d %H:%M", time.localtime(modified_ts))
                preview = entry.get("summary") or entry.get("firstPrompt") or entry.get("preview") or ""
                preview_text = ""
                if isinstance(preview, str) and preview:
                    preview_text = f" — {preview[:60]}"
                short_id = session_id[:8]
                project = entry.get("project", "")
                ws_label = f" `{project}`" if project else ""
                lines.append(f"{idx}. `{short_id}` {time_str}{ws_label}{preview_text}")
                btn_label = f"{idx}. {time_str}"
                if isinstance(preview, str) and preview:
                    btn_label += f" {preview[:28]}"
                buttons.append((btn_label, f"/resume {session_id}"))
        text = "\n".join(lines)
        send_with_buttons = getattr(ch, "send_text_with_inline_keyboard", None)
        if send_with_buttons is not None:
            await send_with_buttons(msg.chat_id, text, buttons)
        else:
            await ch.send_text(msg.chat_id, text)

    async def _do_resume_native(self, msg: IncomingMessage, entry: dict[str, object]):
        ch = self._resolve_channel(msg)
        chat_id = msg.chat_id
        target_session_id = str(entry["sessionId"])
        restored_workspace = str(entry.get("projectPath", "")) if entry.get("projectPath") else ""
        restored_model = str(entry.get("model", "")) if entry.get("model") else ""

        if self.pool:
            self.pool.set_session_id(chat_id, target_session_id)
            if restored_workspace:
                self.pool.set_workspace(chat_id, restored_workspace)
            if restored_model:
                self.pool.set_model(chat_id, restored_model)
        else:
            await self._reset_backend_session()
            self.backend.session_id = target_session_id
        self._compact_summaries.pop(chat_id, None)
        self._resume_contexts.pop(chat_id, None)
        if self.storage is not None:
            self.storage.save_session(self.bot_name, target_session_id, chat_id=chat_id)

        # Build confirmation message
        info_parts = [f"Resumed session `{target_session_id[:8]}`"]
        if restored_workspace:
            info_parts.append(f"workspace: `{restored_workspace}`")
        if restored_model:
            info_parts.append(f"model: `{restored_model}`")
        await ch.send_text(chat_id, "\n".join(info_parts))

    async def _cmd_model(self, msg: IncomingMessage):
        """Show or switch the model for this chat."""
        ch = self._resolve_channel(msg)
        chat_id = msg.chat_id
        parts = msg.text.strip().split(maxsplit=1)

        if self.pool:
            current = self.pool.get_model(chat_id) or "default"
        else:
            current = getattr(self.backend, "model", "") or "default"

        if len(parts) < 2:
            await ch.send_text(
                chat_id, f"Current model: {current}"
            )
            return

        new_model = parts[1].strip()
        if self.pool:
            self.pool.set_model(chat_id, new_model)
        else:
            self.backend.model = new_model
        await ch.send_text(
            chat_id, f"Model switched: {current} → {new_model}"
        )

    async def _cmd_cd(self, msg: IncomingMessage):
        """Show or switch the working directory for this chat."""
        import os

        ch = self._resolve_channel(msg)
        chat_id = msg.chat_id
        parts = msg.text.strip().split(maxsplit=1)

        if self.pool:
            current = self.pool.get_workspace(chat_id) or "(not set)"
        else:
            current = self.workspace or "(not set)"

        if len(parts) < 2:
            await ch.send_text(
                chat_id, f"Current workspace: {current}"
            )
            return

        new_path = os.path.expanduser(parts[1].strip())
        if not os.path.isdir(new_path):
            await ch.send_text(
                chat_id, f"Directory not found: {new_path}"
            )
            return

        new_path = os.path.realpath(new_path)
        if self.pool:
            self.pool.set_workspace(chat_id, new_path)
            self.pool.clear_session(chat_id)
        else:
            self.backend.workspace = new_path
            self.workspace = new_path
            await self._reset_backend_session()
        self._compact_summaries.pop(chat_id, None)
        self._resume_contexts.pop(chat_id, None)
        if self.storage:
            self.storage.clear_session(self.bot_name, chat_id=chat_id)
        await ch.send_text(
            chat_id, f"Workspace switched: {current} → {new_path}"
        )



    async def _cmd_backend(self, msg: IncomingMessage):
        """Show or switch the AI backend."""
        from boxagent.agent.backend_factory import create_backend
        from boxagent.config import BotConfig

        ch = self._resolve_channel(msg)
        parts = msg.text.strip().split(maxsplit=1)
        valid = sorted(BACKEND_KINDS)

        if len(parts) < 2:
            await ch.send_text(
                msg.chat_id,
                f"Current backend: {self.ai_backend}\nAvailable: {', '.join(valid)}",
            )
            return

        new_kind = parts[1].strip()
        if new_kind not in BACKEND_KINDS:
            await ch.send_text(
                msg.chat_id,
                f"Unknown backend: {new_kind}\nAvailable: {', '.join(valid)}",
            )
            return

        if new_kind == self.ai_backend:
            await ch.send_text(msg.chat_id, f"Already using {new_kind}.")
            return

        old_kind = self.ai_backend
        old_backend = self.backend

        # Carry over common attributes from old backend.
        bot_cfg = BotConfig(
            name=self.bot_name,
            ai_backend=new_kind,
            workspace=getattr(old_backend, "workspace", self.workspace),
            model=getattr(old_backend, "model", "") or "",
            agent=getattr(old_backend, "agent", "") or "",
            yolo=bool(getattr(old_backend, "yolo", False)),
        )
        await old_backend.stop()
        new_backend = create_backend(bot_cfg, session_id=None)

        new_backend.start()
        self.backend = new_backend
        self.ai_backend = new_kind
        self._compact_summaries.clear()
        self._resume_contexts.clear()
        if self.storage:
            self.storage.clear_session(self.bot_name, chat_id=msg.chat_id)
        # Notify Gateway so watchdog/scheduler refs are updated too.
        if self.on_backend_switched:
            await self.on_backend_switched(self.bot_name, new_backend, new_kind)
        await ch.send_text(
            msg.chat_id, f"Backend switched: {old_kind} → {new_kind}"
        )

    async def _cmd_compact(self, msg: IncomingMessage):
        """Summarize current conversation, reset session, carry summary forward."""
        ch = self._resolve_channel(msg)
        chat_id = msg.chat_id

        sid = self.pool.get_session_id(chat_id) if self.pool else getattr(self.backend, "session_id", None)
        if not sid:
            await ch.send_text(
                chat_id, "No active session to compact."
            )
            return

        await ch.send_text(chat_id, "Compacting conversation...")

        # Extract user instructions after /compact
        user_hint = msg.text.strip().partition(" ")[2].strip()

        summary_prompt = (
            "Please provide a concise summary of our entire conversation so far. "
            "Include: key topics discussed, decisions made, important context, "
            "and any pending tasks. Format as bullet points. "
            "This summary will be used to continue in a new session."
        )
        if user_hint:
            summary_prompt += f"\n\nAdditional instructions: {user_hint}"

        collector = TextCollector()
        await ch.show_typing(chat_id)
        try:
            env = self._build_env(msg)
            async with self._acquire_proc(chat_id) as proc:
                await proc.send(summary_prompt, collector, env=env)
        except Exception as e:
            await ch.send_text(
                chat_id, f"Failed to generate summary: {e}"
            )
            return

        summary = collector.text.strip()
        if not summary:
            await ch.send_text(
                chat_id, "Failed to generate summary (empty response)."
            )
            return

        # Reset session
        if self.pool is not None:
            self.pool.clear_session(chat_id)
        else:
            await self._reset_backend_session()
        if self.storage:
            self.storage.clear_session(self.bot_name, chat_id=chat_id)

        self._resume_contexts.pop(chat_id, None)
        self._compact_summaries[chat_id] = summary

        await ch.send_text(
            chat_id,
            f"Session compacted. Summary:\n\n{summary}\n\n"
            "Next message will start a new session with this context.",
        )

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
        # turn. Capture proc state inside the with-block before release clears
        # it (proc.session_id is reset on release; pool keeps a copy though).
        async with self._acquire_proc(chat_id) as proc:
            await callback.start_typing()
            try:
                await proc.send(prompt, callback, model=model_override, chat_id=chat_id, append_system_prompt=append_system_prompt, env=env)
                drain_output = getattr(proc, "drain_output", None)
                if drain_output is not None:
                    await drain_output()
            finally:
                await callback.close()
            turn_failed = getattr(proc, "last_turn_failed", False) is True
            turn_error_raw = getattr(proc, "last_turn_error", "")
            turn_error = turn_error_raw if isinstance(turn_error_raw, str) else ""
            proc_sid = getattr(proc, "session_id", None)

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
        """Reset session state, falling back to session_id-only backends."""
        reset_session = getattr(self.backend, "reset_session", None)
        if reset_session is not None:
            await reset_session()
        else:
            self.backend.session_id = None

    def _build_env(self, msg: IncomingMessage) -> AgentEnv:
        """Create an AgentEnv snapshot for this message."""
        from boxagent.router.env_builder import build_env
        return build_env(msg, self)

    def _build_session_context(self, chat_id: str = "", env: AgentEnv | None = None) -> str:
        """Build a one-time context block for the first message of a session."""
        from boxagent.router.env_builder import build_session_context
        return build_session_context(chat_id, self, env=env)
