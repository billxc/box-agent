"""Discovery + reading of Claude CLI native session JSONL files.

Thin adapter around ``claude_agent_sdk``: the heavy lifting (jsonl parsing,
session-file scanning, per-cwd indexing) is delegated to the SDK. We keep
the encoded↔cwd mapping helpers because the web UI uses the encoded
project directory name in URLs, while the SDK takes a directory path.

Web UI handlers in ``transports/web/server.py`` call:

- ``list_projects()`` — picker rows, one per encoded project dir.
- ``project_cwd(encoded)`` — resolve cwd at resume time.
- ``list_sessions(encoded)`` — picker rows, one per session inside a project.
- ``read_messages(encoded, sid)`` — full transcript (text + tool blocks).
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import (
    SDKSessionInfo,
    SessionMessage,
    get_session_messages,
)
from claude_agent_sdk import (
    list_sessions as sdk_list_sessions,
)

logger = logging.getLogger(__name__)


# ── Path helpers ──────────────────────────────────────────────────────


def default_claude_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _read_cwd_from_jsonl_head(path: Path) -> str:
    """Best-effort: extract the original ``cwd`` field from a session JSONL.

    Claude writes ``"cwd": "/abs/path"`` on most user/event records. The
    first line that has it within the first 50 records is enough — it's
    stable for the whole session. SDK doesn't expose this directly, so
    we keep the helper.
    """
    import json

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


def _lookup_cwd(project_dir: Path) -> str:
    """Resolve the real cwd for a Claude project dir."""
    if not project_dir.is_dir():
        return ""
    files = sorted(
        (p for p in project_dir.iterdir() if p.suffix == ".jsonl" and p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for f in files:
        cwd = _read_cwd_from_jsonl_head(f)
        if cwd:
            return cwd
    # Fallback — naive decode
    name = project_dir.name
    if name.startswith("-"):
        return "/" + name[1:].replace("-", "/")
    return name.replace("-", "/")


def project_cwd(encoded: str, claude_dir: Path | None = None) -> str:
    """Return the resolved cwd for a single encoded project dir."""
    base = claude_dir or default_claude_projects_dir()
    return _lookup_cwd(base / encoded)


# ── Project listing ──────────────────────────────────────────────────


def list_projects(claude_dir: Path | None = None) -> list[dict]:
    """Group sessions by encoded project dir.

    SDK's ``list_sessions()`` returns flat (one row per session). Web UI
    expects one row per project with a session count, so we group by
    encoded dir on disk.
    """
    base = claude_dir or default_claude_projects_dir()
    if not base.is_dir():
        return []
    out: list[dict] = []
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        sessions = [p for p in entry.iterdir() if p.suffix == ".jsonl" and p.is_file()]
        if not sessions:
            continue
        last_mtime = max(p.stat().st_mtime for p in sessions)
        cwd = _lookup_cwd(entry)
        label = (cwd or entry.name).rstrip("/").rsplit("/", 1)[-1] or entry.name
        out.append({
            "encoded": entry.name,
            "label": label,
            "cwd": cwd,
            "session_count": len(sessions),
            "last_ts": last_mtime,
        })
    out.sort(key=lambda x: x["last_ts"], reverse=True)
    return out


# ── Session listing (per project) ────────────────────────────────────


def list_sessions(encoded: str, claude_dir: Path | None = None) -> list[dict]:
    """List sessions for a single encoded project. SDK-backed."""
    cwd = project_cwd(encoded, claude_dir)
    if not cwd:
        return []
    try:
        infos = sdk_list_sessions(directory=cwd, include_worktrees=False)
    except Exception as e:
        logger.warning("SDK list_sessions failed for cwd=%s: %s", cwd, e)
        return []
    out: list[dict] = []
    for info in infos:
        out.append(_session_info_to_dict(info))
    out.sort(key=lambda x: x.get("last_ts") or 0, reverse=True)
    return out


def _session_info_to_dict(info: SDKSessionInfo) -> dict:
    """Map :class:`SDKSessionInfo` into the picker-friendly dict shape.

    Web UI consumes: session_id, first_user, message_count, last_ts.
    We additionally surface the SDK's richer fields (custom_title,
    git_branch, tag, created_at) for callers that want them.
    """
    first_user = (info.first_prompt or "").strip().split("\n", 1)[0][:120]
    return {
        "session_id": info.session_id,
        "first_user": first_user,
        "message_count": 0,  # SDK doesn't expose this cheaply; UI tolerates 0
        "last_ts": (info.last_modified / 1000.0) if info.last_modified else 0.0,
        # Extra SDK metadata (new in this Phase 1)
        "summary": info.summary,
        "custom_title": info.custom_title,
        "git_branch": info.git_branch,
        "tag": info.tag,
        "created_at": (info.created_at / 1000.0) if info.created_at else 0.0,
    }


# ── Message reading (transcript replay) ──────────────────────────────


def read_messages(
    encoded: str, session_id: str, claude_dir: Path | None = None,
) -> list[dict]:
    """Return parsed records for a single Claude session — SDK-backed.

    Records are normalised to one of:
      {"role": "user"|"assistant", "text": str, "ts": float}
      {"role": "tool_call", "tool_id": str, "name": str, "args": dict, "ts": float}
      {"role": "tool_result", "tool_id": str, "ok": bool, "summary": str,
       "error": str, "ts": float}
      {"role": "skill_output", "text": str, "ts": float}

    A single SDK ``SessionMessage`` may carry several content blocks (text +
    tool_use); we split them into separate records so the frontend can
    render each one.

    Note on ``ts``: SDK ``SessionMessage`` doesn't expose per-message
    timestamps. Records get ``ts=0.0`` and rely on the SDK's chronological
    return order for sorting.
    """
    cwd = project_cwd(encoded, claude_dir)
    if not cwd:
        return []
    try:
        messages = get_session_messages(session_id, directory=cwd)
    except Exception as e:
        logger.warning(
            "SDK get_session_messages failed for sid=%s cwd=%s: %s",
            session_id, cwd, e,
        )
        return []

    out: list[dict] = []
    prev_was_tool_result = False
    for msg in messages:
        records = _extract_records(msg)
        has_tool = any(r["role"] in ("tool_call", "tool_result") for r in records)
        # Heuristic: a user message with no tool blocks immediately after a
        # tool_result is the model's "skill output" coming back to the user.
        if msg.type == "user" and not has_tool and prev_was_tool_result:
            for r in records:
                if r["role"] == "user":
                    r["role"] = "skill_output"
        out.extend(records)
        prev_was_tool_result = msg.type == "user" and has_tool
    return out


def _extract_records(msg: SessionMessage) -> list[dict]:
    """Split one SDK ``SessionMessage`` into ordered display records."""
    raw = msg.message if isinstance(msg.message, dict) else None
    if raw is None:
        return []
    content = raw.get("content")
    role = msg.type
    ts = _msg_timestamp(raw)

    if isinstance(content, str):
        return [{"role": role, "text": content, "ts": ts}] if content else []
    if not isinstance(content, list):
        return []

    out: list[dict] = []
    text_buf: list[str] = []

    def _flush_text():
        if text_buf:
            joined = "\n".join(p for p in text_buf if p)
            if joined:
                out.append({"role": role, "text": joined, "ts": ts})
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
            out.append({
                "role": "tool_call",
                "tool_id": item.get("id", "") or "",
                "name": item.get("name", "") or "",
                "args": item.get("input") if isinstance(item.get("input"), dict) else {},
                "ts": ts,
            })
        elif block_type == "tool_result":
            _flush_text()
            summary, error = _stringify_tool_result(item.get("content"))
            is_error = bool(item.get("is_error"))
            out.append({
                "role": "tool_result",
                "tool_id": item.get("tool_use_id", "") or "",
                "ok": not is_error,
                "summary": "" if is_error else summary,
                "error": (error or summary) if is_error else "",
                "ts": ts,
            })
    _flush_text()
    return out


def _msg_timestamp(raw: dict) -> float:
    """Try a few common timestamp keys on the raw API message dict."""
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


def _stringify_tool_result(raw) -> tuple[str, str]:
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
