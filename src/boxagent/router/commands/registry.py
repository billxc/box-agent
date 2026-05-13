"""Command registry — `@command("/name")` decorator + lookup map.

Every slash-command the Router serves is a free function in
``router.commands`` decorated with
``@command("/X", help="...", category=CommandCategory.X)``.
The decorator populates ``COMMAND_REGISTRY`` at import time. Router goes
through the registry and never knows the list of commands; ``/help``
auto-generates from the same metadata, grouped by ``category``.

Adding a new command = define a free function with the standard
``(router, msg, channel)`` signature and decorate it. No bookkeeping.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from boxagent.router.core import Router
    from boxagent.transports.base import Channel, IncomingMessage


CommandFn = Callable[
    ["Router", "IncomingMessage", "Channel"],
    Awaitable[None],
]


class CommandCategory(Enum):
    """Section a command lives under in ``/help``.

    Order of declaration here is the order the sections render in. Add a
    new category by adding a member; misspelled categories at call sites
    fail the type checker instead of silently sliding into a "extras"
    bucket the way bare strings did.
    """
    SESSION = "Session"
    WORKSPACE = "Workspace"
    TOOLS = "Tools"
    INFO = "Info"


@dataclass
class CommandSpec:
    """One registered command. ``help`` is the one-line description shown
    by ``/help``; empty means the command is hidden from the listing.
    ``category`` decides which section the command lives under."""
    name: str
    handler: CommandFn
    help: str = ""
    category: CommandCategory | None = None


COMMAND_REGISTRY: dict[str, CommandSpec] = {}


def command(
    name: str,
    *,
    help: str = "",
    category: CommandCategory | None = None,
) -> Callable[[CommandFn], CommandFn]:
    """Register ``handler`` as the handler for slash command ``name``.

    Pass ``help`` to include the command in ``/help`` output (one line).
    Omit ``help`` for commands intentionally hidden from the listing.
    ``category`` decides which section the command lives under in
    ``/help``; section order is the declaration order of
    :class:`CommandCategory`.

    Raises if ``name`` is already registered.
    """
    def decorator(handler: CommandFn) -> CommandFn:
        if name in COMMAND_REGISTRY:
            raise RuntimeError(
                f"command {name!r} already registered to {COMMAND_REGISTRY[name].handler.__name__}"
            )
        COMMAND_REGISTRY[name] = CommandSpec(
            name=name, handler=handler, help=help, category=category,
        )
        return handler
    return decorator

