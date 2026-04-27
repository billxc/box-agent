"""CLI subcommands for listing Claude CLI sessions on this machine."""

import json
import os
import re
import sys
import time as _time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from boxagent.utils import safe_print as _safe_print


CLAUDE_DIR = Path.home() / ".claude"


def build_sessions_parser(subparsers) -> None:
    """Register 'sessions' subcommand with sub-subparsers."""
    sessions = subparsers.add_parser("sessions", help="Search and list sessions")
    sessions_sub = sessions.add_subparsers(dest="sessions_cmd")

    ls = sessions_sub.add_parser("list", help="List all sessions")
    ls.add_argument(
        "query", nargs="*", default=[],
        help=(
            "Search query tokens: keywords, --all, cwd:X, grep:X, "
            "Nd (days), backend:X, bot:X, pN (page)"
        ),
    )
    ls.add_argument(
        "--json", dest="output_json", action="store_true", default=False,
        help="Output as JSON",
    )
    ls.add_argument(
        "--workspace", default="",
        help="Project directory to scope results (default: show all)",
    )
    ls.set_defaults(func=sessions_list)


# ---------------------------------------------------------------------------
# Data loading — Claude CLI sessions
# ---------------------------------------------------------------------------

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


def _parse_jsonl_metadata(jsonl_file: Path) -> dict | None:
    """Extract minimal metadata from a session .jsonl file."""
    sid = jsonl_file.stem

    first_prompt = ""
    created = ""
    project_path = ""
    message_count = 0

    try:
        with open(jsonl_file, encoding="utf-8") as f:
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

    # Use file mtime as modified time
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


# ---------------------------------------------------------------------------
# Unified session loading — merge all sources
# ---------------------------------------------------------------------------

def _load_all_unified_sessions(
    storage: object | None = None,
    workspace: str = "",
) -> list[dict]:
    """Merge Claude CLI sessions, BoxAgent session_history, and Codex sessions.

    Returns a list of dicts with a common schema, sorted by modified desc.
    """
    # 1. Load Claude CLI sessions
    claude_entries = _load_claude_sessions()

    # Build unified entries keyed by session_id
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

    # 2. Overlay BoxAgent session_history (if storage available)
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
                # Overlay metadata
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
                # Update modified_ts if saved_at is newer
                saved_at = e.get("saved_at")
                if isinstance(saved_at, int | float) and saved_at > (entry.get("modified_ts") or 0):
                    entry["modified_ts"] = int(saved_at)
            else:
                # New entry from BoxAgent only
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

    # 3. Overlay Codex sessions (if storage available)
    if storage is not None:
        try:
            codex_entries = storage.list_codex_session_history(workspace or "", limit=None)
        except Exception:
            codex_entries = []
        for e in codex_entries:
            sid = str(e.get("session_id", ""))
            if not sid:
                continue
            if sid in unified:
                entry = unified[sid]
                if not entry["backend"] and e.get("backend"):
                    entry["backend"] = str(e["backend"])
                if not entry["preview"] and e.get("preview"):
                    entry["preview"] = str(e["preview"])
                cwd = str(e.get("cwd", "")) if e.get("cwd") else ""
                if cwd and not entry["projectPath"]:
                    entry["projectPath"] = cwd
                    entry["project"] = Path(cwd).name
                if e.get("path") and not entry.get("_codex_path"):
                    entry["_codex_path"] = str(e["path"])
            else:
                saved_at = e.get("saved_at")
                cwd = str(e.get("cwd", "")) if e.get("cwd") else ""
                unified[sid] = {
                    "sessionId": sid,
                    "project": Path(cwd).name if cwd else "",
                    "projectPath": cwd,
                    "summary": "",
                    "firstPrompt": "",
                    "preview": str(e.get("preview", "")),
                    "messageCount": 0,
                    "modified_ts": int(saved_at) if isinstance(saved_at, int | float) else 0,
                    "backend": str(e.get("backend", "")),
                    "model": "",
                    "bot": "",
                    "_codex_path": str(e.get("path", "")),
                }

    # Sort by modified_ts desc
    result = sorted(unified.values(), key=lambda e: e.get("modified_ts") or 0, reverse=True)
    return result


def _parse_iso_to_ts(iso_str: str) -> int:
    """Parse an ISO 8601 string to a Unix timestamp. Returns 0 on failure."""
    if not iso_str:
        return 0
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# Token parsing
# ---------------------------------------------------------------------------

