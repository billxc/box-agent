"""Build the per-message AgentEnv snapshot from Router state + IncomingMessage.

Extracted from Router so the construction logic is reachable without
instantiating a full Router. The Router method becomes a thin delegator.

Usage in production:
    env = build_env(msg, router=self)

In tests you can pass any object with the matching attributes (or a
SimpleNamespace) and assert against the returned AgentEnv directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from boxagent.agent_env import AgentEnv, ChannelInfo
from boxagent.transports.base import IncomingMessage

if TYPE_CHECKING:
    from .core import Router


def build_env(msg: IncomingMessage, router: "Router") -> AgentEnv:
    """Create an AgentEnv snapshot for *msg* using the live state on *router*."""
    chat_id = msg.chat_id
    channel = msg.channel_info or ChannelInfo(platform=msg.channel or "unknown")

    if router.pool and chat_id:
        model = router.pool.get_model(chat_id) or ""
        workspace = router.pool.get_workspace(chat_id) or router.workspace
    else:
        model = router.backend.model or ""
        workspace = router.workspace

    return AgentEnv(
        channel=channel,
        chat_id=chat_id,
        user_id=msg.user_id,
        bot_name=router.bot_name,
        display_name=router.display_name,
        node_id=router.node_id,
        workspace=workspace,
        config_dir=router.config_dir,
        local_dir=str(router.local_dir) if router.local_dir else "",
        telegram_token=router.telegram_token,
        ai_backend=router.ai_backend,
        model=model,
        yolo=router.backend.yolo,
        passthrough=router.passthrough,
    )


def build_session_context(chat_id: str, router: "Router", env: AgentEnv | None = None) -> str:
    """Build a one-time context block for the first message of a session.

    If *env* is supplied (the common case during dispatch), the heavy lifting
    happens inside :mod:`boxagent.router.context` and we just hand it through.
    Otherwise we recover the minimum context from *router* directly — used
    by callers that need a context block before an env exists.
    """
    from boxagent.router.context import build_session_context as _build

    if env is not None:
        return _build(env=env)

    return _build(
        bot_name=router.bot_name,
        display_name=router.display_name,
        node_id=router.node_id,
        workspace=router.workspace,
        config_dir=router.config_dir,
    )
