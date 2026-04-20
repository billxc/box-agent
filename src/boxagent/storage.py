"""Storage helpers — sessions.yaml management."""

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


class Storage:
    """Manages ~/.boxagent-local/ directory: sessions, PIDs."""

    def __init__(
        self,
        local_dir: Path | str,
        codex_sessions_dir: Path | str | None = None,
    ):
        self._local_dir = Path(local_dir)
        if codex_sessions_dir is None:
            codex_sessions_dir = Path.home() / ".codex" / "sessions"
        self._codex_sessions_dir = Path(codex_sessions_dir).expanduser()

    @property
    def local_dir(self) -> Path:
        return self._local_dir

    def _ensure_dir(self, subdir: str = "") -> Path:
        """Auto-create directory if needed."""
        path = self._local_dir / subdir if subdir else self._local_dir
        path.mkdir(parents=True, exist_ok=True)
        return path

    # --- Session tracking ---

    def _sessions_path(self) -> Path:
        self._ensure_dir()
        return self._local_dir / "sessions.yaml"

    def _load_sessions(self) -> dict:
        path = self._sessions_path()
        if not path.exists():
            return {}
        with open(path) as f:
            data = yaml.safe_load(f)
        return data or {}

    def _save_sessions(self, data: dict) -> None:
        path = self._sessions_path()
        with open(path, "w") as f:
            yaml.safe_dump(data, f)

    def save_session(
        self,
        bot_id: str,
        session_id: str,
        *,
        preview: str = "",
        backend: str = "",
        workspace: str = "",
    ) -> None:
        sessions = self._load_sessions()
        scoped_key = self._session_scope_key(
            bot_id,
            backend=backend,
            workspace=workspace,
        )
        sessions[scoped_key or bot_id] = session_id
        self._save_sessions(sessions)
        self._remember_session(
            bot_id,
            session_id,
            preview=preview,
            backend=backend,
            workspace=workspace,
        )

    def load_session(
        self,
        bot_id: str,
        *,
        backend: str = "",
        workspace: str = "",
    ) -> str | None:
        sessions = self._load_sessions()
        scoped_key = self._session_scope_key(
            bot_id,
            backend=backend,
            workspace=workspace,
        )
        value = sessions.get(scoped_key or bot_id)
        return value if isinstance(value, str) and value else None

    def clear_session(
        self,
        bot_id: str,
        *,
        backend: str = "",
        workspace: str = "",
    ) -> None:
        sessions = self._load_sessions()
        scoped_key = self._session_scope_key(
            bot_id,
            backend=backend,
            workspace=workspace,
        )
        sessions.pop(scoped_key or bot_id, None)
        self._save_sessions(sessions)

    def _session_history_path(self) -> Path:
        self._ensure_dir()
        return self._local_dir / "session_history.yaml"

    def _load_session_history(self) -> dict:
        path = self._session_history_path()
        if not path.exists():
            return {}
        with open(path) as f:
            data = yaml.safe_load(f)
        return data or {}

    def _save_session_history(self, data: dict) -> None:
        path = self._session_history_path()
        with open(path, "w") as f:
            yaml.safe_dump(data, f)

    def _remember_session(
        self,
        bot_id: str,
        session_id: str,
        *,
        preview: str = "",
        backend: str = "",
        workspace: str = "",
    ) -> None:
        history = self._load_session_history()
        entries = self._normalize_session_history_entries(
            history.get(bot_id, [])
        )
        normalized_workspace = self._normalize_workspace_path(workspace)
        # Update existing entry or create new one
        existing = None
        for entry in entries:
            if entry["session_id"] == session_id:
                existing = entry
                break
        entries = [
            entry
            for entry in entries
            if entry["session_id"] != session_id
        ]
        new_entry: dict[str, object] = {
            "session_id": session_id,
            "saved_at": int(time.time()),
        }
        if backend:
            new_entry["backend"] = backend
        elif existing and existing.get("backend"):
            new_entry["backend"] = existing["backend"]
        if normalized_workspace:
            new_entry["workspace"] = normalized_workspace
        elif existing and existing.get("workspace"):
            new_entry["workspace"] = existing["workspace"]
        # Keep existing preview if no new one provided
        if preview:
            compact = " ".join(preview.split())
            new_entry["preview"] = compact[:90] + "..." if len(compact) > 90 else compact
        elif existing and existing.get("preview"):
            new_entry["preview"] = existing["preview"]
        entries.insert(0, new_entry)
        history[bot_id] = entries[:50]
        self._save_session_history(history)

    def list_session_history(
        self,
        bot_id: str,
        *,
        backend: str = "",
        workspace: str = "",
    ) -> list[dict[str, object]]:
        entries = self._normalize_session_history_entries(
            self._load_session_history().get(bot_id, [])
        )
        if backend or workspace:
            entries = [
                entry
                for entry in entries
                if self._matches_session_scope(
                    entry,
                    backend=backend,
                    workspace=workspace,
                )
            ]
        if entries:
            return entries

        current = self.load_session(
            bot_id,
            backend=backend,
            workspace=workspace,
        )
        if current:
            entry: dict[str, object] = {"session_id": current}
            if backend:
                entry["backend"] = backend
            normalized_workspace = self._normalize_workspace_path(workspace)
            if normalized_workspace:
                entry["workspace"] = normalized_workspace
            return [entry]
        return []

    def _normalize_session_history_entries(
        self,
        entries: object,
    ) -> list[dict[str, object]]:
        if not isinstance(entries, list):
            return []

        normalized: list[dict[str, object]] = []
        for entry in entries:
            if isinstance(entry, str):
                normalized.append({"session_id": entry})
                continue
            if not isinstance(entry, dict):
                continue
            session_id = entry.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                continue
            normalized_entry: dict[str, object] = {"session_id": session_id}
            saved_at = entry.get("saved_at")
            if isinstance(saved_at, int | float):
                normalized_entry["saved_at"] = int(saved_at)
            preview = entry.get("preview")
            if isinstance(preview, str) and preview:
                normalized_entry["preview"] = preview
            backend = entry.get("backend")
            if isinstance(backend, str) and backend:
                normalized_entry["backend"] = backend
            workspace = entry.get("workspace")
            if isinstance(workspace, str) and workspace:
                normalized_entry["workspace"] = workspace
            normalized.append(normalized_entry)
        return normalized

    def _session_scope_key(
        self,
        bot_id: str,
        *,
        backend: str = "",
        workspace: str = "",
    ) -> str | None:
        normalized_workspace = self._normalize_workspace_path(workspace)
        if not backend and not normalized_workspace:
            return None
        return f"{bot_id}::{backend}::{normalized_workspace or ''}"

    def _matches_session_scope(
        self,
        entry: dict[str, object],
        *,
        backend: str = "",
        workspace: str = "",
    ) -> bool:
        if backend:
            entry_backend = entry.get("backend")
            if not isinstance(entry_backend, str) or entry_backend != backend:
                return False

        normalized_workspace = self._normalize_workspace_path(workspace)
        if normalized_workspace:
            entry_workspace = entry.get("workspace")
            if not isinstance(entry_workspace, str):
                return False
            if self._normalize_workspace_path(entry_workspace) != normalized_workspace:
                return False

        return True

    # --- Codex local session history ---

    def list_codex_session_history(
        self,
        workspace: str,
        limit: int | None = 10,
    ) -> list[dict[str, object]]:
        """List Codex rollout files for the current workspace."""
        sessions_dir = self._codex_sessions_dir
        if not sessions_dir.exists():
            return []

        workspace_path = self._normalize_workspace_path(workspace)
        try:
            paths = sorted(
                sessions_dir.rglob("rollout-*.jsonl"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return []

        entries: list[dict[str, object]] = []
        seen_session_ids: set[str] = set()
        for path in paths:
            entry = self._read_codex_session_listing_entry(path)
            if not entry:
                continue

            entry_cwd = entry.get("cwd")
            if workspace_path is not None:
                if not isinstance(entry_cwd, str):
                    continue
                if self._normalize_workspace_path(entry_cwd) != workspace_path:
                    continue

            session_id = str(entry["session_id"])
            if session_id in seen_session_ids:
                continue
            seen_session_ids.add(session_id)
            entries.append(entry)
            if limit is not None and len(entries) >= limit:
                break

        return entries

    def build_codex_resume_context(
        self,
        session_path: Path | str,
        max_messages: int = 12,
    ) -> str:
        """Build a compact prompt block from a local Codex rollout."""
        path = Path(session_path).expanduser()
        if not path.exists():
            return ""

        session_id = ""
        cwd = ""
        saved_at = int(path.stat().st_mtime)
        recovered_messages: list[tuple[str, str]] = []
        saw_aborted_turn = False

        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if not isinstance(item, dict):
                        continue

                    item_type = item.get("type")
                    if item_type == "session_meta":
                        payload = item.get("payload")
                        if isinstance(payload, dict):
                            meta_id = payload.get("id")
                            if isinstance(meta_id, str) and meta_id:
                                session_id = meta_id
                            meta_cwd = payload.get("cwd")
                            if isinstance(meta_cwd, str) and meta_cwd:
                                cwd = meta_cwd
                            parsed_saved_at = self._parse_codex_timestamp(
                                payload.get("timestamp") or item.get("timestamp")
                            )
                            if parsed_saved_at is not None:
                                saved_at = parsed_saved_at
                        continue

                    if item_type != "event_msg":
                        continue

                    payload = item.get("payload")
                    if not isinstance(payload, dict):
                        continue

                    event_type = payload.get("type")
                    role = ""
                    text = ""
                    if event_type == "user_message":
                        role = "User"
                        raw_text = payload.get("message")
                        if isinstance(raw_text, str):
                            text = raw_text
                    elif event_type == "agent_message":
                        role = "Assistant"
                        raw_text = payload.get("message")
                        if isinstance(raw_text, str):
                            text = raw_text
                    elif event_type == "task_complete":
                        role = "Assistant"
                        raw_text = payload.get("last_agent_message")
                        if isinstance(raw_text, str):
                            text = raw_text
                    elif event_type == "turn_aborted":
                        saw_aborted_turn = True

                    if not role or not text.strip():
                        continue

                    compact_text = self._shorten_codex_message(text, 500)
                    if recovered_messages:
                        last_role, last_text = recovered_messages[-1]
                        if last_role == role and last_text == compact_text:
                            continue
                    recovered_messages.append((role, compact_text))
        except OSError:
            return ""

        if not recovered_messages:
            return ""

        selected_messages = self._select_codex_resume_messages(
            recovered_messages, max_messages
        )
        saved_at_text = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(saved_at),
        )

        lines = [
            "[Recovered previous Codex session]",
            "This is a soft restore built from a local Codex rollout log.",
            "Before answering the next user message, internalize the recovered transcript below as prior context.",
            "Continue in a new session and do not claim the original thread was natively resumed.",
        ]
        if session_id:
            lines.append(f"Source session: {session_id}")
        if cwd:
            lines.append(f"Workspace: {cwd}")
        lines.append(f"Captured at: {saved_at_text}")
        if saw_aborted_turn:
            lines.append("Note: the previous session included an interrupted turn.")
        lines.append("")
        lines.append("Recovered transcript:")
        for role, text in selected_messages:
            lines.append(f"{role}: {text}")
        lines.append("[End recovered session]")
        return "\n".join(lines)

    def _read_codex_session_listing_entry(
        self, path: Path
    ) -> dict[str, object] | None:
        session_id = ""
        cwd = ""
        preview = ""
        saved_at = int(path.stat().st_mtime)

        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if not isinstance(item, dict):
                        continue

                    item_type = item.get("type")
                    if item_type == "session_meta":
                        payload = item.get("payload")
                        if not isinstance(payload, dict):
                            continue
                        meta_id = payload.get("id")
                        if isinstance(meta_id, str) and meta_id:
                            session_id = meta_id
                        meta_cwd = payload.get("cwd")
                        if isinstance(meta_cwd, str) and meta_cwd:
                            cwd = meta_cwd
                        parsed_saved_at = self._parse_codex_timestamp(
                            payload.get("timestamp") or item.get("timestamp")
                        )
                        if parsed_saved_at is not None:
                            saved_at = parsed_saved_at
                    elif item_type == "event_msg":
                        payload = item.get("payload")
                        if not isinstance(payload, dict):
                            continue
                        if payload.get("type") != "user_message":
                            continue
                        message = payload.get("message")
                        if isinstance(message, str) and message.strip():
                            preview = self._shorten_codex_message(message, 90)

                    if session_id and preview:
                        break
        except OSError:
            return None

        if not session_id:
            return None

        entry: dict[str, object] = {
            "session_id": session_id,
            "path": str(path),
            "saved_at": saved_at,
            "backend": "codex-cli",
        }
        if cwd:
            entry["cwd"] = cwd
        if preview:
            entry["preview"] = preview
        return entry

    def _normalize_workspace_path(self, workspace: str) -> str | None:
        if not workspace:
            return None
        try:
            return str(Path(workspace).expanduser().resolve())
        except OSError:
            return str(Path(workspace).expanduser())

    def _parse_codex_timestamp(self, value: object) -> int | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None

    def _shorten_codex_message(self, text: str, limit: int) -> str:
        compact = " ".join(text.split())
        if len(compact) <= limit:
            return compact
        return compact[: max(limit - 3, 1)].rstrip() + "..."

    def _select_codex_resume_messages(
        self,
        messages: list[tuple[str, str]],
        max_messages: int,
    ) -> list[tuple[str, str]]:
        if len(messages) <= max_messages:
            return messages

        head_count = min(2, len(messages))
        tail_count = max(max_messages - head_count - 1, 1)
        omitted = len(messages) - head_count - tail_count
        selected = list(messages[:head_count])
        if omitted > 0:
            selected.append(
                ("System", f"... {omitted} earlier messages omitted ...")
            )
        selected.extend(messages[-tail_count:])
        return selected[:max_messages]
