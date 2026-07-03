"""The bus envelope.

`Message` is the ONLY thing the bus routes on. It is a neutral leaf: this
module imports nothing project-internal. `payload` is opaque to the bus core —
the core NEVER inspects it. Fields other than `topic` (`level`, `bot`,
`origin_seq`, `id`, `category`, ...) all live inside `payload` and are
understood only by whichever subscriber cares.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    """An immutable delivery envelope.

    topic:    routing key. Two schemes today:
                "events.<category>"                    e.g. events.scheduler.run
                "chat.<machine_id>.<bot>.<chat_id>"    e.g. chat.win-mini.assistant.web-42
    payload:  OPAQUE to the bus. The core never reads it; subscribers do.
    ts:       caller-supplied timestamp (seconds). The bus core is clock-free
              on purpose (deterministic + testable), so the caller passes ts.
              WebChannel already sets this today.
    """

    topic: str
    payload: dict
    ts: float
