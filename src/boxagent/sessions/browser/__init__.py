"""sessions.browser — read-only multi-source session listing.

Layout:
    loaders   — Claude CLI + BoxAgent + Codex session merge (file IO)
    tokens    — /sessions DSL parser (regex-driven, pure)
    filters   — list filters + search helpers (pure functions)
    format    — text rendering for /sessions output

Used by the ``/sessions`` and ``/resume`` slash commands and the MCP
``sessions`` tool. Submodule helpers stay private to their submodules —
only the three names below are part of the public surface.
"""

from .format import format_sessions_list
from .loaders import _load_all_unified_sessions
from .tokens import parse_session_tokens

__all__ = [
    "format_sessions_list",
    "_load_all_unified_sessions",
    "parse_session_tokens",
]
