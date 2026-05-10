"""Backend factory — instantiate the right ``AgentBackend`` for a ``BotConfig``.

Used by both ``AgentManager`` and ``WorkgroupManager``. Module-level so
both can ``from boxagent.agent.backend_factory import create_backend``
directly — no DI plumbing through Gateway needed.
"""

from boxagent.agent.claude_process import ClaudeProcess
from boxagent.agent.protocol import AgentBackend
from boxagent.config import BotConfig


def create_backend(bot_config: BotConfig, session_id: str | None) -> AgentBackend:
    """Instantiate the AI backend for a bot config."""
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
    if bot_config.ai_backend == "agent-sdk-claude":
        from boxagent.agent.sdk_claude_process import AgentSDKClaude

        return AgentSDKClaude(
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
        )
    # Default: claude-cli.
    return ClaudeProcess(
        workspace=bot_config.workspace,
        session_id=session_id,
        model=bot_config.model,
        agent=bot_config.agent,
        bot_name=bot_config.name,
        yolo=bot_config.yolo,
    )
