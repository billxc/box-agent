"""NullLogger: drop-in sink that swallows everything. Used as default before
gateway binds the real EventBus, and as a test stand-in."""
from __future__ import annotations


class NullLogger:
    def publish(self, level: str, category: str, message: str, **meta) -> None:
        return None
