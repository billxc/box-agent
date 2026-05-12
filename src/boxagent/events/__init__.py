"""Internal event log implementation. Business code MUST NOT import from
this package — use `boxagent.log` instead.

This package will grow over commits #2-#10:
  models.py             — Event dataclass + Level constants
  storage.py            — SQLite-backed EventStore
  bus.py                — EventBus (commit #3)
  cluster_frames.py     — wire frames for cross-machine sync (commit #5)
  syncer.py             — host/satellite event replication (commit #5)
  telegram_notifier.py  — standalone Telegram push subscriber (commit #6)
  web_stream.py         — SSE subscriber for the web UI (commit #4)
"""
