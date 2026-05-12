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
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    SDKSessionInfo,
    SessionMessage,
    get_session_info as sdk_get_session_info,
    get_session_messages,
    list_sessions as sdk_list_sessions,
    project_key_for_directory,
    rename_session as sdk_rename_session,
)

from boxagent.history.protocol import Message, ProjectInfo, SessionInfo

logger = logging.getLogger(__name__)


class ClaudeAgentHistory:
    """``AgentHistory`` implementation built on top of ``claude_agent_sdk``."""

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

    async def rename_session(
        self, session_id: str, title: str, project_id: str = "",
    ) -> None:
        """Set the SDK ``custom_title`` for a session. Cross-device persistent."""
        await asyncio.to_thread(
            sdk_rename_session, session_id, title, project_id or None,
        )

    async def read_messages(
        self, session_id: str, project_id: str = "",
    ) -> list[Message]:
        # When Claude's native /compact (manual or auto) fires, the new
        # summary + post-compact entries land in the SAME jsonl as the
        # pre-compact ones, but SDK's get_session_messages stops at the
        # compact_boundary entry. Detect that and read raw so the web UI
        # can still surface pre-compact content (yait #89).
        try:
            messages = await asyncio.to_thread(
                self._read_messages_sync, session_id, project_id,
            )
        except Exception as e:
            logger.warning(
                "read_messages failed sid=%s cwd=%s: %s",
                session_id, project_id, e,
            )
            return []
        return self._convert_messages(messages)

    def _read_messages_sync(
        self, session_id: str, project_id: str,
    ) -> list[SessionMessage]:
        jsonl_path = self._jsonl_path_for(session_id, project_id)
        if jsonl_path is not None and self._has_compact_boundary(jsonl_path):
            return self._read_session_raw(jsonl_path)
        return get_session_messages(session_id, project_id or None)

    @staticmethod
    def _has_compact_boundary(jsonl_path: Path) -> bool:
        try:
            with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"compact_boundary"' in line or '"isCompactSummary":true' in line or '"isCompactSummary": true' in line:
                        return True
        except OSError:
            return False
        return False

    @staticmethod
    def _read_session_raw(jsonl_path: Path) -> list[SessionMessage]:
        """Parse a session JSONL into ``SessionMessage`` objects directly.

        Mirrors SDK's ``_is_visible_message`` filter (skip isMeta /
        isSidechain / teamName) but keeps **all** user/assistant entries
        regardless of compact boundaries — that's the whole point of this
        path. ``timestamp`` is attached so ``_msg_timestamp`` orders the
        merged list correctly.
        """
        out: list[SessionMessage] = []
        try:
            with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    entry_type = entry.get("type")
                    if entry_type not in ("user", "assistant"):
                        continue
                    if entry.get("isMeta") or entry.get("isSidechain") or entry.get("teamName"):
                        continue
                    raw_message = entry.get("message")
                    msg = SessionMessage(
                        type=entry_type,  # type: ignore[arg-type]
                        uuid=entry.get("uuid", "") or "",
                        session_id=entry.get("sessionId", "") or "",
                        message=raw_message,
                        parent_tool_use_id=None,
                    )
                    # Attach timestamp/cwd/git_branch the same way the
                    # _sdk_patch monkey patch does for SDK output, so
                    # _msg_timestamp can sort entries chronologically.
                    msg.timestamp = entry.get("timestamp")  # type: ignore[attr-defined]
                    msg.cwd = entry.get("cwd")  # type: ignore[attr-defined]
                    msg.git_branch = entry.get("gitBranch")  # type: ignore[attr-defined]
                    out.append(msg)
        except OSError:
            return []
        return out

    def _jsonl_path_for(self, session_id: str, project_id: str) -> Path | None:
        project_dir = self._project_dir_for(session_id, project_id)
        if project_dir is None:
            return None
        candidate = project_dir / f"{session_id}.jsonl"
        return candidate if candidate.is_file() else None

    async def walk_compact_chain(
        self, session_id: str, project_id: str = "",
    ) -> list[str]:
        """Walk pre-compact ancestor sessions via JSONL ``parentUuid`` linkage.

        SDK's ``get_session_messages`` deliberately stops at compact
        boundaries (``_build_conversation_chain`` ignores
        ``logicalParentUuid``). For full transcript reconstruction we read
        the head ``isCompactSummary`` entry's ``parentUuid`` and locate
        the prior session whose JSONL contains a row with that ``uuid``,
        recursing back to the chain root.

        Returns ancestor session_ids oldest-first, excluding the input
        ``session_id``. Empty if there is no compaction predecessor.
        """
        return await asyncio.to_thread(self._walk_compact_chain_sync, session_id, project_id)

    # ── Sync API for callers already inside an event loop ─────────
    # Mirrors codex.py: ``loaders._load_all_unified_sessions`` runs
    # under the sessions_list MCP tool path which is already inside an
    # asyncio event loop, so it can't ``asyncio.run`` the async API.
    # New code should prefer the async API above.

    def list_projects_sync(self) -> list[ProjectInfo]:
        return self._list_projects_sync()

    def list_sessions_sync(self, project_id: str) -> list[SessionInfo]:
        return self._list_sessions_sync(project_id)

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

    # ── Compact-chain walking ─────────────────────────────────────

    def _project_dir_for(self, session_id: str, project_id: str) -> Path | None:
        """Locate the ``~/.claude/projects/<key>/`` dir holding the session."""
        cwd = project_id
        if not cwd:
            try:
                info = sdk_get_session_info(session_id, None)
            except Exception:
                info = None
            if info is None:
                return None
            cwd = info.cwd or ""
        if not cwd:
            return None
        try:
            key = project_key_for_directory(cwd)
        except Exception:
            return None
        candidate = Path.home() / ".claude" / "projects" / key
        return candidate if candidate.is_dir() else None

    @staticmethod
    def _read_compact_parent_uuid(jsonl_path: Path) -> str:
        """Return ``parentUuid`` of the head ``isCompactSummary`` entry, or ''."""
        try:
            with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("isCompactSummary"):
                        parent = entry.get("parentUuid")
                        return parent if isinstance(parent, str) else ""
                    # First user/assistant entry that isn't a compact summary
                    # means this session is not compaction-derived.
                    if entry.get("type") in ("user", "assistant"):
                        return ""
        except OSError:
            return ""
        return ""

    @staticmethod
    def _find_session_containing_uuid(project_dir: Path, target_uuid: str) -> str:
        """Return the session_id of whichever JSONL contains ``target_uuid``."""
        if not target_uuid:
            return ""
        target_token = f'"uuid":"{target_uuid}"'
        target_token_alt = f'"uuid": "{target_uuid}"'
        try:
            entries = list(project_dir.iterdir())
        except OSError:
            return ""
        for entry in entries:
            if entry.suffix != ".jsonl":
                continue
            try:
                with entry.open("r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if target_token in line or target_token_alt in line:
                            return entry.stem
            except OSError:
                continue
        return ""

    def _walk_compact_chain_sync(self, session_id: str, project_id: str) -> list[str]:
        project_dir = self._project_dir_for(session_id, project_id)
        if project_dir is None:
            return []

        chain: list[str] = []
        seen: set[str] = {session_id}
        current = session_id
        # Bound iterations to avoid pathological cycles or runaway scans.
        for _ in range(20):
            jsonl = project_dir / f"{current}.jsonl"
            if not jsonl.exists():
                break
            parent_uuid = self._read_compact_parent_uuid(jsonl)
            if not parent_uuid:
                break
            prev_sid = self._find_session_containing_uuid(project_dir, parent_uuid)
            if not prev_sid or prev_sid in seen:
                break
            seen.add(prev_sid)
            chain.append(prev_sid)
            current = prev_sid
        chain.reverse()  # oldest-first
        return chain

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
            recap=getattr(info, "recap", "") or "",
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
        ts = self._msg_timestamp(msg, raw)
        cwd = getattr(msg, "cwd", None) or ""
        git_branch = getattr(msg, "git_branch", None) or ""

        def _new(role_: str, **kwargs) -> Message:
            return Message(role=role_, ts=ts, cwd=cwd, git_branch=git_branch, **kwargs)

        if isinstance(content, str):
            return [_new(role, text=content)] if content else []
        if not isinstance(content, list):
            return []

        out: list[Message] = []
        text_buf: list[str] = []

        def _flush_text():
            if text_buf:
                joined = "\n".join(p for p in text_buf if p)
                if joined:
                    out.append(_new(role, text=joined))
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
                out.append(_new(
                    "tool_call",
                    tool_id=item.get("id", "") or "",
                    name=item.get("name", "") or "",
                    args=args or {},
                ))
            elif block_type == "tool_result":
                _flush_text()
                summary, error = self._stringify_tool_result(item.get("content"))
                is_error = bool(item.get("is_error"))
                out.append(_new(
                    "tool_result",
                    tool_id=item.get("tool_use_id", "") or "",
                    ok=not is_error,
                    summary="" if is_error else summary,
                    error=(error or summary) if is_error else "",
                ))
        _flush_text()
        return out

    @staticmethod
    def _msg_timestamp(msg: SessionMessage, raw: dict) -> float:
        # Preferred path: monkey patch in boxagent.history._sdk_patch attaches
        # entry["timestamp"] (ISO 8601 string) onto the SessionMessage.
        patched = getattr(msg, "timestamp", None)
        if isinstance(patched, str) and patched:
            try:
                return datetime.fromisoformat(patched.replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
        elif isinstance(patched, (int, float)):
            return float(patched)
        # Fallback: try the inner API message dict (older SDK versions or
        # patch failure). Keys vary; check the common ones.
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
