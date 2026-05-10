"""Read-only access to agent backends' session history.

See :mod:`boxagent.history.protocol` for the design overview.
"""

from boxagent.history import _sdk_patch as _sdk_patch
from boxagent.history.factory import get_history, supported_backends

# Apply SDK monkey patch eagerly so any caller using ClaudeAgentHistory
# (directly or via factory) sees timestamp/cwd/git_branch on SessionMessage.
_sdk_patch.apply()
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
