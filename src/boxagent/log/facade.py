"""Public log facade. Business modules import only `log` (and optionally
`Category`). The facade hides the underlying EventBus / SQLite / sync /
Telegram details.

Contract:
- `log.<level>(category, message, **meta)` never raises.
- Before `gateway` calls `log.bind(bus)`, all calls are no-ops.
- If the bound sink raises, the exception is logged to stderr and dropped.
"""
from __future__ import annotations

import sys
from typing import Protocol

from .null import NullLogger


class LogSink(Protocol):
    def publish(self, level: str, category: str, message: str, **meta) -> None: ...


class LogFacade:
    def __init__(self) -> None:
        self._sink: LogSink = NullLogger()

    def bind(self, sink: LogSink) -> None:
        self._sink = sink

    def unbind(self) -> None:
        self._sink = NullLogger()

    def _emit(self, level: str, category: str, message: str, meta: dict) -> None:
        try:
            self._sink.publish(level, category, message, **meta)
        except Exception as exception:
            print(f"[log facade] sink failed: {exception!r}", file=sys.stderr)

    def debug(self, category: str, message: str, **meta) -> None:
        self._emit("debug", category, message, meta)

    def info(self, category: str, message: str, **meta) -> None:
        self._emit("info", category, message, meta)

    def warning(self, category: str, message: str, **meta) -> None:
        self._emit("warning", category, message, meta)

    def error(self, category: str, message: str, **meta) -> None:
        self._emit("error", category, message, meta)

    def notify(self, category: str, message: str, **meta) -> None:
        self._emit("notify", category, message, meta)


log = LogFacade()
