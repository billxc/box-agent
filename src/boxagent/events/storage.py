"""SQLite-backed event store. Internal — do not import from business code.

- WAL mode + autocommit; single in-process write lock for safety.
- (origin_machine, origin_seq) is the global identity used for cross-machine dedup.
- `read_at` is local-only state; not part of the sync payload.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .models import Event


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  origin_machine TEXT NOT NULL,
  origin_seq INTEGER NOT NULL,
  ts REAL NOT NULL,
  bot TEXT,
  level TEXT NOT NULL,
  category TEXT NOT NULL,
  message TEXT NOT NULL,
  meta_json TEXT,
  read_at REAL,
  UNIQUE(origin_machine, origin_seq)
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_bot_ts ON events(bot, ts);
CREATE INDEX IF NOT EXISTS idx_events_level_ts ON events(level, ts);
CREATE INDEX IF NOT EXISTS idx_events_category_ts ON events(category, ts);
CREATE INDEX IF NOT EXISTS idx_events_origin ON events(origin_machine, origin_seq);

CREATE TABLE IF NOT EXISTS sync_cursor (
  peer_machine TEXT PRIMARY KEY,
  last_seen_seq INTEGER NOT NULL,
  updated_at REAL NOT NULL
);
"""


class EventStore:
    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- writes ----------

    def insert_local(
        self,
        machine_id: str,
        level: str,
        category: str,
        message: str,
        *,
        bot: str | None = None,
        meta: dict | None = None,
        ts: float | None = None,
    ) -> Event:
        if ts is None:
            ts = time.time()
        meta_json = json.dumps(meta) if meta else None
        with self._lock:
            cur = self._conn.execute(
                "SELECT COALESCE(MAX(origin_seq), 0) + 1 FROM events WHERE origin_machine = ?",
                (machine_id,),
            )
            seq = cur.fetchone()[0]
            cur = self._conn.execute(
                """INSERT INTO events
                   (origin_machine, origin_seq, ts, bot, level, category, message, meta_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (machine_id, seq, ts, bot, level, category, message, meta_json),
            )
            event_id = cur.lastrowid
        return Event(
            id=event_id,
            origin_machine=machine_id,
            origin_seq=seq,
            ts=ts,
            level=level,
            category=category,
            message=message,
            bot=bot,
            meta=meta or {},
            read_at=None,
        )

    def insert_remote(self, event: Event) -> bool:
        """INSERT OR IGNORE based on (origin_machine, origin_seq).
        Returns True if newly inserted, False if duplicate."""
        meta_json = json.dumps(event.meta) if event.meta else None
        with self._lock:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO events
                   (origin_machine, origin_seq, ts, bot, level, category, message, meta_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.origin_machine,
                    event.origin_seq,
                    event.ts,
                    event.bot,
                    event.level,
                    event.category,
                    event.message,
                    meta_json,
                ),
            )
            return cur.rowcount > 0

    def mark_read(self, ids: list[int]) -> int:
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE events SET read_at = ? "
                f"WHERE read_at IS NULL AND id IN ({placeholders})",
                [time.time(), *ids],
            )
            return cur.rowcount

    def delete_older_than(self, cutoff_ts: float) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM events WHERE ts < ?", (cutoff_ts,))
            return cur.rowcount

    # ---------- reads ----------

    def query(
        self,
        *,
        bot: str | None = None,
        levels: list[str] | None = None,
        machines: list[str] | None = None,
        category_prefix: str | None = None,
        since: float | None = None,
        until: float | None = None,
        search: str | None = None,
        unread_only: bool = False,
        limit: int | None = None,
        before_id: int | None = None,
    ) -> list[Event]:
        where: list[str] = []
        params: list = []

        if bot is not None:
            where.append("bot = ?")
            params.append(bot)
        if levels:
            ph = ",".join("?" * len(levels))
            where.append(f"level IN ({ph})")
            params.extend(levels)
        if machines:
            ph = ",".join("?" * len(machines))
            where.append(f"origin_machine IN ({ph})")
            params.extend(machines)
        if category_prefix is not None:
            escaped = (
                category_prefix.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            where.append("(category = ? OR category LIKE ? ESCAPE '\\')")
            params.extend([category_prefix, escaped + ".%"])
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        if until is not None:
            where.append("ts <= ?")
            params.append(until)
        if search:
            esc = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("message LIKE ? ESCAPE '\\'")
            params.append(f"%{esc}%")
        if unread_only:
            where.append("read_at IS NULL")
        if before_id is not None:
            where.append("id < ?")
            params.append(before_id)

        sql = "SELECT * FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        cur = self._conn.execute(sql, params)
        return [self._row_to_event(r) for r in cur.fetchall()]

    def max_origin_seq(self, machine: str) -> int:
        cur = self._conn.execute(
            "SELECT COALESCE(MAX(origin_seq), 0) FROM events WHERE origin_machine = ?",
            (machine,),
        )
        return cur.fetchone()[0]

    # ---------- sync cursor ----------

    def get_cursor(self, peer_machine: str) -> int:
        cur = self._conn.execute(
            "SELECT last_seen_seq FROM sync_cursor WHERE peer_machine = ?",
            (peer_machine,),
        )
        row = cur.fetchone()
        return row[0] if row else 0

    def set_cursor(self, peer_machine: str, seq: int) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO sync_cursor (peer_machine, last_seen_seq, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(peer_machine) DO UPDATE SET
                     last_seen_seq = excluded.last_seen_seq,
                     updated_at = excluded.updated_at""",
                (peer_machine, seq, time.time()),
            )

    # ---------- helpers ----------

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        meta = json.loads(row["meta_json"]) if row["meta_json"] else {}
        return Event(
            id=row["id"],
            origin_machine=row["origin_machine"],
            origin_seq=row["origin_seq"],
            ts=row["ts"],
            level=row["level"],
            category=row["category"],
            message=row["message"],
            bot=row["bot"],
            meta=meta,
            read_at=row["read_at"],
        )
