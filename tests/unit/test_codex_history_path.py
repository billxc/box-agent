"""Tests for CodexAgentHistory.get_session_path / sync escape hatches.

The grep filter in the ``sessions_list`` tool needs the on-disk path of
a Codex session to walk its JSONL — that's a codex-only extension.
"""

import json
import os
from pathlib import Path

import pytest

from boxagent.history.codex import CodexAgentHistory


def _write_rollout(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


@pytest.mark.asyncio
async def test_get_session_path_returns_jsonl_for_known_sid(tmp_path):
    sessions_dir = tmp_path / "codex"
    rollout = sessions_dir / "2026" / "03" / "rollout-x.jsonl"
    _write_rollout(rollout, [
        {"type": "session_meta", "payload": {"id": "sid-1", "cwd": "/work"}},
    ])
    h = CodexAgentHistory(codex_dir=sessions_dir)
    path = await h.get_session_path("sid-1")
    assert path == rollout


@pytest.mark.asyncio
async def test_get_session_path_returns_none_for_unknown(tmp_path):
    sessions_dir = tmp_path / "codex"
    sessions_dir.mkdir()
    h = CodexAgentHistory(codex_dir=sessions_dir)
    assert await h.get_session_path("nonexistent") is None


def test_sync_helpers_match_async_results(tmp_path):
    sessions_dir = tmp_path / "codex"
    rollout = sessions_dir / "rollout-y.jsonl"
    _write_rollout(rollout, [
        {"type": "session_meta", "payload": {"id": "sid-2", "cwd": "/work"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}},
    ])
    h = CodexAgentHistory(codex_dir=sessions_dir)
    sessions = h.list_sessions_sync("/work")
    assert len(sessions) == 1
    assert sessions[0].session_id == "sid-2"
    assert sessions[0].first_user == "hi"
    assert h.get_session_path_sync("sid-2") == rollout
    assert h.get_session_path_sync("nope") is None


def test_loaders_unified_includes_codex(tmp_path):
    """End-to-end: _load_all_unified_sessions walks the codex history
    factory and surfaces a Codex session in the unified list."""
    # Point the codex dir at our temp fixture by patching the default.
    sessions_dir = tmp_path / "codex"
    rollout = sessions_dir / "rollout-z.jsonl"
    _write_rollout(rollout, [
        {"type": "session_meta", "payload": {"id": "sid-loaders", "cwd": str(tmp_path / "ws")}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "zzz"}},
    ])

    from unittest.mock import patch
    from boxagent.sessions.browser.loaders import _load_all_unified_sessions

    with patch("boxagent.sessions.browser.loaders.CODEX_DIR", sessions_dir), \
         patch("boxagent.sessions.browser.loaders.CLAUDE_DIR", tmp_path / "empty-claude"):
        entries = _load_all_unified_sessions(workspace=str(tmp_path / "ws"))

    sids = {e.get("sessionId") for e in entries}
    assert "sid-loaders" in sids
    entry = next(e for e in entries if e["sessionId"] == "sid-loaders")
    assert entry["backend"] == "codex-cli"
    assert entry["preview"] == "zzz"
    assert entry["_codex_path"] == str(rollout)
