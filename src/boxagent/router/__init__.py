"""Router — message routing, commands, callbacks, context."""

from boxagent.router.core import Router
from boxagent.router.callback import ChannelCallback, TextCollector, log_turn
from boxagent.router.context import build_session_context, build_schedule_context

__all__ = [
    "Router",
    "ChannelCallback",
    "TextCollector",
    "log_turn",
    "build_session_context",
    "build_schedule_context",
]
