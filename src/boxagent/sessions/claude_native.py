"""Discovery + reading of Claude CLI's native session JSONL files.

Claude Code stores each session at ``~/.claude/projects/<encoded-cwd>/<uuid>.jsonl``
where ``<encoded-cwd>`` is the absolute working directory with ``/`` replaced by ``-``.
Each line is a JSON object — user/assistant messages, tool calls, queue events, etc.

This module provides three lazy entry points used by the web UI:
- ``list_projects()``           — one row per encoded project dir.
- ``list_sessions(encoded)``    — one row per ``.jsonl`` file in that project.
- ``read_messages(encoded, sid)`` — full parsed user/assistant transcript.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def default_claude_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _decode_cwd(encoded: str) -> str:
    """Reverse ``/`` → ``-`` encoding used by Claude Code project dirs.

    Claude does not escape genuine ``-`` in paths, so this is best-effort:
    we replace each leading run/segment with ``/`` to recover the cwd.
    """
    if not encoded:
        return ""
    # The original cwd starts with "/", which becomes a leading "-".
    if encoded.startswith("-"):
        return "/" + encoded[1:].replace("-", "/")
    return encoded.replace("-", "/")


def _project_label(encoded: str) -> str:
    """Short human label — last path segment of the decoded cwd."""
    decoded = _decode_cwd(encoded)
    base = decoded.rstrip("/").rsplit("/", 1)[-1]
    return base or encoded


def list_projects(claude_dir: Path | None = None) -> list[dict]:
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
        out.append({
            "encoded": entry.name,
            "label": _project_label(entry.name),
            "cwd": _decode_cwd(entry.name),
            "session_count": len(sessions),
            "last_ts": last_mtime,
        })
    out.sort(key=lambda x: x["last_ts"], reverse=True)
    return out


def list_sessions(encoded: str, claude_dir: Path | None = None) -> list[dict]:
    base = (claude_dir or default_claude_projects_dir()) / encoded
    if not base.is_dir():
        return []
    out: list[dict] = []
    for path in base.iterdir():
        if path.suffix != ".jsonl" or not path.is_file():
            continue
        info = _summarize(path)
        if info is None:
            continue
        out.append(info)
    out.sort(key=lambda x: x.get("last_ts") or 0, reverse=True)
    return out


def _summarize(path: Path) -> dict | None:
    """Quickly summarize a session file for the picker."""
    session_id = path.stem
    first_user = ""
    msg_count = 0
    last_ts = path.stat().st_mtime
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                t = rec.get("type")
                if t == "user" or t == "assistant":
                    msg_count += 1
                    if t == "user" and not first_user:
                        first_user = _extract_text(rec).strip().split("\n", 1)[0][:120]
    except OSError as e:
        logger.debug("claude_native: failed to read %s: %s", path, e)
        return None
    return {
        "session_id": session_id,
        "first_user": first_user,
        "message_count": msg_count,
        "last_ts": last_ts,
    }


def read_messages(encoded: str, session_id: str, claude_dir: Path | None = None) -> list[dict]:
    """Return parsed user/assistant messages for a single Claude session."""
    base = (claude_dir or default_claude_projects_dir()) / encoded / f"{session_id}.jsonl"
    if not base.is_file():
        return []
    out: list[dict] = []
    try:
        with open(base, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                t = rec.get("type")
                if t not in ("user", "assistant"):
                    continue
                text = _extract_text(rec)
                if not text:
                    continue
                ts = _parse_ts(rec.get("timestamp"))
                out.append({"role": t, "text": text, "ts": ts})
    except OSError as e:
        logger.debug("claude_native: failed to read %s: %s", base, e)
    return out


def _extract_text(rec: dict) -> str:
    """Pull the human-readable text out of a Claude JSONL message record."""
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                txt = item.get("text") or ""
                if txt:
                    parts.append(txt)
        return "\n".join(parts)
    return ""


def _parse_ts(value) -> float:
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        # ISO 8601 with optional Z
        try:
            from datetime import datetime
            v = value.replace("Z", "+00:00")
            return datetime.fromisoformat(v).timestamp()
        except Exception:
            return 0.0
    return 0.0
