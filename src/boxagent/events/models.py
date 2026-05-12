"""Internal event models. Not for direct import by business code —
use `boxagent.log` instead."""
from __future__ import annotations

from dataclasses import dataclass, field


class Level:
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    NOTIFY = "notify"


@dataclass(frozen=True)
class Event:
    id: int | None
    origin_machine: str
    origin_seq: int
    ts: float
    level: str
    category: str
    message: str
    bot: str | None = None
    meta: dict = field(default_factory=dict)
    read_at: float | None = None
