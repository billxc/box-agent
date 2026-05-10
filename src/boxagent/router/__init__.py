"""Router — message routing, commands, callbacks, context."""

from boxagent.router.core import Router
from boxagent.router.callback import ChannelCallback, TextCollector, log_turn
from boxagent.router.context import build_session_context, build_schedule_context

# Auto-discover slash-command modules under router/commands/. Each .py file
# whose name doesn't start with `_` is imported, which fires its
# @command(...) decorators and populates COMMAND_REGISTRY. The commands/
# directory is a namespace package — no __init__.py boilerplate needed
# there; drop a new file and it's picked up.
import importlib as _importlib
import pkgutil as _pkgutil
from pathlib import Path as _Path

_commands_dir = _Path(__file__).parent / "commands"
for _info in _pkgutil.iter_modules([str(_commands_dir)]):
    if not _info.name.startswith("_"):
        _importlib.import_module(f"boxagent.router.commands.{_info.name}")

__all__ = [
    "Router",
    "ChannelCallback",
    "TextCollector",
    "log_turn",
    "build_session_context",
    "build_schedule_context",
]
