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

    Naive — original path components containing ``-`` are ambiguous (e.g. the
    encoded form ``-a-b-c`` could be ``/a/b/c`` or ``/a/b-c`` or ``/a-b/c``).
    Use :func:`_lookup_cwd` for an exact answer when a session JSONL is
    available; this is the last-resort fallback.
    """
    if not encoded:
        return ""
    if encoded.startswith("-"):
        return "/" + encoded[1:].replace("-", "/")
    return encoded.replace("-", "/")


def _read_cwd_from_jsonl(path: Path) -> str:
    """Best-effort: extract the original `cwd` field from a session JSONL.

    Claude writes ``"cwd": "/abs/path"`` on most user/event records. The first
    line that has it is enough — it's stable for the whole session.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 50:  # session metadata lives at the top; bail out
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
    """Resolve the real cwd for a Claude project dir by reading any JSONL inside."""
    if not project_dir.is_dir():
        return ""
    files = sorted(
        (p for p in project_dir.iterdir() if p.suffix == ".jsonl" and p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for f in files:
        cwd = _read_cwd_from_jsonl(f)
        if cwd:
            return cwd
    # Last resort — use the naive decode
    return _decode_cwd(project_dir.name)


def _project_label(encoded: str, cwd: str = "") -> str:
    """Short human label — last path segment of the actual cwd."""
    decoded = cwd or _decode_cwd(encoded)
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
        cwd = _lookup_cwd(entry)
        out.append({
            "encoded": entry.name,
            "label": _project_label(entry.name, cwd),
            "cwd": cwd,
            "session_count": len(sessions),
            "last_ts": last_mtime,
        })
    out.sort(key=lambda x: x["last_ts"], reverse=True)
    return out


def project_cwd(encoded: str, claude_dir: Path | None = None) -> str:
    """Return the resolved cwd for a single project (used at resume time)."""
    base = claude_dir or default_claude_projects_dir()
    return _lookup_cwd(base / encoded)


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
