"""Pick an :class:`AgentHistory` implementation by backend kind."""

from __future__ import annotations

from boxagent.history.claude import ClaudeAgentHistory
from boxagent.history.codex import CodexAgentHistory
from boxagent.history.copilot import CopilotAgentHistory
from boxagent.history.protocol import AgentHistory


# Map ai_backend kind → history implementation. Both claude-cli and
# agent-sdk-claude write to ~/.claude/projects, so they share one implementation.
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
    implementation = _REGISTRY.get(backend_kind)
    if implementation is None:
        raise ValueError(f"No AgentHistory registered for backend {backend_kind!r}")
    return implementation()


def supported_backends() -> list[str]:
    """List backend kinds with a registered history implementation."""
    return list(_REGISTRY.keys())
