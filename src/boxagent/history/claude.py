"""ClaudeAgentHistory — wraps ``claude_agent_sdk`` for Claude transcripts.

Serves both ``claude-cli`` and ``agent-sdk-claude`` backends since they
write to the same on-disk location (``~/.claude/projects/``) and we
delegate listing/reading to the SDK.

``project_id`` is the encoded project directory name (e.g.
``-Users-bill-code-box-agent``). The SDK's API is keyed on the cwd
path, so we look that up via ``project_cwd``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    SDKSessionInfo,
    SessionMessage,
    get_session_messages,
)
from claude_agent_sdk import (
    list_sessions as sdk_list_sessions,
)

from boxagent.history.protocol import Message, ProjectInfo, SessionInfo

logger = logging.getLogger(__name__)


def _default_claude_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


class ClaudeAgentHistory:
    """``AgentHistory`` impl for ``~/.claude/projects/*.jsonl``."""

    def __init__(self, claude_dir: Path | None = None) -> None:
        self._claude_dir = claude_dir or _default_claude_projects_dir()

    # ── Public API ────────────────────────────────────────────────

    async def list_projects(self) -> list[ProjectInfo]:
        return await asyncio.to_thread(self._list_projects_sync)

    async def list_sessions(self, project_id: str) -> list[SessionInfo]:
        return await asyncio.to_thread(self._list_sessions_sync, project_id)

    async def get_session_info(
        self, session_id: str, project_id: str = "",
    ) -> SessionInfo | None:
        sessions = await self.list_sessions(project_id) if project_id else []
        for s in sessions:
            if s.session_id == session_id:
                return s
        return None

    async def read_messages(
        self, session_id: str, project_id: str = "",
    ) -> list[Message]:
        return await asyncio.to_thread(
            self._read_messages_sync, session_id, project_id,
        )

    # Helpers used by callers that still hand around encoded dir names
    # (kept as a transition aid; new callers should use list_projects).
    def project_cwd(self, project_id: str) -> str:
        return self._lookup_cwd(self._claude_dir / project_id)

    # ── Sync internals ───────────────────────────────────────────

    def _list_projects_sync(self) -> list[ProjectInfo]:
        if not self._claude_dir.is_dir():
            return []
        out: list[ProjectInfo] = []
        for entry in self._claude_dir.iterdir():
            if not entry.is_dir():
                continue
            sessions = [
                p for p in entry.iterdir()
                if p.suffix == ".jsonl" and p.is_file()
            ]
            if not sessions:
                continue
            last_mtime = max(p.stat().st_mtime for p in sessions)
            cwd = self._lookup_cwd(entry)
            label = (cwd or entry.name).rstrip("/").rsplit("/", 1)[-1] or entry.name
            out.append(ProjectInfo(
                project_id=entry.name,
                label=label,
                cwd=cwd,
                session_count=len(sessions),
                last_ts=last_mtime,
            ))
        out.sort(key=lambda p: p.last_ts, reverse=True)
        return out

    def _list_sessions_sync(self, project_id: str) -> list[SessionInfo]:
        cwd = self.project_cwd(project_id)
        if not cwd:
            return []
        try:
            infos = sdk_list_sessions(directory=cwd, include_worktrees=False)
        except Exception as e:
            logger.warning("SDK list_sessions failed for cwd=%s: %s", cwd, e)
            return []
        out = [self._sdk_to_session_info(info, project_id) for info in infos]
        out.sort(key=lambda s: s.last_ts, reverse=True)
        return out

    def _read_messages_sync(
        self, session_id: str, project_id: str,
    ) -> list[Message]:
        cwd = self.project_cwd(project_id) if project_id else ""
        try:
            messages = get_session_messages(session_id, directory=cwd or None)
        except Exception as e:
            logger.warning(
                "SDK get_session_messages failed for sid=%s cwd=%s: %s",
                session_id, cwd, e,
            )
            return []
        return self._convert_messages(messages)

    @staticmethod
    def _sdk_to_session_info(info: SDKSessionInfo, project_id: str) -> SessionInfo:
        first_user = (info.first_prompt or "").strip().split("\n", 1)[0][:120]
        return SessionInfo(
            session_id=info.session_id,
            project_id=project_id,
            first_user=first_user,
            message_count=0,  # SDK doesn't expose cheaply
            last_ts=(info.last_modified / 1000.0) if info.last_modified else 0.0,
            created_at=(info.created_at / 1000.0) if info.created_at else 0.0,
            cwd=info.cwd or "",
            summary=info.summary or "",
            custom_title=info.custom_title,
            git_branch=info.git_branch,
            tag=info.tag,
        )

    def _convert_messages(self, messages: list[SessionMessage]) -> list[Message]:
        out: list[Message] = []
        prev_was_tool_result = False
        for msg in messages:
            records = self._extract_records(msg)
            has_tool = any(r.role in ("tool_call", "tool_result") for r in records)
            # Heuristic: a user message with no tool blocks immediately
            # following a tool_result is a "skill output" coming back to
            # the user.
            if msg.type == "user" and not has_tool and prev_was_tool_result:
                for r in records:
                    if r.role == "user":
                        r.role = "skill_output"
            out.extend(records)
            prev_was_tool_result = msg.type == "user" and has_tool
        return out

    def _extract_records(self, msg: SessionMessage) -> list[Message]:
        raw = msg.message if isinstance(msg.message, dict) else None
        if raw is None:
            return []
        content = raw.get("content")
        role = msg.type
        ts = self._msg_timestamp(raw)

        if isinstance(content, str):
            return [Message(role=role, text=content, ts=ts)] if content else []
        if not isinstance(content, list):
            return []

        out: list[Message] = []
        text_buf: list[str] = []

        def _flush_text():
            if text_buf:
                joined = "\n".join(p for p in text_buf if p)
                if joined:
                    out.append(Message(role=role, text=joined, ts=ts))
                text_buf.clear()

        for item in content:
            if not isinstance(item, dict):
                continue
            block_type = item.get("type")
            if block_type == "text":
                txt = item.get("text") or ""
                if txt:
                    text_buf.append(txt)
            elif block_type == "tool_use":
                _flush_text()
                args = item.get("input") if isinstance(item.get("input"), dict) else {}
                out.append(Message(
                    role="tool_call",
                    tool_id=item.get("id", "") or "",
                    name=item.get("name", "") or "",
                    args=args,
                    ts=ts,
                ))
            elif block_type == "tool_result":
                _flush_text()
                summary, error = self._stringify_tool_result(item.get("content"))
                is_error = bool(item.get("is_error"))
                out.append(Message(
                    role="tool_result",
                    tool_id=item.get("tool_use_id", "") or "",
                    ok=not is_error,
                    summary="" if is_error else summary,
                    error=(error or summary) if is_error else "",
                    ts=ts,
                ))
        _flush_text()
        return out

    @staticmethod
    def _msg_timestamp(raw: dict) -> float:
        for key in ("timestamp", "created_at", "ts"):
            v = raw.get(key)
            if v is None:
                continue
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, str):
                try:
                    return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
        return 0.0

    @staticmethod
    def _stringify_tool_result(raw: Any) -> tuple[str, str]:
        if isinstance(raw, str):
            return raw[:200], raw[:200]
        if isinstance(raw, list):
            parts: list[str] = []
            for blk in raw:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    parts.append(blk.get("text") or "")
                elif isinstance(blk, str):
                    parts.append(blk)
            joined = "\n".join(p for p in parts if p)
            return joined[:200], joined[:200]
        return "", ""

    # ── Internal: encoded → cwd lookup ────────────────────────────

    def _lookup_cwd(self, project_dir: Path) -> str:
        if not project_dir.is_dir():
            return ""
        files = sorted(
            (p for p in project_dir.iterdir()
             if p.suffix == ".jsonl" and p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for f in files:
            cwd = self._read_cwd_from_jsonl_head(f)
            if cwd:
                return cwd
        # Fallback — naive decode
        name = project_dir.name
        if name.startswith("-"):
            return "/" + name[1:].replace("-", "/")
        return name.replace("-", "/")

    @staticmethod
    def _read_cwd_from_jsonl_head(path: Path) -> str:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    if i > 50:
                        break
                    line = line.strip()
                    if not line or '"cwd"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    cwd = rec.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
                    payload = rec.get("payload")
                    if isinstance(payload, dict):
                        cwd = payload.get("cwd")
                        if isinstance(cwd, str) and cwd:
                            return cwd
        except OSError:
            pass
        return ""
