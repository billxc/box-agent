"""Storage helpers — sessions.yaml management."""

import logging
import time
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


class Storage:
    """Manages ~/.boxagent-local/ directory: sessions, PIDs."""

    def __init__(self, local_dir: Path | str):
        self._local_dir = Path(local_dir)

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

    def _session_key(self, bot_id: str, chat_id: str = "") -> str:
        """Build the sessions.yaml key, optionally scoped by chat_id."""
        if chat_id:
            return f"{bot_id}:{chat_id}"
        return bot_id

    def save_session(self, bot_id: str, session_id: str, *, preview: str = "", backend: str = "", chat_id: str = "", model: str = "", workspace: str = "") -> None:
        sessions = self._load_sessions()
        key = self._session_key(bot_id, chat_id)
        entry: dict[str, object] = {"session_id": session_id}

        # Chain old_entry session_ids when this chat_id rotates to a new one
        # (e.g. after /compact). The full chain lets transcript readers
        # stitch the conversation back together across compactions.
        old_entry = sessions.get(key)
        prev_chain: list[str] = []
        old_sid = ""
        if isinstance(old_entry, dict):
            old_sid = str(old_entry.get("session_id", "") or "")
            raw_prev = old_entry.get("previous_session_ids") or []
            if isinstance(raw_prev, list):
                prev_chain = [str(s) for s in raw_prev if isinstance(s, str) and s]
        elif isinstance(old_entry, str):
            old_sid = old_entry
        if old_sid and old_sid != session_id and old_sid not in prev_chain:
            prev_chain.insert(0, old_sid)
        if prev_chain:
            entry["previous_session_ids"] = prev_chain[:20]  # cap to avoid unbounded growth

        if workspace:
            entry["workspace"] = workspace
        if model:
            entry["model"] = model
        if backend:
            entry["backend"] = backend
        sessions[key] = entry
        self._save_sessions(sessions)
        self._remember_session(bot_id, session_id, preview=preview, backend=backend, model=model, workspace=workspace)

    def load_session(self, bot_id: str, chat_id: str = "") -> dict | str | None:
        """Load session data for a bot/chat.

        Returns a dict with session_id/workspace/model/backend if saved
        in the new format, a plain session_id string for legacy entries,
        or None if not found.
        """
        return self._load_sessions().get(self._session_key(bot_id, chat_id))

    def list_chat_sessions(self, bot_id: str) -> list[dict]:
        """Enumerate every persisted chat_id for a given bot.

        Reads sessions.yaml (which stores `{bot}:{chat_id}` → entry) and returns
        one record per chat_id for this bot, regardless of which channel
        (telegram / web) created it.
        """
        prefix = f"{bot_id}:"
        out: list[dict] = []
        for key, entry in self._load_sessions().items():
            if not isinstance(key, str) or not key.startswith(prefix):
                continue
            chat_id = key[len(prefix):]
            if not chat_id:
                continue
            session_id = ""
            workspace = ""
            model = ""
            backend = ""
            if isinstance(entry, dict):
                session_id = str(entry.get("session_id", "") or "")
                workspace = str(entry.get("workspace", "") or "")
                model = str(entry.get("model", "") or "")
                backend = str(entry.get("backend", "") or "")
            elif isinstance(entry, str):
                session_id = entry
            out.append({
                "chat_id": chat_id,
                "session_id": session_id,
                "workspace": workspace,
                "model": model,
                "backend": backend,
            })
        return out

    def clear_session(self, bot_id: str, chat_id: str = "", *, preserve_chain: bool = False) -> None:
        """Clear the active session_id for a bot/chat.

        With ``preserve_chain=True`` the current session_id is pushed onto
        ``previous_session_ids`` (and other entry fields kept) so that
        ``/compact`` can start a fresh Claude session while history readers
        can still walk back into the old_entry transcript. Without it, the entry
        is dropped entirely (``/new`` semantics).
        """
        key = self._session_key(bot_id, chat_id)
        sessions = self._load_sessions()
        if not preserve_chain:
            sessions.pop(key, None)
            self._save_sessions(sessions)
            return

        old_entry = sessions.get(key)
        if not isinstance(old_entry, dict):
            sessions.pop(key, None)
            self._save_sessions(sessions)
            return

        old_sid = str(old_entry.get("session_id", "") or "")
        prev_chain: list[str] = []
        raw_prev = old_entry.get("previous_session_ids") or []
        if isinstance(raw_prev, list):
            prev_chain = [str(s) for s in raw_prev if isinstance(s, str) and s]
        if old_sid and old_sid not in prev_chain:
            prev_chain.insert(0, old_sid)

        new_entry = {k: v for k, v in old_entry.items() if k not in ("session_id", "previous_session_ids")}
        if prev_chain:
            new_entry["previous_session_ids"] = prev_chain[:20]
        sessions[key] = new_entry
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

    def _remember_session(self, bot_id: str, session_id: str, *, preview: str = "", backend: str = "", model: str = "", workspace: str = "") -> None:
        history = self._load_session_history()
        entries = self._normalize_session_history_entries(
            history.get("_global", [])
        )
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
            "bot": bot_id,
        }
        if backend:
            new_entry["backend"] = backend
        elif existing and existing.get("backend"):
            new_entry["backend"] = existing["backend"]
        if model:
            new_entry["model"] = model
        elif existing and existing.get("model"):
            new_entry["model"] = existing["model"]
        if workspace:
            new_entry["workspace"] = workspace
        elif existing and existing.get("workspace"):
            new_entry["workspace"] = existing["workspace"]
        # Keep existing preview if no new one provided
        if preview:
            compact = " ".join(preview.split())
            new_entry["preview"] = compact[:90] + "..." if len(compact) > 90 else compact
        elif existing and existing.get("preview"):
            new_entry["preview"] = existing["preview"]
        entries.insert(0, new_entry)
        history["_global"] = entries[:50]
        self._save_session_history(history)

    def list_session_history(self, bot_id: str = "") -> list[dict[str, object]]:
        """List session history. Returns global history (all bots).

        Falls back to legacy per-bot entries if no global list exists.
        """
        history = self._load_session_history()
        entries = self._normalize_session_history_entries(
            history.get("_global", [])
        )
        if entries:
            return entries

        # Fallback: try legacy per-bot format
        if bot_id:
            legacy = self._normalize_session_history_entries(
                history.get(bot_id, [])
            )
            if legacy:
                return legacy

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
            for key in ("preview", "backend", "model", "workspace", "bot"):
                val = entry.get(key)
                if isinstance(val, str) and val:
                    normalized_entry[key] = val
            normalized.append(normalized_entry)
        return normalized

