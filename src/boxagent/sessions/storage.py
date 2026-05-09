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
        # main_sessions cache + lock — concurrent heartbeat/peer/webui calls
        # would otherwise race on the yaml file and lose the pinned chat_id.
        import threading
        self._main_lock = threading.Lock()
        self._main_cache: dict | None = None

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

        # Chain old session_ids when this chat_id rotates to a new one
        # (e.g. after /compact). The full chain lets transcript readers
        # stitch the conversation back together across compactions.
        old = sessions.get(key)
        prev_chain: list[str] = []
        old_sid = ""
        if isinstance(old, dict):
            old_sid = str(old.get("session_id", "") or "")
            raw_prev = old.get("previous_session_ids") or []
            if isinstance(raw_prev, list):
                prev_chain = [str(s) for s in raw_prev if isinstance(s, str) and s]
        elif isinstance(old, str):
            old_sid = old
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

    def clear_session(self, bot_id: str, chat_id: str = "") -> None:
        sessions = self._load_sessions()
        sessions.pop(self._session_key(bot_id, chat_id), None)
        self._save_sessions(sessions)

    # --- Main session per bot/workgroup ---
    # Persists which chat_id is the bot's "main" session. Heartbeat ticks
    # and incoming peer messages dispatch into this chat_id so they share
    # the admin's primary conversation. Web UI can update it; if unset,
    # the first heartbeat/peer event mints a new chat_id and pins it here.
    #
    # Cache + atomic-write because heartbeat / peer recv / webui set_main
    # can all hit set_main_chat_id concurrently. Without atomic writes a
    # mid-write read sees a truncated file → safe_load returns None →
    # callers think "no main pinned" and mint a fresh `main-<bot>-<ts>`,
    # so the main session appears to flap on every event.

    def _main_sessions_path(self) -> Path:
        self._ensure_dir()
        return self._local_dir / "main_sessions.yaml"

    def _load_main_sessions(self) -> dict:
        if self._main_cache is not None:
            return self._main_cache
        path = self._main_sessions_path()
        if not path.exists():
            self._main_cache = {}
            return self._main_cache
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
            self._main_cache = data or {}
        except Exception as e:
            logger.warning("main_sessions.yaml read failed (%s); treating as empty", e)
            self._main_cache = {}
        return self._main_cache

    def _save_main_sessions(self, data: dict) -> None:
        # Atomic write: dump to temp file, then rename. POSIX rename is
        # atomic so concurrent readers either see the old or new file,
        # never a half-written one.
        path = self._main_sessions_path()
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w") as f:
            yaml.safe_dump(data, f)
        tmp.replace(path)
        self._main_cache = data

    def get_main_chat_id(self, bot_id: str) -> str:
        with self._main_lock:
            return str(self._load_main_sessions().get(bot_id) or "")

    def get_or_create_main_chat_id(self, bot_id: str) -> str:
        """Return the persisted main chat_id for a bot, minting one if unset.

        Used for heartbeat ticks and incoming peer messages so they always
        land in the admin's designated main session.
        """
        cid = self.get_main_chat_id(bot_id)
        if cid:
            return cid
        cid = f"main-{bot_id}-{int(time.time())}"
        self.set_main_chat_id(bot_id, cid)
        return cid

    def set_main_chat_id(self, bot_id: str, chat_id: str) -> None:
        with self._main_lock:
            data = dict(self._load_main_sessions())  # copy to avoid in-place mutation of cache
            old = data.get(bot_id, "")
            if old == chat_id:
                return  # no-op, don't churn the file
            if chat_id:
                data[bot_id] = chat_id
            else:
                data.pop(bot_id, None)
            self._save_main_sessions(data)
            # Caller stack helps diagnose "main keeps flipping" — without it
            # we can't tell whether heartbeat, peer recv, or webui set it.
            import traceback
            stack = traceback.extract_stack(limit=6)
            caller = " > ".join(f"{Path(f.filename).name}:{f.lineno}" for f in stack[-5:-1])
            logger.warning(
                "main_chat_id changed bot=%s old=%s new=%s caller=%s",
                bot_id, old or "(empty)", chat_id or "(empty)", caller,
            )

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

