"""Loaders — pull session metadata via boxagent.history and merge with
BoxAgent's own annotations.

Three data sources merged into a unified DTO:

1. Claude SDK transcripts — ``boxagent.history.claude.ClaudeAgentHistory``
2. BoxAgent's session_history.yaml — ``Storage.list_session_history``
   (annotates entries with backend label / model / bot, plus stand-alone
   entries for sessions BoxAgent saved that have no on-disk transcript yet)
3. Codex rollout JSONLs — ``boxagent.history.codex.CodexAgentHistory``

Reading is fully delegated to the ``boxagent.history`` package; this
module owns only the merge + DTO shape that the legacy ``/sessions`` UI
and ``/resume`` consumers expect.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from boxagent.history.claude import ClaudeAgentHistory
from boxagent.history.codex import CodexAgentHistory
from boxagent.history.protocol import SessionInfo

if TYPE_CHECKING:
    from boxagent.sessions.storage import Storage


CLAUDE_DIR = Path.home() / ".claude"
CODEX_DIR = Path.home() / ".codex" / "sessions"


def _parse_iso_to_ts(iso_str: str) -> int:
    """Parse an ISO 8601 string to a Unix timestamp. Returns 0 on failure."""
    if not iso_str:
        return 0
    try:
        datetime = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(datetime.timestamp())
    except (ValueError, TypeError):
        return 0


def _resolve_session_path(sid: str) -> Path | None:
    """Find the JSONL file for a Claude CLI session ID.

    Used by /sessions grep_pattern filter to read transcript content. The history
    layer doesn't expose paths, so we still scan ``~/.claude/projects/``
    here.
    """
    projects_dir = CLAUDE_DIR / "projects"
    if not projects_dir.is_dir():
        return None
    for jsonl_file in projects_dir.glob(f"*/{sid}.jsonl"):
        return jsonl_file
    return None


def _claude_session_to_unified(s: SessionInfo) -> dict:
    """Convert a ClaudeAgentHistory.SessionInfo to the legacy unified dict."""
    return {
        "sessionId": s.session_id,
        "project": Path(s.project_id or s.cwd).name if (s.project_id or s.cwd) else "",
        "projectPath": s.project_id or s.cwd or "",
        "summary": s.summary or "",
        "firstPrompt": s.first_user or "",
        "preview": "",
        "messageCount": s.message_count,
        "modified_ts": int(s.last_ts) if s.last_ts else 0,
        "backend": "",  # filled in by Storage overlay if BoxAgent saved this
        "model": "",
        "bot": "",
    }


def _load_all_unified_sessions(
    storage: "Storage | None" = None,
    workspace: str = "",
) -> list[dict]:
    """Merge Claude + BoxAgent session_history + Codex sessions.

    Returns a list of dicts with a common schema, sorted by modified desc.
    """
    unified: dict[str, dict] = {}

    # 1) Claude — via ClaudeAgentHistory (SDK)
    claude_history = ClaudeAgentHistory()
    try:
        for project in claude_history.list_projects_sync():
            for s in claude_history.list_sessions_sync(project.project_id):
                if not s.session_id:
                    continue
                unified[s.session_id] = _claude_session_to_unified(s)
    except Exception:
        # SDK can fail (e.g. Claude not installed); leave Claude empty.
        pass

    # 2) BoxAgent session_history.yaml — overlays annotations onto Claude
    # entries above, and adds standalone entries for sessions BoxAgent
    # tracked but that have no on-disk transcript yet.
    if storage is not None:
        try:
            box_entries = storage.list_session_history()
        except Exception:
            box_entries = []
        for e in box_entries:
            sid = str(e.get("session_id", ""))
            if not sid:
                continue
            if sid in unified:
                entry = unified[sid]
                if e.get("backend"):
                    entry["backend"] = str(e["backend"])
                if e.get("model"):
                    entry["model"] = str(e["model"])
                if e.get("bot"):
                    entry["bot"] = str(e["bot"])
                if e.get("preview"):
                    entry["preview"] = str(e["preview"])
                if e.get("workspace") and not entry["projectPath"]:
                    ws = str(e["workspace"])
                    entry["projectPath"] = ws
                    entry["project"] = Path(ws).name
                saved_at = e.get("saved_at")
                if isinstance(saved_at, int | float) and saved_at > (entry.get("modified_ts") or 0):
                    entry["modified_ts"] = int(saved_at)
            else:
                saved_at = e.get("saved_at")
                ws = str(e.get("workspace", "")) if e.get("workspace") else ""
                unified[sid] = {
                    "sessionId": sid,
                    "project": Path(ws).name if ws else "",
                    "projectPath": ws,
                    "summary": "",
                    "firstPrompt": "",
                    "preview": str(e.get("preview", "")),
                    "messageCount": 0,
                    "modified_ts": int(saved_at) if isinstance(saved_at, int | float) else 0,
                    "backend": str(e.get("backend", "")),
                    "model": str(e.get("model", "")),
                    "bot": str(e.get("bot", "")),
                }

    # 3) Codex — via CodexAgentHistory
    codex_history: CodexAgentHistory | None = None
    try:
        codex_history = CodexAgentHistory(codex_dir=CODEX_DIR)
        codex_sessions = codex_history.list_sessions_sync(workspace or "")
    except Exception:
        codex_sessions = []
        codex_history = None
    for s in codex_sessions:
        sid = s.session_id
        if not sid:
            continue
        # Resolve the file path lazily (only used by grep_pattern filter); this
        # walks the rollout dir, but it's cached during this single call.
        path = ""
        if codex_history is not None:
            try:
                p = codex_history.get_session_path_sync(sid)
                if p is not None:
                    path = str(p)
            except Exception:
                pass
        if sid in unified:
            entry = unified[sid]
            if not entry["backend"]:
                entry["backend"] = "codex-cli"
            if not entry["preview"] and s.first_user:
                entry["preview"] = s.first_user
            if s.cwd and not entry["projectPath"]:
                entry["projectPath"] = s.cwd
                entry["project"] = Path(s.cwd).name
            if path and not entry.get("_codex_path"):
                entry["_codex_path"] = path
        else:
            unified[sid] = {
                "sessionId": sid,
                "project": Path(s.cwd).name if s.cwd else "",
                "projectPath": s.cwd,
                "summary": "",
                "firstPrompt": "",
                "preview": s.first_user,
                "messageCount": 0,
                "modified_ts": int(s.last_ts) if s.last_ts else 0,
                "backend": "codex-cli",
                "model": "",
                "bot": "",
                "_codex_path": path,
            }

    out = list(unified.values())
    out.sort(key=lambda e: e.get("modified_ts") or 0, reverse=True)
    return out
