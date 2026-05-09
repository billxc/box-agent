"""Loaders — pull session metadata from disk and merge into a unified list.

Sources merged here:
  1. ~/.claude/projects/<encoded-cwd>/{sessions-index.json, *.jsonl}
  2. BoxAgent's session_history.yaml (via Storage)
  3. Codex rollout JSONLs (via Storage)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from boxagent.sessions.storage import Storage


CLAUDE_DIR = Path.home() / ".claude"
CODEX_DIR = Path.home() / ".codex" / "sessions"


def _parse_iso_to_ts(iso_str: str) -> int:
    """Parse an ISO 8601 string to a Unix timestamp. Returns 0 on failure."""
    if not iso_str:
        return 0
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return 0


def _parse_jsonl_metadata(jsonl_file: Path) -> dict | None:
    """Extract minimal metadata from a session .jsonl file."""
    sid = jsonl_file.stem

    first_prompt = ""
    created = ""
    project_path = ""
    message_count = 0

    try:
        with open(jsonl_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rtype = record.get("type", "")
                if rtype == "user":
                    message_count += 1
                    if not first_prompt:
                        msg = record.get("message", {})
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            first_prompt = content
                        elif isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    first_prompt = block.get("text", "")
                                    break
                    if not created:
                        created = record.get("timestamp", "")
                    if not project_path:
                        project_path = record.get("cwd", "")
                elif rtype == "assistant":
                    message_count += 1
    except OSError:
        return None

    if not message_count:
        return None

    try:
        mtime = os.path.getmtime(jsonl_file)
        modified = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    except OSError:
        modified = created

    return {
        "sessionId": sid,
        "projectPath": project_path,
        "firstPrompt": first_prompt,
        "summary": "",
        "messageCount": message_count,
        "created": created,
        "modified": modified,
    }


def _load_claude_sessions() -> list[dict]:
    """Collect sessions from Claude CLI index files and unindexed .jsonl files."""
    projects_dir = CLAUDE_DIR / "projects"
    if not projects_dir.is_dir():
        return []

    entries: list[dict] = []
    indexed_ids: set[str] = set()

    # Pass 1: load from sessions-index.json (rich metadata)
    for index_file in projects_dir.glob("*/sessions-index.json"):
        try:
            data = json.loads(index_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue

        project_path = data.get("originalPath", "")
        for entry in data.get("entries", []):
            if not isinstance(entry, dict):
                continue
            sid = entry.get("sessionId", "")
            if sid:
                indexed_ids.add(sid)
            entry.setdefault("projectPath", project_path)
            entries.append(entry)

    # Pass 2: scan .jsonl files not covered by any index
    for jsonl_file in projects_dir.glob("*/*.jsonl"):
        sid = jsonl_file.stem
        if sid in indexed_ids:
            continue

        entry = _parse_jsonl_metadata(jsonl_file)
        if entry:
            entries.append(entry)

    return entries


def _resolve_session_path(sid: str) -> Path | None:
    """Find the JSONL file for a Claude CLI session ID."""
    projects_dir = CLAUDE_DIR / "projects"
    if not projects_dir.is_dir():
        return None
    for jsonl_file in projects_dir.glob(f"*/{sid}.jsonl"):
        return jsonl_file
    return None


def _load_all_unified_sessions(
    storage: "Storage | None" = None,
    workspace: str = "",
) -> list[dict]:
    """Merge Claude CLI sessions, BoxAgent session_history, and Codex sessions.

    Returns a list of dicts with a common schema, sorted by modified desc.
    """
    claude_entries = _load_claude_sessions()

    unified: dict[str, dict] = {}

    for e in claude_entries:
        sid = e.get("sessionId", "")
        if not sid:
            continue
        modified_ts = _parse_iso_to_ts(e.get("modified", ""))
        unified[sid] = {
            "sessionId": sid,
            "project": Path(e.get("projectPath", "")).name if e.get("projectPath") else "",
            "projectPath": e.get("projectPath", ""),
            "summary": e.get("summary", ""),
            "firstPrompt": e.get("firstPrompt", ""),
            "preview": "",
            "messageCount": e.get("messageCount", 0),
            "modified_ts": modified_ts,
            "backend": "",
            "model": "",
            "bot": "",
        }

    # Overlay BoxAgent session_history (if storage available)
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

    # Overlay Codex sessions (sync API on CodexAgentHistory; loaders
    # is called from inside an already-running event loop in the
    # sessions_list MCP tool path, so we can't asyncio.run here).
    try:
        from boxagent.history.codex import CodexAgentHistory
        codex_history = CodexAgentHistory(codex_dir=CODEX_DIR)
        codex_sessions = codex_history.list_sessions_sync(workspace or "")
    except Exception:
        codex_sessions = []
        codex_history = None
    for s in codex_sessions:
        sid = s.session_id
        if not sid:
            continue
        # Resolve the file path lazily (only used by grep filter); this
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

    return sorted(unified.values(), key=lambda e: e.get("modified_ts") or 0, reverse=True)
