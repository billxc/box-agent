"""Backend factory — instantiate the right ``AgentBackend`` for a ``BotConfig``.

Used by both ``AgentManager`` and ``WorkgroupManager``. Module-level so
both can ``from boxagent.agent.backend_factory import create_backend``
directly — no DI plumbing through Gateway needed.
"""

from typing import Any

from boxagent.agent.protocol import AgentBackend
from boxagent.agent.sdk_claude_process import AgentSDKClaude
from boxagent.config import BotConfig


def create_backend(
    bot_config: BotConfig,
    session_id: str | None,
    *,
    gateway: Any = None,
) -> AgentBackend:
    """Instantiate the AI backend for a bot config.

    ``gateway`` is forwarded to in-process SDK backends so their
    ``ToolContext`` carries a live Gateway reference; tools like
    ``send_to_peer`` / admin tools require it. CLI backends route tool
    calls through the HTTP MCP server, which captures gateway separately.

    ``claude-cli`` is a legacy alias kept for config backward-compat: it
    silently routes to the in-process ``agent-sdk-claude`` backend. The old
    CLI subprocess implementation (``ClaudeProcess``) has been removed.
    """
    if bot_config.ai_backend == "codex-cli":
        from boxagent.agent.codex_process import CodexProcess

        return CodexProcess(
            workspace=bot_config.workspace,
            session_id=session_id,
            model=bot_config.model,
            agent=bot_config.agent,
            bot_name=bot_config.name,
            yolo=bot_config.yolo,
        )
    if bot_config.ai_backend == "agent-sdk-copilot":
        from boxagent.agent.sdk_copilot_process import AgentSDKCopilot

        return AgentSDKCopilot(
            workspace=bot_config.workspace,
            session_id=session_id,
            model=bot_config.model,
            agent=bot_config.agent,
            bot_name=bot_config.name,
            yolo=bot_config.yolo,
            gateway=gateway,
        )
    # Default + legacy "claude-cli": route to in-process SDK backend.
    return AgentSDKClaude(
        workspace=bot_config.workspace,
        session_id=session_id,
        model=bot_config.model,
        agent=bot_config.agent,
        bot_name=bot_config.name,
        yolo=bot_config.yolo,
        gateway=gateway,
    )
