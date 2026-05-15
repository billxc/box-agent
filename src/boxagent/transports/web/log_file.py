"""Read tail of boxagent.log for the Web UI Logs page.

The log file is JSON-line formatted by main.py — `{"time","level","logger","msg"}`
per line. This module provides a paginated, filtered tail reader that doesn't
load the whole file into memory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

_CHUNK = 64 * 1024


def _iter_lines_reverse(path: Path) -> Iterator[str]:
    """Yield lines from `path` newest-first without loading the whole file.

    Reads fixed-size chunks from the end and splits on newlines, carrying any
    partial leading fragment forward into the next (earlier) chunk.
    """
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        position = f.tell()
        leftover = b""
        while position > 0:
            read_size = min(_CHUNK, position)
            position -= read_size
            f.seek(position)
            chunk = f.read(read_size) + leftover
            parts = chunk.split(b"\n")
            # The first element may be an incomplete line (continues into the
            # earlier-not-yet-read region); stash it and yield the rest.
            leftover = parts[0]
            for piece in reversed(parts[1:]):
                if piece:
                    yield piece.decode("utf-8", errors="replace")
        if leftover:
            yield leftover.decode("utf-8", errors="replace")


def _parse(line: str) -> dict:
    line = line.rstrip("\r")
    if not line:
        return {"raw": ""}
    if line.startswith("{"):
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
    return {"raw": line}


def _matches(entry: dict, levels_lower: set[str] | None, grep_lower: str | None) -> bool:
    if levels_lower is not None:
        if "raw" in entry:
            return False
        if str(entry.get("level", "")).lower() not in levels_lower:
            return False
    if grep_lower is not None:
        if "raw" in entry:
            return grep_lower in entry["raw"].lower()
        haystack = " ".join(
            str(entry.get(field, "")) for field in ("logger", "msg", "level", "time")
        ).lower()
        if grep_lower not in haystack:
            return False
    return True


def read_tail(
    path: Path,
    limit: int = 200,
    offset: int = 0,
    levels: list[str] | None = None,
    grep: str | None = None,
) -> dict:
    """Return the tail of `path` as a list of parsed entries, newest first.

    - `offset` skips that many matching entries from the end before collecting.
    - `limit` caps how many entries are returned.
    - `levels` is an OR-list of level names (case-insensitive); None = all.
    - `grep` is a case-insensitive substring matched against logger/msg/raw.
    - `has_more` is True iff at least one further matching entry exists past
      the returned page.
    """
    if limit <= 0:
        return {"lines": [], "has_more": False}
    if offset < 0:
        offset = 0

    path = Path(path)
    if not path.exists() or not path.is_file():
        return {"lines": [], "has_more": False}

    levels_lower = {lv.lower() for lv in levels} if levels else None
    grep_lower = grep.lower() if grep else None

    skipped = 0
    collected: list[dict] = []
    has_more = False
    for line in _iter_lines_reverse(path):
        entry = _parse(line)
        if not _matches(entry, levels_lower, grep_lower):
            continue
        if skipped < offset:
            skipped += 1
            continue
        if len(collected) >= limit:
            has_more = True
            break
        collected.append(entry)

    return {"lines": collected, "has_more": has_more}