_RE_PAGE = re.compile(r"^p(\d+)$", re.IGNORECASE)
_RE_DAYS = re.compile(r"^(\d+)d$", re.IGNORECASE)
_RE_BACKEND = re.compile(r"^backend:(.+)$", re.IGNORECASE)
_RE_BOT = re.compile(r"^bot:(.+)$", re.IGNORECASE)
_RE_CWD = re.compile(r"^cwd:(.+)$", re.IGNORECASE)
_RE_GREP = re.compile(r"^grep:(.+)$", re.IGNORECASE)
_RE_HEX = re.compile(r"^[0-9a-f]{4,}$", re.IGNORECASE)


def parse_session_tokens(raw: str) -> dict:
    """Parse the argument string after ``/sessions``.

    Returns a dict with keys:
        page: int (1-based)
        days: int | None (time filter in days)
        backend: str (empty if not filtered)
        bot: str (empty if not filtered)
        cwd_search: str (empty if not filtered, substring match on projectPath)
        grep: str (empty if not filtered, full-text search on session content)
        id_prefix: str (empty if not a hex prefix lookup)
        query: str (remaining search words joined)
        all: bool (True to show all sessions, ignoring cwd filter)
    """
    tokens = raw.split()
    page = 1
    days: int | None = None
    backend = ""
    bot = ""
    cwd_search = ""
    grep = ""
    id_prefix = ""
    all_flag = False
    query_parts: list[str] = []

    page_set = False
    days_set = False

    for token in tokens:
        if token == "--all":
            all_flag = True
            continue

        m = _RE_PAGE.match(token)
        if m and not page_set:
            page = int(m.group(1))
            page_set = True
            continue

        m = _RE_DAYS.match(token)
        if m and not days_set:
            days = int(m.group(1))
            days_set = True
            continue

        m = _RE_BACKEND.match(token)
        if m and not backend:
            backend = m.group(1)
            continue

        m = _RE_BOT.match(token)
        if m and not bot:
            bot = m.group(1)
            continue

        m = _RE_CWD.match(token)
        if m and not cwd_search:
            cwd_search = m.group(1)
            continue

        m = _RE_GREP.match(token)
        if m and not grep:
            grep = m.group(1)
            continue

        # Hex prefix is checked later against actual sessions
        # For now just collect as query part
        query_parts.append(token)

    query = " ".join(query_parts)
    return {
        "page": page,
        "days": days,
        "backend": backend,
        "bot": bot,
        "cwd_search": cwd_search,
        "grep": grep,
        "id_prefix": id_prefix,
        "query": query,
        "all": all_flag,
    }


# ---------------------------------------------------------------------------
# Filtering and search
# ---------------------------------------------------------------------------

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

    # CWD filter (exact/prefix): session projectPath must be under (or equal to) the given cwd
    if cwd:
        cwd_norm = cwd.rstrip("/") + "/"
        result = [
            e for e in result
            if (e.get("projectPath") or "") == cwd.rstrip("/")
            or (e.get("projectPath") or "").startswith(cwd_norm)
        ]

    # CWD search (substring): fuzzy match on projectPath
    if cwd_search:
        cl = cwd_search.lower()
        result = [
            e for e in result
            if cl in (e.get("projectPath") or "").lower()
        ]

    # Time filter
    if days is not None and days > 0:
        cutoff = _time.time() - days * 86400
        result = [e for e in result if (e.get("modified_ts") or 0) >= cutoff]

    # Backend filter
    if backend:
        bl = backend.lower()
        result = [e for e in result if bl in (e.get("backend") or "").lower()]

    # Bot filter
    if bot:
        bl = bot.lower()
        result = [e for e in result if bl in (e.get("bot") or "").lower()]

    # Text search: multi-word AND, multi-field OR
    if query:
        words = query.lower().split()
        result = [e for e in result if _matches_all_words(e, words)]

    return result


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


def _find_by_id_prefix(entries: list[dict], prefix: str) -> list[dict]:
    """Find sessions whose sessionId starts with the given hex prefix."""
    pl = prefix.lower()
    return [e for e in entries if e.get("sessionId", "").lower().startswith(pl)]


