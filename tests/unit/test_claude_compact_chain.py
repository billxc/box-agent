"""Tests for ClaudeAgentHistory.walk_compact_chain.

Builds a fake ``~/.claude/projects/<key>/`` with two JSONL files where the
newer session's first entry is an ``isCompactSummary`` whose ``parentUuid``
points into the older session — simulating Claude's native compaction
linkage. The walker should follow the link.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from boxagent.history.claude import ClaudeAgentHistory


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


@pytest.fixture
def fake_project(tmp_path, monkeypatch):
    """Patch ``Path.home()`` so the walker reads from tmp_path."""
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    cwd = "/tmp/fake-project"
    # project_key_for_directory is deterministic; just call it to learn the key
    from claude_agent_sdk import project_key_for_directory

    project_key = project_key_for_directory(cwd)
    project_dir = tmp_path / ".claude" / "projects" / project_key
    project_dir.mkdir(parents=True)
    return project_dir, cwd


def test_walk_compact_chain_follows_isCompactSummary_link(fake_project, monkeypatch):
    project_dir, cwd = fake_project

    # Old session: one user message, one assistant
    old_sid = "11111111-1111-1111-1111-111111111111"
    last_old_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    _write_jsonl(project_dir / f"{old_sid}.jsonl", [
        {"type": "user", "uuid": "u-old-1", "sessionId": old_sid,
         "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "uuid": last_old_uuid, "parentUuid": "u-old-1",
         "sessionId": old_sid, "message": {"role": "assistant", "content": "hello"}},
    ])

    # New session: starts with isCompactSummary pointing back to old session's last uuid
    new_sid = "22222222-2222-2222-2222-222222222222"
    _write_jsonl(project_dir / f"{new_sid}.jsonl", [
        {"type": "user", "uuid": "u-new-summary", "parentUuid": last_old_uuid,
         "sessionId": new_sid, "isCompactSummary": True,
         "message": {"role": "user", "content": "[summary]"}},
        {"type": "assistant", "uuid": "u-new-2", "parentUuid": "u-new-summary",
         "sessionId": new_sid, "message": {"role": "assistant", "content": "ok"}},
    ])

    history = ClaudeAgentHistory()
    chain = history._walk_compact_chain_sync(new_sid, cwd)
    assert chain == [old_sid]


def test_walk_compact_chain_returns_empty_for_root_session(fake_project):
    project_dir, cwd = fake_project
    sid = "33333333-3333-3333-3333-333333333333"
    _write_jsonl(project_dir / f"{sid}.jsonl", [
        {"type": "user", "uuid": "u-1", "sessionId": sid,
         "message": {"role": "user", "content": "first"}},
    ])

    history = ClaudeAgentHistory()
    assert history._walk_compact_chain_sync(sid, cwd) == []


def test_walk_compact_chain_recurses_through_multiple_compactions(fake_project):
    project_dir, cwd = fake_project

    sid_a = "aaaa1111-0000-0000-0000-000000000000"
    sid_b = "bbbb2222-0000-0000-0000-000000000000"
    sid_c = "cccc3333-0000-0000-0000-000000000000"
    last_a = "leaf-a"
    last_b = "leaf-b"

    _write_jsonl(project_dir / f"{sid_a}.jsonl", [
        {"type": "user", "uuid": "u-a-1", "sessionId": sid_a,
         "message": {"role": "user", "content": "first chat"}},
        {"type": "assistant", "uuid": last_a, "parentUuid": "u-a-1",
         "sessionId": sid_a, "message": {"role": "assistant", "content": "ok"}},
    ])
    _write_jsonl(project_dir / f"{sid_b}.jsonl", [
        {"type": "user", "uuid": "u-b-summary", "parentUuid": last_a,
         "sessionId": sid_b, "isCompactSummary": True,
         "message": {"role": "user", "content": "[summary 1]"}},
        {"type": "assistant", "uuid": last_b, "parentUuid": "u-b-summary",
         "sessionId": sid_b, "message": {"role": "assistant", "content": "ok"}},
    ])
    _write_jsonl(project_dir / f"{sid_c}.jsonl", [
        {"type": "user", "uuid": "u-c-summary", "parentUuid": last_b,
         "sessionId": sid_c, "isCompactSummary": True,
         "message": {"role": "user", "content": "[summary 2]"}},
    ])

    history = ClaudeAgentHistory()
    chain = history._walk_compact_chain_sync(sid_c, cwd)
    # oldest-first
    assert chain == [sid_a, sid_b]
