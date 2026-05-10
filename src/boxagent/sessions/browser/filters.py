"""Filtering, search, and small display helpers (truncate, relative time)."""

from __future__ import annotations

import time as _time
from pathlib import Path

from .loaders import _resolve_session_path


def _matches_all_words(entry: dict, words: list[str]) -> bool:
    """Return True if every word matches at least one searchable field."""
    fields = [
        (entry.get("summary") or "").lower(),
        (entry.get("firstPrompt") or "").lower(),
        (entry.get("preview") or "").lower(),
        (entry.get("project") or "").lower(),
        (entry.get("projectPath") or "").lower(),
        (entry.get("backend") or "").lower(),
        (entry.get("model") or "").lower(),
    ]
    for word in words:
        if not any(word in f for f in fields):
            return False
    return True


def _filter_sessions(
    entries: list[dict],
    *,
    query: str = "",
    days: int | None = None,
    backend: str = "",
    bot: str = "",
    cwd: str = "",
    cwd_search: str = "",
) -> list[dict]:
    """Apply filters to the unified session list."""
    result = entries

    if cwd:
        cwd_norm = cwd.rstrip("/") + "/"
        result = [
            e for e in result
            if (e.get("projectPath") or "") == cwd.rstrip("/")
            or (e.get("projectPath") or "").startswith(cwd_norm)
        ]

    if cwd_search:
        cl = cwd_search.lower()
        result = [
            e for e in result
            if cl in (e.get("projectPath") or "").lower()
        ]

    if days is not None and days > 0:
        cutoff = _time.time() - days * 86400
        result = [e for e in result if (e.get("modified_ts") or 0) >= cutoff]

    if backend:
        bl = backend.lower()
        result = [e for e in result if bl in (e.get("backend") or "").lower()]

    if bot:
        bl = bot.lower()
        result = [e for e in result if bl in (e.get("bot") or "").lower()]

    if query:
        words = query.lower().split()
        result = [e for e in result if _matches_all_words(e, words)]

    return result


def _find_by_id_prefix(entries: list[dict], prefix: str) -> list[dict]:
    """Find sessions whose sessionId starts with the given hex prefix."""
    pl = prefix.lower()
    return [e for e in entries if e.get("sessionId", "").lower().startswith(pl)]


def _grep_sessions(entries: list[dict], pattern: str) -> list[dict]:
    """Full-text search: keep only sessions whose JSONL content contains *pattern*.

    Searches Claude CLI sessions (~/.claude/projects/*/{sid}.jsonl) and
    Codex sessions (path stored in entry['_codex_path']).
    """
    pl = pattern.lower()
    result = []
    for e in entries:
        sid = e.get("sessionId", "")
        path_str = e.get("_codex_path", "")
        path = Path(path_str) if path_str else _resolve_session_path(sid)
        if not path or not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            if pl in content.lower():
                result.append(e)
        except OSError:
            continue
    return result


def _truncate(text: str, limit: int) -> str:
    s = " ".join(str(text).split())
    if len(s) > limit:
        return s[: limit - 3] + "..."
    return s


def _relative_time(ts: int) -> str:
    """Format a Unix timestamp as a relative time string."""
    if not ts:
        return ""
    diff = int(_time.time()) - ts
    if diff < 0:
        diff = 0
    if diff < 60:
        return "just now"
    if diff < 3600:
        m = diff // 60
        return f"{m}m ago"
    if diff < 86400:
        h = diff // 3600
        return f"{h}h ago"
    d = diff // 86400
    return f"{d}d ago"
