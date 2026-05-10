"""CodexAgentHistory — reads ``~/.codex/sessions/rollout-*.jsonl``.

Codex CLI writes each session as a JSONL rollout file. ``project_id``
in this implementation is the cwd path: callers can either pass an
explicit cwd, or pass empty string to list sessions from every cwd.
The full transcript reader here is intentionally minimal — Codex
session files are richer than what the web UI's history replay can
render today, so we focus on the picker-shape (session_id / preview /
cwd / saved_at).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from boxagent.history.protocol import Message, ProjectInfo, SessionInfo

logger = logging.getLogger(__name__)


def _default_codex_sessions_dir() -> Path:
    return Path.home() / ".codex" / "sessions"


class CodexAgentHistory:
    """``AgentHistory`` impl for Codex CLI's ``~/.codex/sessions/`` rollouts."""

    def __init__(self, codex_dir: Path | None = None) -> None:
        self._codex_dir = codex_dir or _default_codex_sessions_dir()

    # ── Public API ────────────────────────────────────────────────

    async def list_projects(self) -> list[ProjectInfo]:
        return await asyncio.to_thread(self._list_projects_sync)

    async def list_sessions(self, project_id: str) -> list[SessionInfo]:
        return await asyncio.to_thread(self._list_sessions_sync, project_id)

    async def get_session_info(
        self, session_id: str, project_id: str = "",
    ) -> SessionInfo | None:
        sessions = await self.list_sessions(project_id)
        for s in sessions:
            if s.session_id == session_id:
                return s
        return None

    async def read_messages(
        self, session_id: str, project_id: str = "",
    ) -> list[Message]:
        return await asyncio.to_thread(self._read_messages_sync, session_id, project_id)

    # ── Codex-specific extensions (NOT part of AgentHistory protocol) ──

    async def get_session_path(self, session_id: str) -> Path | None:
        """Return the JSONL rollout file path for a session, or None.

        Used by the ``sessions_list`` tool's ``grep:`` filter, which
        wants to greppable file paths. Other backends don't expose a
        single-file abstraction so this is codex-only.
        """
        return await asyncio.to_thread(self._find_rollout_for, session_id)

    # ── Sync escape hatches ──
    # The CodexAgentHistory implementation is entirely synchronous
    # under the hood (file I/O on the local rollout dir). Sync callers
    # — like the legacy ``sessions/browser/loaders.py`` driven by the
    # ``sessions_list`` tool — can use these directly to avoid running
    # ``asyncio.run`` from within an already-running event loop.
    # New code should prefer the async API above.

    def list_sessions_sync(self, project_id: str) -> list[SessionInfo]:
        return self._list_sessions_sync(project_id)

    def get_session_path_sync(self, session_id: str) -> Path | None:
        return self._find_rollout_for(session_id)

    # ── Sync internals ───────────────────────────────────────────

    def _list_projects_sync(self) -> list[ProjectInfo]:
        if not self._codex_dir.is_dir():
            return []
        # Bucket entries by cwd; each unique cwd is one ProjectInfo.
        buckets: dict[str, list[tuple[str, float]]] = {}
        for path in self._iter_rollouts():
            entry = self._read_listing_entry(path)
            if not entry:
                continue
            cwd = str(entry.get("cwd") or "")
            saved_at = float(entry.get("saved_at") or 0)
            sid = str(entry.get("session_id") or "")
            if not sid:
                continue
            buckets.setdefault(cwd, []).append((sid, saved_at))
        out: list[ProjectInfo] = []
        for cwd, items in buckets.items():
            last_ts = max(t for _, t in items) if items else 0.0
            label = cwd.rstrip("/").rsplit("/", 1)[-1] or "(no cwd)"
            out.append(ProjectInfo(
                project_id=cwd or "(unknown)",
                label=label,
                cwd=cwd,
                session_count=len(items),
                last_ts=last_ts,
            ))
        out.sort(key=lambda p: p.last_ts, reverse=True)
        return out

    def _list_sessions_sync(self, project_id: str) -> list[SessionInfo]:
        if not self._codex_dir.is_dir():
            return []
        target_cwd = self._normalize(project_id) if project_id else None
        out: list[SessionInfo] = []
        seen: set[str] = set()
        for path in self._iter_rollouts():
            entry = self._read_listing_entry(path)
            if not entry:
                continue
            sid = str(entry.get("session_id") or "")
            if not sid or sid in seen:
                continue
            entry_cwd = str(entry.get("cwd") or "")
            if target_cwd is not None and self._normalize(entry_cwd) != target_cwd:
                continue
            seen.add(sid)
            out.append(SessionInfo(
                session_id=sid,
                project_id=entry_cwd or project_id,
                first_user=str(entry.get("preview") or ""),
                message_count=0,
                last_ts=float(entry.get("saved_at") or 0),
                cwd=entry_cwd,
            ))
        out.sort(key=lambda s: s.last_ts, reverse=True)
        return out

    def _read_messages_sync(
        self, session_id: str, project_id: str,
    ) -> list[Message]:
        # Minimal implementation — find the rollout file for session_id
        # and walk its event_msg payloads. The web UI's transcript replay
        # is currently Claude-only; this returns a flat list of user /
        # assistant text records sufficient for sessions_list previews.
        path = self._find_rollout_for(session_id)
        if path is None:
            return []
        out: list[Message] = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        item = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(item, dict) or item.get("type") != "event_msg":
                        continue
                    payload = item.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    role = (
                        "user" if payload.get("type") == "user_message"
                        else "assistant" if payload.get("type") == "agent_message"
                        else None
                    )
                    if role is None:
                        continue
                    text = payload.get("message") or payload.get("text") or ""
                    if not isinstance(text, str) or not text:
                        continue
                    out.append(Message(role=role, text=text))
        except OSError as e:
            logger.warning("CodexAgentHistory read %s failed: %s", path, e)
        return out

    # ── File-walk helpers ─────────────────────────────────────────

    def _iter_rollouts(self):
        try:
            return sorted(
                self._codex_dir.rglob("rollout-*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return []

    def _find_rollout_for(self, session_id: str) -> Path | None:
        for path in self._iter_rollouts():
            entry = self._read_listing_entry(path)
            if entry and entry.get("session_id") == session_id:
                return path
        return None

    def _read_listing_entry(self, path: Path) -> dict | None:
        """Walk the file head until we have session_id + first user preview.

        Returns None if the file lacks a session_meta block.
        """
        session_id = ""
        cwd = ""
        preview = ""
        try:
            saved_at = int(path.stat().st_mtime)
        except OSError:
            return None
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
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
                        ts = self._parse_ts(
                            payload.get("timestamp") or item.get("timestamp"),
                        )
                        if ts is not None:
                            saved_at = ts
                    elif item_type == "event_msg":
                        payload = item.get("payload")
                        if not isinstance(payload, dict):
                            continue
                        if payload.get("type") != "user_message":
                            continue
                        message = payload.get("message")
                        if isinstance(message, str) and message.strip():
                            preview = self._shorten(message, 90)
                    if session_id and preview:
                        break
        except OSError:
            return None
        if not session_id:
            return None
        return {
            "session_id": session_id,
            "path": str(path),
            "saved_at": saved_at,
            "cwd": cwd,
            "preview": preview,
            "backend": "codex-cli",
        }

    @staticmethod
    def _parse_ts(value) -> int | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None

    @staticmethod
    def _shorten(text: str, limit: int) -> str:
        compact = " ".join(text.split())
        if len(compact) <= limit:
            return compact
        return compact[: max(limit - 3, 1)].rstrip() + "..."

    @staticmethod
    def _normalize(workspace: str) -> str | None:
        if not workspace:
            return None
        try:
            return str(Path(workspace).expanduser().resolve())
        except OSError:
            return str(Path(workspace).expanduser())
