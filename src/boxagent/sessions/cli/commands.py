"""CLI subcommand handler for ``boxagent sessions list``."""

from __future__ import annotations

import json

from boxagent.utils import safe_print as _safe_print

from .filters import _filter_sessions, _grep_sessions
from .format import format_sessions_list
from .loaders import _load_all_unified_sessions
from .tokens import parse_session_tokens


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


def sessions_list(args) -> None:
    """List all sessions (unified: Claude CLI + BoxAgent history + Codex)."""
    from boxagent.config import load_config

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
