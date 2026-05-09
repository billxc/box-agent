"""ClaudeAgentHistory — wraps ``claude_agent_sdk`` for Claude transcripts.

Serves both ``claude-cli`` and ``agent-sdk-claude`` since they share
``~/.claude/projects/`` on disk and we delegate everything to the SDK.

``project_id`` here IS the resolved cwd path (the SDK's natural key).
Old code paths used an encoded directory name like
``-Users-bill-code-box-agent``; we don't expose that anymore — callers
hand the cwd back.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from claude_agent_sdk import (
    SDKSessionInfo,
    SessionMessage,
    get_session_info as sdk_get_session_info,
    get_session_messages,
    list_sessions as sdk_list_sessions,
)

from boxagent.history.protocol import Message, ProjectInfo, SessionInfo

logger = logging.getLogger(__name__)


class ClaudeAgentHistory:
    """``AgentHistory`` impl built on top of ``claude_agent_sdk``."""

    # ── Public API ────────────────────────────────────────────────

    async def list_projects(self) -> list[ProjectInfo]:
        return await asyncio.to_thread(self._list_projects_sync)

    async def list_sessions(self, project_id: str) -> list[SessionInfo]:
        return await asyncio.to_thread(self._list_sessions_sync, project_id)

    async def get_session_info(
        self, session_id: str, project_id: str = "",
    ) -> SessionInfo | None:
        info = await asyncio.to_thread(
            sdk_get_session_info, session_id, project_id or None,
        )
        if info is None:
            return None
        return self._sdk_to_session_info(info, project_id or info.cwd or "")

    async def read_messages(
        self, session_id: str, project_id: str = "",
    ) -> list[Message]:
        try:
            messages = await asyncio.to_thread(
                get_session_messages, session_id, project_id or None,
            )
        except Exception as e:
            logger.warning(
                "SDK get_session_messages failed sid=%s cwd=%s: %s",
                session_id, project_id, e,
            )
            return []
        return self._convert_messages(messages)

    # ── Sync internals (run via to_thread) ───────────────────────

    def _list_projects_sync(self) -> list[ProjectInfo]:
        try:
            infos = sdk_list_sessions()
        except Exception as e:
            logger.warning("SDK list_sessions(global) failed: %s", e)
            return []
        # Bucket by cwd. SDK already gives us cwd per session — no
        # filesystem scan needed.
        buckets: dict[str, list[SDKSessionInfo]] = {}
        for info in infos:
            cwd = info.cwd or ""
            buckets.setdefault(cwd, []).append(info)
        out: list[ProjectInfo] = []
        for cwd, items in buckets.items():
            last_ms = max((i.last_modified or 0) for i in items)
            label = (cwd or "(no cwd)").rstrip("/").rsplit("/", 1)[-1] or cwd
            out.append(ProjectInfo(
                project_id=cwd,
                label=label or "(no cwd)",
                cwd=cwd,
                session_count=len(items),
                last_ts=last_ms / 1000.0 if last_ms else 0.0,
            ))
        out.sort(key=lambda p: p.last_ts, reverse=True)
        return out

    def _list_sessions_sync(self, project_id: str) -> list[SessionInfo]:
        try:
            infos = sdk_list_sessions(
                directory=project_id or None,
                include_worktrees=False,
            )
        except Exception as e:
            logger.warning("SDK list_sessions(%s) failed: %s", project_id, e)
            return []
        out = [self._sdk_to_session_info(info, project_id) for info in infos]
        out.sort(key=lambda s: s.last_ts, reverse=True)
        return out

    @staticmethod
    def _sdk_to_session_info(info: SDKSessionInfo, project_id: str) -> SessionInfo:
        first_user = (info.first_prompt or "").strip().split("\n", 1)[0][:120]
        return SessionInfo(
            session_id=info.session_id,
            project_id=project_id or info.cwd or "",
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

    # ── Message conversion ───────────────────────────────────────

    def _convert_messages(self, messages: list[SessionMessage]) -> list[Message]:
        out: list[Message] = []
        prev_was_tool_result = False
        for msg in messages:
            records = self._extract_records(msg)
            has_tool = any(r.role in ("tool_call", "tool_result") for r in records)
            # Heuristic: a user message with no tool blocks immediately
            # following a tool_result is a "skill output" coming back to
            # the user (Claude Code's send_to_user pattern).
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
                    args=args or {},
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