def _resolve_session_path(sid: str) -> Path | None:
    """Find the JSONL file for a Claude CLI session ID."""
    projects_dir = CLAUDE_DIR / "projects"
    if not projects_dir.is_dir():
        return None
    for jsonl_file in projects_dir.glob(f"*/{sid}.jsonl"):
        return jsonl_file
    return None


def _grep_sessions(entries: list[dict], pattern: str) -> list[dict]:
    """Full-text search: keep only sessions whose JSONL content contains *pattern*.

    Searches Claude CLI sessions (~/.claude/projects/*/{sid}.jsonl) and
    Codex sessions (path stored in entry['_codex_path']).
    """
    pl = pattern.lower()
    result = []
    for e in entries:
        sid = e.get("sessionId", "")
        # Try Codex path first (stored during loading)
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


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

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


def format_sessions_list(
    query: str = "",
    page: int = 1,
    page_size: int = 5,
    storage: object | None = None,
    workspace: str = "",
) -> str:
    """Return a formatted string listing unified sessions.

    This is the main entry point for the ``/sessions`` bot command.
    Parses *query* for special tokens (pN, Nd, backend:X, bot:X, hex prefix).
    """
    parsed = parse_session_tokens(query)
    if parsed["page"] > 1:
        page = parsed["page"]

    entries = _load_all_unified_sessions(storage=storage, workspace=workspace)

    # Check for hex prefix match first
    remaining_query = parsed["query"]
    if remaining_query and _RE_HEX.match(remaining_query) and " " not in remaining_query:
        matches = _find_by_id_prefix(entries, remaining_query)
        if matches:
            return _format_id_match(matches)
        # No match — treat as regular search

    # Determine cwd filter: default to workspace unless --all or cwd_search
    cwd_filter = "" if (parsed["all"] or parsed["cwd_search"]) else workspace

    # Apply filters
    filtered = _filter_sessions(
        entries,
        query=remaining_query,
        days=parsed["days"],
        backend=parsed["backend"],
        bot=parsed["bot"],
        cwd=cwd_filter,
        cwd_search=parsed["cwd_search"],
    )

    # Full-text search (applied after metadata filters to limit I/O)
    if parsed["grep"]:
        filtered = _grep_sessions(filtered, parsed["grep"])

    total = len(filtered)
    if total == 0:
        # Build a descriptive "no results" message
        parts = []
        if remaining_query:
            parts.append(f'"{remaining_query}"')
        if parsed["days"]:
            parts.append(f"in {parsed['days']}d")
        if parsed["backend"]:
            parts.append(f"backend:{parsed['backend']}")
        if parsed["bot"]:
            parts.append(f"bot:{parsed['bot']}")
        if parsed["cwd_search"]:
            parts.append(f"cwd:{parsed['cwd_search']}")
        if parsed["grep"]:
            parts.append(f"grep:{parsed['grep']}")
        if cwd_filter:
            parts.append(f"in {Path(cwd_filter).name}")
        if parts:
            hint = " (use `--all` for all projects)" if cwd_filter else ""
            return f"No sessions matching {' '.join(parts)}.{hint}"
        return "No sessions found. (use `--all` for all projects)" if cwd_filter else "No sessions found."

    total_pages = (total + page_size - 1) // page_size
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    page_entries = filtered[start : start + page_size]

    # Header
    header_parts = []
    if remaining_query:
        header_parts.append(f'"{remaining_query}"')
    if parsed["days"]:
        header_parts.append(f"in {parsed['days']}d")
    if parsed["backend"]:
        header_parts.append(f"backend:{parsed['backend']}")
    if parsed["bot"]:
        header_parts.append(f"bot:{parsed['bot']}")
    if parsed["cwd_search"]:
        header_parts.append(f"cwd:{parsed['cwd_search']}")
    if parsed["grep"]:
        header_parts.append(f"grep:{parsed['grep']}")

    if cwd_filter:
        scope = f"in {Path(cwd_filter).name}"
    elif parsed["cwd_search"]:
        scope = f"cwd~{parsed['cwd_search']}"
    else:
        scope = "all projects"
    range_str = f"{start + 1}-{start + len(page_entries)} / {total}"
    if header_parts:
        header = f"Sessions matching {' '.join(header_parts)} · {scope} ({range_str})"
    else:
        header = f"Sessions · {scope} ({range_str})"

    lines = [f"**{header}**\n"]

    for idx, e in enumerate(page_entries, start + 1):
        sid = e.get("sessionId", "?")
        project = e.get("project", "")
        modified_ts = e.get("modified_ts") or 0
        msgs = e.get("messageCount") or 0
        backend_str = e.get("backend") or ""
        rel_time = _relative_time(modified_ts)

        # Line 1: metadata
        meta_parts = []
        if project:
            meta_parts.append(project)
        if rel_time:
            meta_parts.append(rel_time)
        if msgs:
            meta_parts.append(f"{msgs} msgs")
        if backend_str:
            meta_parts.append(backend_str)
        lines.append(f"{idx}. {' · '.join(meta_parts)}")

        # Line 2: summary/preview
        summary = _truncate(
            e.get("summary", "") or e.get("firstPrompt", "") or e.get("preview", ""),
            70,
        )
        if summary:
            lines.append(f"   {summary}")

        # Line 3: resume command
        lines.append(f"   `/resume {sid}`")
        lines.append("")  # blank line

    # Navigation hint
    base_args = []
    if parsed["all"]:
        base_args.append("--all")
    if remaining_query:
        base_args.append(remaining_query)
    if parsed["days"]:
        base_args.append(f"{parsed['days']}d")
    if parsed["backend"]:
        base_args.append(f"backend:{parsed['backend']}")
    if parsed["bot"]:
        base_args.append(f"bot:{parsed['bot']}")
    if parsed["cwd_search"]:
        base_args.append(f"cwd:{parsed['cwd_search']}")
    if parsed["grep"]:
        base_args.append(f"grep:{parsed['grep']}")
    base = " ".join(base_args)

    hints = []
    if page > 1:
        prev_cmd = f"/sessions {base} p{page - 1}".strip()
        hints.append(f"`{prev_cmd}` <-")
    if page < total_pages:
        next_cmd = f"/sessions {base} p{page + 1}".strip()
        hints.append(f"`{next_cmd}` ->")
    if hints:
        lines.append(" | ".join(hints))

    return "\n".join(lines)


