"""Neutral, content-agnostic message bus.

This package is a LEAF: it imports nothing project-internal. `events/` and
`cluster/` both depend on it; it depends on neither. The core routes on `topic`
only and never inspects `payload`.
"""
from __future__ import annotations

from boxagent.bus.core import MessageBus, Subscription
from boxagent.bus.message import Packet
from boxagent.bus.subscriber import Subscriber

__all__ = [
    "Packet",
    "MessageBus",
    "Subscription",
    "Subscriber",
]
