"""Backend factory — instantiate the right ``AgentBackend`` for a ``BotConfig``.

Used by both ``AgentManager`` and ``WorkgroupManager``. Module-level so
both can ``from boxagent.agent.backend_factory import create_backend``
directly — no DI plumbing through Gateway needed.

``ClaudeProcess`` is looked up via ``boxagent.gateway`` so existing tests
that ``patch("boxagent.gateway.ClaudeProcess")`` keep working. The other
backends import normally — patching them at their own module paths works.
"""

from boxagent.agent.protocol import AgentBackend
from boxagent.config import BotConfig


def create_backend(bot_cfg: BotConfig, session_id: str | None) -> AgentBackend:
    """Instantiate the AI backend for a bot config."""
    if bot_cfg.ai_backend == "codex-cli":
        from boxagent.agent.codex_process import CodexProcess

        return CodexProcess(
            workspace=bot_cfg.workspace,
            session_id=session_id,
            model=bot_cfg.model,
            agent=bot_cfg.agent,
            bot_name=bot_cfg.name,
            yolo=bot_cfg.yolo,
        )
    if bot_cfg.ai_backend == "agent-sdk-claude":
        from boxagent.agent.sdk_claude_process import AgentSDKClaude

        return AgentSDKClaude(
            workspace=bot_cfg.workspace,
            session_id=session_id,
            model=bot_cfg.model,
            agent=bot_cfg.agent,
            bot_name=bot_cfg.name,
            yolo=bot_cfg.yolo,
        )
    if bot_cfg.ai_backend == "agent-sdk-copilot":
        from boxagent.agent.sdk_copilot_process import AgentSDKCopilot

        return AgentSDKCopilot(
            workspace=bot_cfg.workspace,
            session_id=session_id,
            model=bot_cfg.model,
            agent=bot_cfg.agent,
            bot_name=bot_cfg.name,
            yolo=bot_cfg.yolo,
        )
    # Default: claude-cli. Looked up via boxagent.gateway so tests can
    # patch("boxagent.gateway.ClaudeProcess") to inject mocks without
    # knowing the factory's internals.
    from boxagent import gateway as _gw_pkg
    return _gw_pkg.ClaudeProcess(
        workspace=bot_cfg.workspace,
        session_id=session_id,
        model=bot_cfg.model,
        agent=bot_cfg.agent,
        bot_name=bot_cfg.name,
        yolo=bot_cfg.yolo,
    )
