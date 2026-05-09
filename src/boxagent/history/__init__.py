"""Read-only access to agent backends' session history.

See :mod:`boxagent.history.protocol` for the design overview.
"""

from boxagent.history.factory import get_history, supported_backends
from boxagent.history.protocol import (
    AgentHistory,
    Message,
    ProjectInfo,
    SessionInfo,
)

__all__ = [
    "AgentHistory",
    "Message",
    "ProjectInfo",
    "SessionInfo",
    "get_history",
    "supported_backends",
]
