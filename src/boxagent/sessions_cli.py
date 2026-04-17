"""CLI subcommands for listing Claude CLI sessions on this machine."""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from boxagent.utils import safe_print as _safe_print


CLAUDE_DIR = Path.home() / ".claude"


def build_sessions_parser(subparsers) -> None:
    """Register 'sessions' subcommand with sub-subparsers."""
    sessions = subparsers.add_parser("sessions", help="List Claude CLI sessions")
    sessions_sub = sessions.add_subparsers(dest="sessions_cmd")

    ls = sessions_sub.add_parser("list", help="List all sessions")
    ls.add_argument(
        "--project", default="",
        help="Filter by project path (substring match)",
    )
    ls.add_argument(
        "--json", dest="output_json", action="store_true", default=False,
        help="Output as JSON",
    )
    ls.set_defaults(func=sessions_list)


def _load_all_sessions() -> list[dict]:
    """Collect sessions from index files and unindexed .jsonl files."""
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

    # Sort by modified descending
    entries.sort(key=lambda e: e.get("modified", ""), reverse=True)
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


def _truncate(text: str, limit: int) -> str:
    s = " ".join(str(text).split())
    if len(s) > limit:
        return s[: limit - 3] + "..."
    return s


def format_sessions_list(project_filter: str = "", limit: int = 20) -> str:
    """Return a formatted string listing Claude CLI sessions."""
    entries = _load_all_sessions()

    if project_filter:
        entries = [
            e for e in entries
            if project_filter.lower() in e.get("projectPath", "").lower()
        ]

    if not entries:
        return "No sessions found."

    lines = []
    for idx, e in enumerate(entries[:limit], 1):
        sid = e.get("sessionId", "?")[:8]
        msgs = e.get("messageCount", "")
        modified = e.get("modified", "")
        time_str = ""
        if modified:
            try:
                dt = datetime.fromisoformat(modified.replace("Z", "+00:00"))
                time_str = dt.strftime("%m-%d %H:%M")
            except (ValueError, TypeError):
                time_str = modified[:10]
        project_path = e.get("projectPath", "")
        project = Path(project_path).name if project_path else ""
        summary = _truncate(
            e.get("summary", "") or e.get("firstPrompt", ""), 60,
        )
        line = f"{idx}. `{sid}` {msgs}msg {time_str} **{project}**"
        if summary:
            line += f"\n    {summary}"
        lines.append(line)
    return "\n".join(lines)


def sessions_list(args) -> None:
    """List all Claude CLI sessions."""
    entries = _load_all_sessions()

    project_filter = getattr(args, "project", "")
    if project_filter:
        entries = [
            e for e in entries
            if project_filter in e.get("projectPath", "")
        ]

    if not entries:
        print("No sessions found.")
        return

    if getattr(args, "output_json", False):
        _safe_print(json.dumps(entries, indent=2, ensure_ascii=False))
        return

    # Table output
    print(
        f"{'SESSION_ID':<38} {'MSGS':>4}  {'MODIFIED':<20} {'PROJECT':<30} {'SUMMARY'}"
    )
    print("-" * 120)
    for e in entries:
        sid = e.get("sessionId", "?")[:36]
        msgs = str(e.get("messageCount", ""))
        modified = e.get("modified", "")[:19]
        project = _truncate(e.get("projectPath", ""), 28)
        summary = _truncate(e.get("summary", e.get("firstPrompt", "")), 40)
        print(f"{sid:<38} {msgs:>4}  {modified:<20} {project:<30} {summary}")
