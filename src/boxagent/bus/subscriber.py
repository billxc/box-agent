"""The bus Subscriber protocol.

A subscriber is "something that receives a Message for a topic, in order": the
sync entry the bus calls during its ordered fan-out. Concrete subscribers live
next to their consumers (events/bus.py's store + callback adapters, WebChannel's
per-chat queue adapter) — each is a tiny class that duck-types this protocol.

This module is a neutral leaf: it imports nothing project-internal.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from boxagent.bus.message import Message


@runtime_checkable
class Subscriber(Protocol):
    """The sync entry the bus calls during its ordered fan-out.

    `deliver` must be non-blocking and must not raise back into the bus's
    fan-out loop for control flow (the bus isolates exceptions, but a subscriber
    that blocks stalls the synchronous publish). Ephemeral consumers drop on a
    full queue; durable ones must not lose messages.
    """

    def deliver(self, message: Message) -> None: ...
