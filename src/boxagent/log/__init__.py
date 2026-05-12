"""BoxAgent log facade — public entry point.

Usage:
    from boxagent.log import log, Category

    log.info(Category.SCHEDULER_RUN, "task fired", task_id=tid, bot=bot_id)
    log.error("backend.crash", str(exc), bot=bot_id, traceback=tb)

The facade is decoupled from the underlying event store. `gateway` binds
the real implementation at startup; until then all calls are no-ops.

Do NOT import from `boxagent.events` in business code — that is the
internal implementation. Stick to this package.
"""
from __future__ import annotations

from .categories import Category
from .facade import LogFacade, LogSink, log
from .null import NullLogger

__all__ = ["log", "Category", "LogFacade", "LogSink", "NullLogger"]
