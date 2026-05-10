"""format_sessions_list — render the unified session list as text for the
``/sessions`` bot command and CLI subcommand.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .filters import (
    _filter_sessions,
    _find_by_id_prefix,
    _grep_sessions,
    _relative_time,
    _truncate,
)
from .loaders import _load_all_unified_sessions
from .tokens import _RE_HEX, parse_session_tokens

if TYPE_CHECKING:
    from boxagent.sessions.storage import Storage


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


def format_sessions_list(
    query: str = "",
    page: int = 1,
    page_size: int = 5,
    storage: "Storage | None" = None,
    workspace: str = "",
) -> str:
    """Return a formatted string listing unified sessions.

    Main entry point for the ``/sessions`` bot command. Parses *query* for
    special tokens (pN, Nd, backend:X, bot:X, hex prefix).
    """
    parsed = parse_session_tokens(query)
    if parsed["page"] > 1:
        page = parsed["page"]

    entries = _load_all_unified_sessions(storage=storage, workspace=workspace)

    # Hex prefix shortcut
    remaining_query = parsed["query"]
    if remaining_query and _RE_HEX.match(remaining_query) and " " not in remaining_query:
        matches = _find_by_id_prefix(entries, remaining_query)
        if matches:
            return _format_id_match(matches)
        # No match — fall through to regular search

    # Default to current workspace unless --all or cwd_search
    cwd_filter = "" if (parsed["all"] or parsed["cwd_search"]) else workspace

    filtered = _filter_sessions(
        entries,
        query=remaining_query,
        days=parsed["days"],
        backend=parsed["backend"],
        bot=parsed["bot"],
        cwd=cwd_filter,
        cwd_search=parsed["cwd_search"],
    )

    # Full-text search applied after metadata filters to limit I/O
    if parsed["grep"]:
        filtered = _grep_sessions(filtered, parsed["grep"])

    total = len(filtered)
    if total == 0:
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

        summary = _truncate(
            e.get("summary", "") or e.get("firstPrompt", "") or e.get("preview", ""),
            70,
        )
        if summary:
            lines.append(f"   {summary}")

        lines.append(f"   `/resume {sid}`")
        lines.append("")

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
