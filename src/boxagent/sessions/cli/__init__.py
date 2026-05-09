"""sessions.cli — package layout.

The old single-file ``sessions/cli.py`` was split into:
    loaders   — Claude CLI + BoxAgent + Codex session merge (file IO)
    tokens    — /sessions DSL parser (regex-driven, pure)
    filters   — list filters + search helpers (pure functions)
    format    — text rendering for /sessions output
    commands  — argparse subcommand wiring + JSON output

Public surface is re-exported here so existing imports keep working.
"""

from .commands import build_sessions_parser, sessions_list
from .filters import (
    _filter_sessions,
    _find_by_id_prefix,
    _grep_sessions,
    _matches_all_words,
    _relative_time,
    _truncate,
)
from .format import _format_id_match, format_sessions_list
from .loaders import (
    CLAUDE_DIR,
    _load_all_unified_sessions,
    _load_claude_sessions,
    _parse_iso_to_ts,
    _parse_jsonl_metadata,
    _resolve_session_path,
)
from .tokens import parse_session_tokens

__all__ = [
    # commands
    "build_sessions_parser",
    "sessions_list",
    # loaders
    "CLAUDE_DIR",
    "_load_all_unified_sessions",
    "_load_claude_sessions",
    "_parse_iso_to_ts",
    "_parse_jsonl_metadata",
    "_resolve_session_path",
    # tokens
    "parse_session_tokens",
    # filters
    "_filter_sessions",
    "_find_by_id_prefix",
    "_grep_sessions",
    "_matches_all_words",
    "_relative_time",
    "_truncate",
    # format
    "format_sessions_list",
    "_format_id_match",
]
