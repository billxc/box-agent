"""Tests for the SDK monkey patch that surfaces JSONL ``timestamp`` /
``cwd`` / ``gitBranch`` on ``SessionMessage``.

The patch lives in :mod:`boxagent.history._sdk_patch` and is applied
when :mod:`boxagent.history` is imported. We verify it by calling
``_to_session_message`` directly with a synthetic transcript entry —
no SDK session reads required.
"""

from claude_agent_sdk._internal import sessions as sdk_sessions

import boxagent.history  # noqa: F401  — triggers patch.apply()
from boxagent.history.claude import ClaudeAgentHistory


def _entry(**overrides) -> dict:
    """Build a minimal JSONL transcript entry the SDK would parse."""
    base = {
        "type": "user",
        "uuid": "u-abc",
        "sessionId": "sess-1",
        "message": {"role": "user", "content": "hi"},
        "timestamp": "2026-05-10T12:34:56.000Z",
        "cwd": "/Users/x/proj",
        "gitBranch": "main",
    }
    base.update(overrides)
    return base


def test_patched_session_message_has_timestamp_cwd_git_branch():
    sm = sdk_sessions._to_session_message(_entry())
    assert getattr(sm, "timestamp", None) == "2026-05-10T12:34:56.000Z"
    assert getattr(sm, "cwd", None) == "/Users/x/proj"
    assert getattr(sm, "git_branch", None) == "main"


def test_extract_records_uses_patched_timestamp():
    sm = sdk_sessions._to_session_message(_entry(
        message={"role": "user", "content": "hello"},
    ))
    recs = ClaudeAgentHistory()._extract_records(sm)
    assert len(recs) == 1
    # ISO 2026-05-10T12:34:56Z → unix epoch (timezone-aware)
    from datetime import datetime
    expected = datetime.fromisoformat("2026-05-10T12:34:56+00:00").timestamp()
    assert recs[0].ts == expected
    assert recs[0].cwd == "/Users/x/proj"
    assert recs[0].git_branch == "main"


def test_missing_optional_fields_fall_back_cleanly():
    sm = sdk_sessions._to_session_message({
        "type": "assistant",
        "uuid": "u-z",
        "sessionId": "sess-1",
        "message": {"role": "assistant", "content": "ok"},
        # No timestamp / cwd / gitBranch.
    })
    recs = ClaudeAgentHistory()._extract_records(sm)
    assert recs[0].ts == 0.0
    assert recs[0].cwd == ""
    assert recs[0].git_branch == ""
