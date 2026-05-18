"""Tests for web/log_file.py — read_tail."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from boxagent.transports.web.log_file import read_tail


def _write_log(path: Path, entries: list[dict | str]) -> None:
    lines = []
    for entry in entries:
        if isinstance(entry, dict):
            lines.append(json.dumps(entry))
        else:
            lines.append(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _entry(idx: int, level: str = "INFO", logger: str = "boxagent.x", msg: str | None = None) -> dict:
    return {
        "time": f"2026-05-15 10:00:{idx:02d},000",
        "level": level,
        "logger": logger,
        "msg": msg if msg is not None else f"message {idx}",
    }


class TestReadTailBasic:
    def test_empty_file(self, tmp_path: Path) -> None:
        log = tmp_path / "boxagent.log"
        log.write_text("", encoding="utf-8")
        result = read_tail(log, limit=10)
        assert result["lines"] == []
        assert result["has_more"] is False

    def test_missing_file(self, tmp_path: Path) -> None:
        result = read_tail(tmp_path / "nope.log", limit=10)
        assert result["lines"] == []
        assert result["has_more"] is False

    def test_short_file_returns_newest_first(self, tmp_path: Path) -> None:
        log = tmp_path / "boxagent.log"
        _write_log(log, [_entry(i) for i in range(5)])
        result = read_tail(log, limit=10)
        assert len(result["lines"]) == 5
        assert result["lines"][0]["msg"] == "message 4"
        assert result["lines"][-1]["msg"] == "message 0"
        assert result["has_more"] is False

    def test_limit_truncates(self, tmp_path: Path) -> None:
        log = tmp_path / "boxagent.log"
        _write_log(log, [_entry(i) for i in range(20)])
        result = read_tail(log, limit=5)
        assert len(result["lines"]) == 5
        assert result["lines"][0]["msg"] == "message 19"
        assert result["lines"][-1]["msg"] == "message 15"
        assert result["has_more"] is True


class TestReadTailPagination:
    def test_offset_skips_newest(self, tmp_path: Path) -> None:
        log = tmp_path / "boxagent.log"
        _write_log(log, [_entry(i) for i in range(20)])
        result = read_tail(log, limit=5, offset=5)
        assert [line["msg"] for line in result["lines"]] == [
            "message 14", "message 13", "message 12", "message 11", "message 10",
        ]
        assert result["has_more"] is True

    def test_offset_past_end(self, tmp_path: Path) -> None:
        log = tmp_path / "boxagent.log"
        _write_log(log, [_entry(i) for i in range(5)])
        result = read_tail(log, limit=10, offset=100)
        assert result["lines"] == []
        assert result["has_more"] is False


class TestReadTailFilters:
    def test_level_filter_single(self, tmp_path: Path) -> None:
        log = tmp_path / "boxagent.log"
        _write_log(log, [
            _entry(0, level="INFO"),
            _entry(1, level="ERROR"),
            _entry(2, level="INFO"),
            _entry(3, level="WARNING"),
            _entry(4, level="ERROR"),
        ])
        result = read_tail(log, limit=10, levels=["ERROR"])
        assert len(result["lines"]) == 2
        assert result["lines"][0]["msg"] == "message 4"
        assert result["lines"][1]["msg"] == "message 1"

    def test_level_filter_multiple_case_insensitive(self, tmp_path: Path) -> None:
        log = tmp_path / "boxagent.log"
        _write_log(log, [
            _entry(0, level="INFO"),
            _entry(1, level="ERROR"),
            _entry(2, level="WARNING"),
        ])
        result = read_tail(log, limit=10, levels=["error", "warning"])
        assert {line["msg"] for line in result["lines"]} == {"message 1", "message 2"}

    def test_grep_filter(self, tmp_path: Path) -> None:
        log = tmp_path / "boxagent.log"
        _write_log(log, [
            _entry(0, msg="hello world"),
            _entry(1, msg="HELLO again"),
            _entry(2, msg="goodbye"),
        ])
        result = read_tail(log, limit=10, grep="hello")
        assert {line["msg"] for line in result["lines"]} == {"hello world", "HELLO again"}

    def test_grep_searches_logger_and_msg(self, tmp_path: Path) -> None:
        log = tmp_path / "boxagent.log"
        _write_log(log, [
            _entry(0, logger="boxagent.cluster", msg="x"),
            _entry(1, logger="aiohttp.access", msg="y"),
        ])
        result = read_tail(log, limit=10, grep="cluster")
        assert len(result["lines"]) == 1
        assert result["lines"][0]["msg"] == "x"

    def test_filter_then_paginate(self, tmp_path: Path) -> None:
        log = tmp_path / "boxagent.log"
        entries: list[dict] = []
        for i in range(20):
            entries.append(_entry(i, level="ERROR" if i % 2 == 0 else "INFO"))
        _write_log(log, entries)
        result = read_tail(log, limit=3, offset=2, levels=["ERROR"])
        # ERRORs newest-first: 18, 16, 14, 12, 10, 8, 6, 4, 2, 0
        # offset=2 skips 18, 16; limit=3 -> 14, 12, 10
        assert [line["msg"] for line in result["lines"]] == ["message 14", "message 12", "message 10"]
        assert result["has_more"] is True


class TestReadTailMalformed:
    def test_bad_json_kept_as_raw(self, tmp_path: Path) -> None:
        log = tmp_path / "boxagent.log"
        log.write_text(
            json.dumps(_entry(0)) + "\n"
            + "not json at all\n"
            + json.dumps(_entry(1)) + "\n",
            encoding="utf-8",
        )
        result = read_tail(log, limit=10)
        assert len(result["lines"]) == 3
        assert result["lines"][0]["msg"] == "message 1"
        assert result["lines"][1].get("raw") == "not json at all"
        assert result["lines"][2]["msg"] == "message 0"

    def test_bad_json_grep_falls_back_to_raw(self, tmp_path: Path) -> None:
        log = tmp_path / "boxagent.log"
        log.write_text(
            "garbage with needle inside\n"
            "garbage without\n",
            encoding="utf-8",
        )
        result = read_tail(log, limit=10, grep="needle")
        assert len(result["lines"]) == 1
        assert result["lines"][0].get("raw") == "garbage with needle inside"

    def test_bad_json_skipped_when_level_filter_set(self, tmp_path: Path) -> None:
        log = tmp_path / "boxagent.log"
        log.write_text(
            "garbage\n"
            + json.dumps(_entry(0, level="ERROR")) + "\n",
            encoding="utf-8",
        )
        result = read_tail(log, limit=10, levels=["ERROR"])
        assert len(result["lines"]) == 1
        assert result["lines"][0]["msg"] == "message 0"

    def test_raw_pseudo_level_included_when_selected(self, tmp_path: Path) -> None:
        log = tmp_path / "boxagent.log"
        log.write_text(
            "garbage one\n"
            + json.dumps(_entry(0, level="ERROR")) + "\n"
            + "garbage two\n",
            encoding="utf-8",
        )
        result = read_tail(log, limit=10, levels=["ERROR", "RAW"])
        assert [
            line.get("msg") or line.get("raw")
            for line in result["lines"]
        ] == ["garbage two", "message 0", "garbage one"]


class TestReadTailLargeFile:
    def test_chunked_read_across_chunk_boundary(self, tmp_path: Path) -> None:
        # Force many lines so the seek-from-end iterator must cross chunks.
        log = tmp_path / "boxagent.log"
        entries = [_entry(i, msg="x" * 200) for i in range(2000)]
        _write_log(log, entries)
        result = read_tail(log, limit=50)
        assert len(result["lines"]) == 50
        assert result["lines"][0]["msg"].startswith("x")
        assert result["lines"][0]["time"] == entries[-1]["time"]

    def test_no_trailing_newline(self, tmp_path: Path) -> None:
        log = tmp_path / "boxagent.log"
        log.write_text(json.dumps(_entry(0)) + "\n" + json.dumps(_entry(1)), encoding="utf-8")
        result = read_tail(log, limit=10)
        assert len(result["lines"]) == 2
        assert result["lines"][0]["msg"] == "message 1"
