"""sessions.browser — read-only multi-source session listing.

Layout:
    loaders   — Claude CLI + BoxAgent + Codex session merge (file IO)
    tokens    — /sessions DSL parser (regex-driven, pure)
    filters   — list filters + search helpers (pure functions)
    format    — text rendering for /sessions output

Used by the ``/sessions`` and ``/resume`` slash commands and the MCP
``sessions`` tool.
"""

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
    _parse_iso_to_ts,
    _resolve_session_path,
)
from .tokens import parse_session_tokens

__all__ = [
    # loaders
    "CLAUDE_DIR",
    "_load_all_unified_sessions",
    "_parse_iso_to_ts",
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
