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
    cmd_start,
    cmd_status,
    cmd_sync_skills,
    cmd_trust_workspace,
    cmd_verbose,
    cmd_version,
)

logger = logging.getLogger(__name__)

SYSTEM_COMMANDS = {"/status", "/new", "/cancel", "/resume", "/start", "/help", "/verbose", "/sync_skills", "/compact", "/model", "/exec", "/version", "/trust_workspace"}


@dataclass
class Router:
    cli_process: object
    channel: object
    allowed_users: list[int]
    storage: object = None
    bot_name: str = ""
    display_name: str = ""
    config_dir: str = ""
    node_id: str = ""
    local_dir: Path | None = None
    start_time: float = field(default_factory=time.time)
    workspace: str = ""
    extra_skill_dirs: list[str] = field(default_factory=list)
    ai_backend: str = "claude-cli"
    _compact_summary: str = field(default="", repr=False)
    _resume_context: str = field(default="", repr=False)
    _session_context_injected: bool = field(default=False, repr=False)

    async def handle_message(self, msg: IncomingMessage) -> None:
        try:
            uid = int(msg.user_id)
        except (ValueError, TypeError):
            uid = -1

        logger.debug(
            "Message from user_id=%s (parsed uid=%d), allowed=%s",
            msg.user_id, uid, self.allowed_users,
        )

        if uid not in self.allowed_users:
            await self.channel.send_text(
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
            await cmd_status(
                msg, channel=self.channel, bot_name=self.display_name or self.bot_name,
                cli_process=self.cli_process, start_time=self.start_time,
            )
        elif command == "/start":
            await cmd_start(msg, channel=self.channel, bot_name=self.display_name or self.bot_name)
        elif command == "/help":
            await cmd_help(msg, channel=self.channel)
        elif command == "/verbose":
            await cmd_verbose(msg, channel=self.channel)
        elif command == "/sync_skills":
            await cmd_sync_skills(
                msg, channel=self.channel, workspace=self.workspace,
                extra_skill_dirs=self.extra_skill_dirs, ai_backend=self.ai_backend,
            )
        elif command == "/exec":
            await cmd_exec(msg, channel=self.channel, workspace=self.workspace)
        elif command == "/version":
            await cmd_version(msg, channel=self.channel)
        elif command == "/trust_workspace":
            await cmd_trust_workspace(msg, channel=self.channel, workspace=self.workspace)

    # ---- Core session commands ----

    async def _cmd_new(self, msg: IncomingMessage):
        await self._reset_backend_session()
        self._compact_summary = ""
        self._resume_context = ""
        self._session_context_injected = False
        if self.storage:
            self.storage.clear_session(self.bot_name)
        await self.channel.send_text(
            msg.chat_id, "Started a fresh conversation."
        )

    async def _cmd_cancel(self, msg: IncomingMessage):
        await self.cli_process.cancel()
        await self.channel.send_text(
            msg.chat_id, "Cancelled current task."
        )

    async def _cmd_resume(self, msg: IncomingMessage):
        if not self.storage:
            await self.channel.send_text(
                msg.chat_id, "Resume history is unavailable (storage is disabled)."
            )
            return

        arg = msg.text.strip().partition(" ")[2].strip()

        # Gather sessions from both sources
        native_history = self.storage.list_session_history(self.bot_name)
        codex_history = self.storage.list_codex_session_history(
            self.workspace, limit=None if arg else 10,
        )

        # Tag each entry with its source
        for entry in native_history:
            entry["_source"] = "native"
        for entry in codex_history:
            entry["_source"] = "codex"

        # Merge, dedup by session_id (prefer native), sort by saved_at desc
        seen: set[str] = set()
        merged: list[dict[str, object]] = []
        for entry in native_history:
            sid = str(entry["session_id"])
            if sid not in seen:
                seen.add(sid)
                merged.append(entry)
        for entry in codex_history:
            sid = str(entry["session_id"])
            if sid not in seen:
                seen.add(sid)
                merged.append(entry)

        # Group by backend, sort each group by saved_at desc, keep up to 10 per group
        groups: dict[str, list[dict[str, object]]] = {}
        for entry in merged:
            backend = str(entry.get("backend", "")) or "other"
            groups.setdefault(backend, []).append(entry)

        display_ordered: list[dict[str, object]] = []
        for backend in sorted(groups):
            group = sorted(
                groups[backend],
                key=lambda e: int(e.get("saved_at", 0)) if isinstance(e.get("saved_at"), (int, float)) else 0,
                reverse=True,
            )
            display_ordered.extend(group[:10])

        if not arg:
            await self._resume_list(msg, display_ordered, sorted(groups))
        else:
            await self._resume_select(msg, arg, display_ordered)

    async def _resume_list(
        self,
        msg: IncomingMessage,
        display_ordered: list[dict[str, object]],
        backend_order: list[str],
    ):
        if not display_ordered:
            await self.channel.send_text(
                msg.chat_id, "No saved sessions found.",
            )
            return

        lines = ["**Resume Sessions**"]
        buttons = []
        idx = 0
        for backend in backend_order:
            lines.append(f"\n**{backend}**")
            for entry in display_ordered:
                entry_backend = str(entry.get("backend", "")) or "other"
                if entry_backend != backend:
                    continue
                idx += 1
                session_id = str(entry["session_id"])
                saved_at = entry.get("saved_at")
                time_str = ""
                if isinstance(saved_at, int | float):
                    time_str = time.strftime("%m-%d %H:%M", time.localtime(saved_at))
                preview = entry.get("preview")
                preview_text = ""
                if isinstance(preview, str) and preview:
                    preview_text = f" — {preview}"
                short_id = session_id[:8]
                lines.append(f"{idx}. `{short_id}` {time_str}{preview_text}")
                btn_label = f"{idx}. {time_str}"
                if isinstance(preview, str) and preview:
                    btn_label += f" {preview[:28]}"
                buttons.append((btn_label, f"/resume {session_id}"))
        text = "\n".join(lines)
        send_with_buttons = getattr(self.channel, "send_text_with_inline_keyboard", None)
        if callable(send_with_buttons):
            await send_with_buttons(msg.chat_id, text, buttons)
        else:
            await self.channel.send_text(msg.chat_id, text)

    async def _resume_select(self, msg: IncomingMessage, arg: str, merged: list[dict[str, object]]):
        target_entry: dict[str, object] | None = None
        if arg.isdigit():
            index = int(arg)
            if index <= 0 or index > len(merged):
                await self.channel.send_text(
                    msg.chat_id,
                    f"Resume index out of range: {index}. Send `/resume` to list saved sessions.",
                )
                return
            target_entry = merged[index - 1]
        else:
            for entry in merged:
                if str(entry.get("session_id")) == arg:
                    target_entry = entry
                    break

        if target_entry is None:
            await self.channel.send_text(
                msg.chat_id,
                f"Resume target not found: `{arg}`. Send `/resume` to list available sessions.",
            )
            return

        source = target_entry.get("_source", "")
        if source == "codex":
            await self._do_resume_codex(msg, target_entry)
        else:
            await self._do_resume_native(msg, target_entry)

    async def _do_resume_native(self, msg: IncomingMessage, entry: dict[str, object]):
        target_session_id = str(entry["session_id"])
        await self._reset_backend_session()
        self._compact_summary = ""
        self._resume_context = ""
        self.cli_process.session_id = target_session_id
        self.storage.save_session(self.bot_name, target_session_id)
        await self.channel.send_text(
            msg.chat_id,
            f"Resume target set to `{target_session_id}`. Your next message will continue that session.",
        )

    async def _do_resume_codex(self, msg: IncomingMessage, entry: dict[str, object]):
        target_path = entry.get("path")
        if not isinstance(target_path, str) or not target_path:
            await self.channel.send_text(
                msg.chat_id,
                "Selected Codex session is missing a local rollout path.",
            )
            return

        resume_context = self.storage.build_codex_resume_context(target_path)
        if not resume_context:
            await self.channel.send_text(
                msg.chat_id,
                "Failed to recover context from the selected Codex session.",
            )
            return

        await self._reset_backend_session()
        self._compact_summary = ""
        self._resume_context = resume_context
        self.storage.clear_session(self.bot_name)

        session_id = str(entry["session_id"])
        await self.channel.send_text(
            msg.chat_id,
            f"Prepared soft resume from Codex session `{session_id}`. "
            "Your next message will start a new session with recovered context from local logs.",
        )

    async def _cmd_model(self, msg: IncomingMessage):
        """Show or switch the default model."""
        parts = msg.text.strip().split(maxsplit=1)
        current = getattr(self.cli_process, "model", "") or "default"

        if len(parts) < 2:
            await self.channel.send_text(
                msg.chat_id, f"Current model: {current}"
            )
            return

        new_model = parts[1].strip()
        self.cli_process.model = new_model
        await self.channel.send_text(
            msg.chat_id, f"Model switched: {current} → {new_model}"
        )

    async def _cmd_compact(self, msg: IncomingMessage):
        """Summarize current conversation, reset session, carry summary forward."""
        if not self.cli_process.session_id:
            await self.channel.send_text(
                msg.chat_id, "No active session to compact."
            )
            return

        await self.channel.send_text(msg.chat_id, "Compacting conversation...")

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
        await self.channel.show_typing(msg.chat_id)
        try:
            await self.cli_process.send(summary_prompt, collector)
        except Exception as e:
            await self.channel.send_text(
                msg.chat_id, f"Failed to generate summary: {e}"
            )
            return

        summary = collector.text.strip()
        if not summary:
            await self.channel.send_text(
                msg.chat_id, "Failed to generate summary (empty response)."
            )
            return

        # Reset session
        await self._reset_backend_session()
        if self.storage:
            self.storage.clear_session(self.bot_name)

        self._resume_context = ""
        self._compact_summary = summary
        self._session_context_injected = False

        await self.channel.send_text(
            msg.chat_id,
            f"Session compacted. Summary:\n\n{summary}\n\n"
            "Next message will start a new session with this context.",
        )

    # ---- Dispatch ----

    async def _dispatch(self, msg: IncomingMessage):
        # Build prompt: text + attachment file paths
        parts = []
        model_override = ""

        # Inject session context on first message
        if not self._session_context_injected:
            context = self._build_session_context()
            if context:
                parts.append(context)
            self._session_context_injected = True

        if self._resume_context:
            parts.append(self._resume_context)
            self._resume_context = ""

        # Inject compact summary if available
        used_compact = False
        if self._compact_summary:
            parts.append(
                f"[Previous conversation summary]\n{self._compact_summary}\n"
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
            parts.append(text)
        for att in msg.attachments:
            parts.append(f"[Attached {att.type}: {att.file_path}]")
        prompt = "\n".join(parts)

        callback = ChannelCallback(
            channel=self.channel,
            chat_id=msg.chat_id,
        )

        await callback.start_typing()
        try:
            await self.cli_process.send(prompt, callback, model=model_override, chat_id=msg.chat_id)
            drain_output = getattr(self.cli_process, "drain_output", None)
            if callable(drain_output):
                await drain_output()
            if used_compact:
                self._compact_summary = ""
        finally:
            await callback.close()

        logger.info(
            "Turn complete: bot=%s chat_id=%s session=%s assistant_len=%d",
            self.bot_name,
            msg.chat_id,
            getattr(self.cli_process, "session_id", None),
            len(callback.collected_text),
        )

        # Log transcript
        if self.local_dir:
            sid = getattr(self.cli_process, "session_id", None) or "unknown"
            log_turn(
                self.local_dir / "transcripts" / f"{sid}.jsonl",
                self.bot_name, msg.chat_id, text,
                callback.collected_text,
            )

        # Persist session after each turn
        if self.storage and self.cli_process.session_id:
            try:
                self.storage.save_session(
                    self.bot_name, self.cli_process.session_id,
                    preview=text, backend=self.ai_backend,
                )
            except Exception as e:
                logger.warning("Failed to save session: %s", e)

    # ---- Internal helpers ----

    async def _reset_backend_session(self):
        """Reset session state, falling back to session_id-only backends."""
        reset_session = getattr(self.cli_process, "reset_session", None)
        if callable(reset_session):
            await reset_session()
        else:
            self.cli_process.session_id = None

    def _build_session_context(self) -> str:
        """Build a one-time context block for the first message of a session."""
        from boxagent.context import build_session_context

        model = getattr(self.cli_process, "model", "") or "default"
        return build_session_context(
            bot_name=self.bot_name,
            display_name=self.display_name,
            node_id=self.node_id,
            ai_backend=self.ai_backend,
            model=model,
            workspace=self.workspace,
            config_dir=self.config_dir,
        )
