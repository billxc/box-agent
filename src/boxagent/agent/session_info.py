"""SessionInfo — backend-agnostic snapshot of a session, keyed by ``session_id``.

Built by :func:`boxagent.sessions.info_builder.build_session_info` purely
from the backend's transcript on disk + a small in-process model→context
table. Decoupled from chats and from live backend instances.

Token-usage shape (``last_turn_usage``):
    {"input_tokens": int, "output_tokens": int,
     "cache_read_input_tokens": int (optional),
     "cache_creation_input_tokens": int (optional)}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionInfo:
    session_id: str
    backend_kind: str = ""
    model: str = ""
    workspace: str = ""
    last_turn_usage: dict[str, int] | None = None
    message_count: int = 0
    last_ts: float = 0.0
    context_window: int = 0
    context_used: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
