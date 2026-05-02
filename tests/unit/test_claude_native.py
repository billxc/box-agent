"""Tests for claude_native.read_messages tool_use / tool_result extraction."""

import json
from pathlib import Path

import pytest

from boxagent.sessions.claude_native import read_messages


def _write_session(tmp_path: Path, encoded: str, sid: str, lines: list[dict]) -> Path:
    proj = tmp_path / encoded
    proj.mkdir(parents=True)
    f = proj / f"{sid}.jsonl"
    f.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return f


def _user_text(text: str, ts: str = "2026-05-02T12:00:00Z") -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": text},
    }


def _assistant_blocks(blocks: list[dict], ts: str = "2026-05-02T12:00:01Z") -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"role": "assistant", "content": blocks},
    }


def _tool_result_user(blocks: list[dict], ts: str = "2026-05-02T12:00:02Z") -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": blocks},
    }


def test_text_only_assistant_unchanged(tmp_path):
    _write_session(tmp_path, "proj", "s1", [
        _user_text("hello"),
        _assistant_blocks([{"type": "text", "text": "hi there"}]),
    ])
    recs = read_messages("proj", "s1", claude_dir=tmp_path)
    assert [(r["role"], r["text"]) for r in recs] == [
        ("user", "hello"), ("assistant", "hi there"),
    ]


def test_assistant_tool_use_emits_tool_call_record(tmp_path):
    _write_session(tmp_path, "proj", "s1", [
        _assistant_blocks([
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "toolu_x", "name": "Bash",
             "input": {"command": "ls"}},
        ]),
    ])
    recs = read_messages("proj", "s1", claude_dir=tmp_path)
    assert len(recs) == 2
    assert recs[0] == {"role": "assistant", "text": "Let me check.", "ts": recs[0]["ts"]}
    assert recs[1]["role"] == "tool_call"
    assert recs[1]["tool_id"] == "toolu_x"
    assert recs[1]["name"] == "Bash"
    assert recs[1]["args"] == {"command": "ls"}


def test_assistant_with_only_tool_use_no_text_record(tmp_path):
    _write_session(tmp_path, "proj", "s1", [
        _assistant_blocks([
            {"type": "tool_use", "id": "toolu_y", "name": "Read",
             "input": {"path": "/x"}},
        ]),
    ])
    recs = read_messages("proj", "s1", claude_dir=tmp_path)
    assert len(recs) == 1
    assert recs[0]["role"] == "tool_call"


def test_user_tool_result_emits_tool_result_record(tmp_path):
    _write_session(tmp_path, "proj", "s1", [
        _tool_result_user([
            {"type": "tool_result", "tool_use_id": "toolu_x",
             "content": "file.txt\nfile.py", "is_error": False},
        ]),
    ])
    recs = read_messages("proj", "s1", claude_dir=tmp_path)
    assert len(recs) == 1
    assert recs[0]["role"] == "tool_result"
    assert recs[0]["tool_id"] == "toolu_x"
    assert recs[0]["ok"] is True
    assert "file.txt" in recs[0]["summary"]
    assert recs[0]["error"] == ""


def test_tool_result_error_path(tmp_path):
    _write_session(tmp_path, "proj", "s1", [
        _tool_result_user([
            {"type": "tool_result", "tool_use_id": "toolu_x",
             "content": "command not found", "is_error": True},
        ]),
    ])
    recs = read_messages("proj", "s1", claude_dir=tmp_path)
    assert recs[0]["ok"] is False
    assert recs[0]["error"] == "command not found"
    assert recs[0]["summary"] == ""


def test_tool_result_list_content_joins_text_blocks(tmp_path):
    _write_session(tmp_path, "proj", "s1", [
        _tool_result_user([
            {"type": "tool_result", "tool_use_id": "tx", "content": [
                {"type": "text", "text": "line1"},
                {"type": "text", "text": "line2"},
            ]},
        ]),
    ])
    recs = read_messages("proj", "s1", claude_dir=tmp_path)
    assert recs[0]["summary"] == "line1\nline2"


def test_full_turn_round_trip(tmp_path):
    """User → assistant(text+tool_use) → user(tool_result) → assistant(text)
    yields 5 records in order: user, assistant text, tool_call, tool_result, assistant text."""
    _write_session(tmp_path, "proj", "s1", [
        _user_text("run ls"),
        _assistant_blocks([
            {"type": "text", "text": "Sure."},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
        ]),
        _tool_result_user([
            {"type": "tool_result", "tool_use_id": "t1", "content": "a.txt"},
        ]),
        _assistant_blocks([{"type": "text", "text": "Done."}]),
    ])
    recs = read_messages("proj", "s1", claude_dir=tmp_path)
    assert [r["role"] for r in recs] == [
        "user", "assistant", "tool_call", "tool_result", "assistant",
    ]


def test_missing_file_returns_empty(tmp_path):
    assert read_messages("nonexistent", "x", claude_dir=tmp_path) == []
