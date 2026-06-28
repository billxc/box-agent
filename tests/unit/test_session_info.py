"""SessionInfo + builder — pure session_id keyed, disk only."""

from __future__ import annotations

from boxagent.agent.session_info import SessionInfo
from boxagent.sessions.info_builder import (
    build_session_info,
    context_used_from_usage,
    context_window_for,
)


async def test_builder_unknown_backend_returns_minimal_info():
    info = await build_session_info(
        session_id="sess_x", backend_kind="unknown",
        model="claude-opus-4-7",
    )
    assert isinstance(info, SessionInfo)
    assert info.session_id == "sess_x"
    assert info.backend_kind == "unknown"
    assert info.model == "claude-opus-4-7"
    assert info.last_turn_usage is None
    assert info.message_count == 0
    assert info.context_window == 1_000_000
    assert info.context_used == 0


async def test_builder_missing_session_id_still_returns_info_with_window():
    info = await build_session_info(
        session_id="", backend_kind="agent-sdk-claude", model="opus",
    )
    assert info.session_id == ""
    assert info.last_turn_usage is None
    assert info.context_window == 1_000_000


def test_context_used_sums_input_and_cache():
    used = context_used_from_usage({
        "input_tokens": 10, "output_tokens": 99,
        "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30,
    })
    assert used == 60  # output_tokens NOT included


def test_context_used_handles_none():
    assert context_used_from_usage(None) == 0
    assert context_used_from_usage({}) == 0


def test_context_window_falls_back_to_default_for_unknown_model():
    assert context_window_for("") == 1_000_000
    assert context_window_for("some-future-model") == 1_000_000


def test_normalize_codex_usage_maps_cached_to_cache_read():
    from boxagent.agent.codex_process import _normalize_codex_usage
    out = _normalize_codex_usage({
        "input_tokens": 10, "output_tokens": 20, "cached_input_tokens": 5,
    })
    assert out == {
        "input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 5,
    }


def test_normalize_claude_usage_keeps_cache_keys():
    from boxagent.agent.sdk_claude_process import AgentSDKClaude
    out = AgentSDKClaude._normalize_usage({
        "input_tokens": 1, "output_tokens": 2,
        "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4,
        "service_tier": "standard",
    })
    assert out == {
        "input_tokens": 1, "output_tokens": 2,
        "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4,
    }
