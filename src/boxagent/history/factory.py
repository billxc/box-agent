"""Pick an :class:`AgentHistory` impl by backend kind."""

from __future__ import annotations

from boxagent.history.claude import ClaudeAgentHistory
from boxagent.history.codex import CodexAgentHistory
from boxagent.history.copilot import CopilotAgentHistory
from boxagent.history.protocol import AgentHistory


# Map ai_backend kind → history impl. Both claude-cli and
# agent-sdk-claude write to ~/.claude/projects, so they share one impl.
_REGISTRY: dict[str, type[AgentHistory]] = {
    "claude-cli":         ClaudeAgentHistory,
    "agent-sdk-claude":   ClaudeAgentHistory,
    "codex-cli":          CodexAgentHistory,
    "agent-sdk-copilot":  CopilotAgentHistory,
}


def get_history(backend_kind: str) -> AgentHistory:
    """Return a fresh ``AgentHistory`` for this backend kind.

    Caller owns the returned instance. For Copilot specifically you may
    want to ``await history.close()`` when done to release the spawned
    CLI subprocess.
    """
    impl = _REGISTRY.get(backend_kind)
    if impl is None:
        raise ValueError(f"No AgentHistory registered for backend {backend_kind!r}")
    return impl()


def supported_backends() -> list[str]:
    """List backend kinds with a registered history impl."""
    return list(_REGISTRY.keys())