def _format_id_match(matches: list[dict]) -> str:
    """Format output for session ID prefix matches."""
    lines = []
    for e in matches:
        sid = e.get("sessionId", "?")
        project = e.get("project", "")
        modified_ts = e.get("modified_ts") or 0
        msgs = e.get("messageCount") or 0
        backend_str = e.get("backend") or ""
        rel_time = _relative_time(modified_ts)

        meta_parts = []
        if project:
            meta_parts.append(project)
        if rel_time:
            meta_parts.append(rel_time)
        if msgs:
            meta_parts.append(f"{msgs} msgs")
        if backend_str:
            meta_parts.append(backend_str)
        lines.append(f"{' · '.join(meta_parts)}")

        summary = _truncate(
            e.get("summary", "") or e.get("firstPrompt", "") or e.get("preview", ""),
            70,
        )
        if summary:
            lines.append(f"{summary}")
        lines.append(f"`/resume {sid}`")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI subcommand handler
# ---------------------------------------------------------------------------

def sessions_list(args) -> None:
    """List all sessions (unified: Claude CLI + BoxAgent history + Codex)."""
    from boxagent.config import load_config

    # Try to construct Storage for unified loading
    storage = None
    try:
        cfg = load_config()
        local_dir = cfg.get("local_dir", "")
        if local_dir:
            from boxagent.sessions.storage import Storage
            storage = Storage(local_dir)
    except Exception:
        pass

    query_str = " ".join(getattr(args, "query", []))
    workspace = getattr(args, "workspace", "")

    if getattr(args, "output_json", False):
        entries = _load_all_unified_sessions(storage=storage, workspace=workspace)
        parsed = parse_session_tokens(query_str)

        cwd_filter = "" if (parsed["all"] or parsed["cwd_search"]) else workspace
        filtered = _filter_sessions(
            entries,
            query=parsed["query"],
            days=parsed["days"],
            backend=parsed["backend"],
            bot=parsed["bot"],
            cwd=cwd_filter,
            cwd_search=parsed["cwd_search"],
        )
        if parsed["grep"]:
            filtered = _grep_sessions(filtered, parsed["grep"])

        _safe_print(json.dumps(filtered, indent=2, ensure_ascii=False))
        return

    text = format_sessions_list(
        query=query_str,
        storage=storage,
        workspace=workspace,
    )
    _safe_print(text)
