"""Router — auth, command parsing, dispatch to agent."""

import logging
import time
from dataclasses import dataclass, field

from pathlib import Path

from boxagent.channels.base import IncomingMessage
from boxagent.router_callback import ChannelCallback, TextCollector, log_turn
from boxagent.router_commands import (
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

logger = logging.getLogger(__name__)

SYSTEM_COMMANDS = {"/status", "/new", "/cancel", "/resume", "/start", "/help", "/verbose", "/sync_skills", "/compact", "/model", "/exec", "/version", "/trust_workspace", "/review_loop", "/cd", "/backend", "/sessions", "/schedule"}


@dataclass
class Router:
    cli_process: object
    channel: object
    allowed_users: list[int]
    storage: object = None
    pool: object = None  # SessionPool — if set, used for per-chat dispatch
    bot_name: str = ""
    display_name: str = ""
    config_dir: str = ""
    node_id: str = ""
    local_dir: Path | None = None
    start_time: float = field(default_factory=time.time)
    workspace: str = ""
    extra_skill_dirs: list[str] = field(default_factory=list)
    ai_backend: str = "claude-cli"
    on_backend_switched: object = None  # async callback(bot_name, new_cli, new_backend)
    on_bus_send: object = None  # async callback(from_bot, target_bot, text, chat_id)
    _compact_summaries: dict[str, str] = field(default_factory=dict, repr=False)
    _resume_contexts: dict[str, str] = field(default_factory=dict, repr=False)
    _channels: dict[str, object] = field(default_factory=dict, repr=False)

    def _resolve_channel(self, msg: IncomingMessage) -> object:
        """Return the channel that should handle this message's replies."""
        return self._channels.get(msg.channel, self.channel)

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

        if uid not in self.allowed_users:
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
            if command in SYSTEM_COMMANDS:
                await self._handle_command(command, msg)
                return

        await self._dispatch(msg)

    async def _handle_command(self, command: str, msg: IncomingMessage):
        ch = self._resolve_channel(msg)
        # --- Core commands (touch session state) ---
        if command == "/new":
            await self._cmd_new(msg)
        elif command == "/cancel":
            await self._cmd_cancel(msg)
        elif command == "/resume":
            await self._cmd_resume(msg)
        elif command == "/compact":
            await self._cmd_compact(msg)
        elif command == "/model":
            await self._cmd_model(msg)
        # --- Auxiliary commands (delegated) ---
        elif command == "/status":
            status_workspace = self.workspace
            if self.pool:
                status_workspace = self.pool.get_workspace(msg.chat_id) or self.workspace
            await cmd_status(
                msg, channel=ch, bot_name=self.display_name or self.bot_name,
                cli_process=self.cli_process, start_time=self.start_time,
                display_name=self.display_name, ai_backend=self.ai_backend,
                workspace=status_workspace, node_id=self.node_id,
                pool=self.pool, chat_id=msg.chat_id,
            )
        elif command == "/start":
            await cmd_start(msg, channel=ch, bot_name=self.display_name or self.bot_name)
        elif command == "/help":
            await cmd_help(msg, channel=ch)
        elif command == "/verbose":
            await cmd_verbose(msg, channel=ch)
        elif command == "/sync_skills":
            await cmd_sync_skills(
                msg, channel=ch, workspace=self.workspace,
                extra_skill_dirs=self.extra_skill_dirs, ai_backend=self.ai_backend,
            )
        elif command == "/exec":
            await cmd_exec(msg, channel=ch, workspace=self.workspace)
        elif command == "/version":
            await cmd_version(msg, channel=ch)
        elif command == "/trust_workspace":
            await cmd_trust_workspace(msg, channel=ch, workspace=self.workspace)
        elif command == "/review_loop":
            await self._cmd_review_loop(msg)
        elif command == "/cd":
            await self._cmd_cd(msg)
        elif command == "/backend":
            await self._cmd_backend(msg)
        elif command == "/sessions":
            await cmd_sessions(msg, channel=ch, storage=self.storage, workspace=self.workspace)
        elif command == "/schedule":
            await cmd_schedule(
                msg, channel=ch, config_dir=self.config_dir,
                local_dir=self.local_dir, node_id=self.node_id,
            )

    # ---- Core session commands ----

    async def _cmd_review_loop(self, msg: IncomingMessage):
        """Start a multi-agent review loop."""
        ch = self._resolve_channel(msg)
        parts = msg.text.split(maxsplit=1)
        topic = parts[1] if len(parts) > 1 else ""
        if not topic:
            await ch.send_text(
                msg.chat_id,
                "Usage: /review_loop <topic>\n"
                "Example: /review_loop write a thread-safe LRU cache",
            )
            return

        if self.ai_backend != "claude-cli":
            await ch.send_text(
                msg.chat_id,
                f"Review loop requires claude-cli backend (current: {self.ai_backend}). "
                "Fork session is not supported by other backends yet.",
            )
            return

        from boxagent.review_loop import ReviewLoopRunner

        runner = ReviewLoopRunner(
            cli_process=self.cli_process,
            channel=ch,
            chat_id=msg.chat_id,
            workspace=self.workspace,
            model=getattr(self.cli_process, "model", ""),
        )
        await runner.run(topic)

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
            await self.cli_process.cancel()
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
        from boxagent.sessions_cli import _load_all_unified_sessions

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
        if callable(send_with_buttons):
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
            self.cli_process.session_id = target_session_id
        self._compact_summaries.pop(chat_id, None)
        self._resume_contexts.pop(chat_id, None)
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
            current = getattr(self.cli_process, "model", "") or "default"

        if len(parts) < 2:
            await ch.send_text(
                chat_id, f"Current model: {current}"
            )
            return

        new_model = parts[1].strip()
        if self.pool:
            self.pool.set_model(chat_id, new_model)
        else:
            self.cli_process.model = new_model
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
            self.cli_process.workspace = new_path
            self.workspace = new_path
            await self._reset_backend_session()
        self._compact_summaries.pop(chat_id, None)
        self._resume_contexts.pop(chat_id, None)
        if self.storage:
            self.storage.clear_session(self.bot_name, chat_id=chat_id)
        await ch.send_text(
            chat_id, f"Workspace switched: {current} → {new_path}"
        )

    _VALID_BACKENDS = {"claude-cli", "codex-cli", "codex-acp"}

    async def _cmd_backend(self, msg: IncomingMessage):
        """Show or switch the AI backend."""
        ch = self._resolve_channel(msg)
        parts = msg.text.strip().split(maxsplit=1)

        if len(parts) < 2:
            await ch.send_text(
                msg.chat_id,
                f"Current backend: {self.ai_backend}\n"
                f"Available: {', '.join(sorted(self._VALID_BACKENDS))}",
            )
            return

        new_backend = parts[1].strip()
        if new_backend not in self._VALID_BACKENDS:
            await ch.send_text(
                msg.chat_id,
                f"Unknown backend: {new_backend}\n"
                f"Available: {', '.join(sorted(self._VALID_BACKENDS))}",
            )
            return

        if new_backend == self.ai_backend:
            await ch.send_text(
                msg.chat_id, f"Already using {new_backend}."
            )
            return

        old_backend = self.ai_backend
        old_proc = self.cli_process

        # Carry over common attributes from old process.
        workspace = getattr(old_proc, "workspace", self.workspace)
        model = getattr(old_proc, "model", "")
        agent = getattr(old_proc, "agent", "")
        bot_token = getattr(old_proc, "bot_token", "")
        yolo = getattr(old_proc, "yolo", False)

        await old_proc.stop()

        if new_backend == "codex-acp":
            from boxagent.agent.acp_process import ACPProcess

            new_proc = ACPProcess(
                workspace=workspace,
                model=model,
                agent=agent,
                bot_token=bot_token,
            )
        elif new_backend == "codex-cli":
            from boxagent.agent.codex_process import CodexProcess

            new_proc = CodexProcess(
                workspace=workspace,
                model=model,
                agent=agent,
                bot_token=bot_token,
                yolo=yolo,
            )
        else:
            from boxagent.agent.claude_process import ClaudeProcess

            new_proc = ClaudeProcess(
                workspace=workspace,
                model=model,
                agent=agent,
                bot_token=bot_token,
                yolo=yolo,
            )

        new_proc.start()
        self.cli_process = new_proc
        self.ai_backend = new_backend
        self._compact_summaries.clear()
        self._resume_contexts.clear()
        if self.storage:
            self.storage.clear_session(self.bot_name, chat_id=msg.chat_id)
        # Notify Gateway so watchdog/scheduler refs are updated too.
        if self.on_backend_switched:
            await self.on_backend_switched(self.bot_name, new_proc, new_backend)
        await ch.send_text(
            msg.chat_id, f"Backend switched: {old_backend} → {new_backend}"
        )

    async def _cmd_compact(self, msg: IncomingMessage):
        """Summarize current conversation, reset session, carry summary forward."""
        ch = self._resolve_channel(msg)
        chat_id = msg.chat_id

        sid = self.pool.get_session_id(chat_id) if self.pool else getattr(self.cli_process, "session_id", None)
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

        # Acquire a process to run the summary
        proc = None
        use_pool = self.pool is not None
        if use_pool:
            proc = await self.pool.acquire(chat_id)
        else:
            proc = self.cli_process

        collector = TextCollector()
        await ch.show_typing(chat_id)
        try:
            await proc.send(summary_prompt, collector)
        except Exception as e:
            if use_pool:
                self.pool.release(chat_id, proc)
            await ch.send_text(
                chat_id, f"Failed to generate summary: {e}"
            )
            return

        if use_pool:
            self.pool.release(chat_id, proc)

        summary = collector.text.strip()
        if not summary:
            await ch.send_text(
                chat_id, "Failed to generate summary (empty response)."
            )
            return

        # Reset session
        if use_pool:
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

    async def _dispatch(self, msg: IncomingMessage):
        chat_id = msg.chat_id
        # Build system prompt and user message separately
        system_parts = []
        user_parts = []
        model_override = ""

        # Inject session context every turn via --append-system-prompt;
        # the flag is independent of the conversation so it won't be
        # compressed away by context window management.
        context = self._build_session_context(chat_id)
        if context:
            system_parts.append(context)

        resume_ctx = self._resume_contexts.pop(chat_id, "")
        if resume_ctx:
            system_parts.append(resume_ctx)

        # Inject compact summary if available (system-level)
        used_compact = False
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
        )

        # Acquire a process from the pool (or use the single cli_process)
        proc = None
        use_pool = self.pool is not None
        if use_pool:
            proc = await self.pool.acquire(chat_id)
        else:
            proc = self.cli_process

        await callback.start_typing()
        try:
            await proc.send(prompt, callback, model=model_override, chat_id=chat_id, append_system_prompt=append_system_prompt)
            drain_output = getattr(proc, "drain_output", None)
            if callable(drain_output):
                await drain_output()
            turn_failed = getattr(proc, "last_turn_failed", False) is True
            if used_compact and not turn_failed:
                self._compact_summaries.pop(chat_id, None)
        finally:
            await callback.close()
            if use_pool:
                self.pool.release(chat_id, proc)

        turn_failed = getattr(proc, "last_turn_failed", False) is True
        turn_error = getattr(proc, "last_turn_error", "")
        if not isinstance(turn_error, str):
            turn_error = ""

        if turn_failed:
            logger.warning(
                "Turn failed: bot=%s chat_id=%s session=%s assistant_len=%d error=%s",
                self.bot_name,
                chat_id,
                getattr(proc, "session_id", None),
                len(callback.collected_text),
                turn_error,
            )
        else:
            logger.info(
                "Turn complete: bot=%s chat_id=%s session=%s assistant_len=%d",
                self.bot_name,
                chat_id,
                getattr(proc, "session_id", None),
                len(callback.collected_text),
            )

        # Log transcript
        sid = self.pool.get_session_id(chat_id) if use_pool else getattr(proc, "session_id", None)
        if self.local_dir:
            assistant_text = callback.collected_text
            if turn_failed and not assistant_text and turn_error:
                assistant_text = f"Error: {turn_error}"
            log_turn(
                self.local_dir / "transcripts" / f"{sid or 'unknown'}.jsonl",
                self.bot_name, chat_id, text,
                assistant_text,
            )

        # Bus: check if AI output starts with @bot-name and forward internally
        if self.on_bus_send and not turn_failed:
            await self._check_bus_output(callback.collected_text, chat_id)

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

    # ---- Internal helpers ----

    async def _check_bus_output(self, text: str, chat_id: str) -> None:
        """If AI output contains @bot-name lines, forward them via bus."""
        if not text or not self.on_bus_send:
            return
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("@"):
                continue
            first_space = line.find(" ")
            if first_space < 0:
                continue
            target = line[1:first_space]
            body = line[first_space + 1:].strip()
            if not body:
                continue
            try:
                await self.on_bus_send(self.bot_name, target, body, chat_id)
            except Exception as e:
                logger.warning("Bus send failed: %s → @%s: %s", self.bot_name, target, e)

    async def _reset_backend_session(self):
        """Reset session state, falling back to session_id-only backends."""
        reset_session = getattr(self.cli_process, "reset_session", None)
        if callable(reset_session):
            await reset_session()
        else:
            self.cli_process.session_id = None

    def _build_session_context(self, chat_id: str = "") -> str:
        """Build a one-time context block for the first message of a session."""
        from boxagent.context import build_session_context

        if self.pool and chat_id:
            model = self.pool.get_model(chat_id) or "default"
            workspace = self.pool.get_workspace(chat_id) or self.workspace
        else:
            model = getattr(self.cli_process, "model", "") or "default"
            workspace = self.workspace
        return build_session_context(
            bot_name=self.bot_name,
            display_name=self.display_name,
            node_id=self.node_id,
            ai_backend=self.ai_backend,
            model=model,
            workspace=workspace,
            config_dir=self.config_dir,
        )
