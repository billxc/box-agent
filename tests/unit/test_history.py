"""Tests for boxagent.history — Protocol satisfaction + factory wiring."""

import pytest

from boxagent.history import (
    AgentHistory,
    Message,
    ProjectInfo,
    SessionInfo,
    get_history,
    supported_backends,
)
from boxagent.history.claude import ClaudeAgentHistory
from boxagent.history.codex import CodexAgentHistory
from boxagent.history.copilot import CopilotAgentHistory


class TestProtocolSatisfaction:
    def test_claude_satisfies(self):
        assert isinstance(ClaudeAgentHistory(), AgentHistory)

    def test_codex_satisfies(self):
        assert isinstance(CodexAgentHistory(), AgentHistory)

    def test_copilot_satisfies(self):
        assert isinstance(CopilotAgentHistory(), AgentHistory)


class TestFactory:
    def test_claude_cli_routes_to_claude(self):
        h = get_history("claude-cli")
        assert isinstance(h, ClaudeAgentHistory)

    def test_agent_sdk_claude_routes_to_claude(self):
        """Both Claude backends share one history (they use the same dir)."""
        h = get_history("agent-sdk-claude")
        assert isinstance(h, ClaudeAgentHistory)

    def test_codex_routes_to_codex(self):
        h = get_history("codex-cli")
        assert isinstance(h, CodexAgentHistory)

    def test_copilot_routes_to_copilot(self):
        h = get_history("agent-sdk-copilot")
        assert isinstance(h, CopilotAgentHistory)

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="No AgentHistory"):
            get_history("nonexistent-backend")

    def test_supported_backends_list(self):
        backends = supported_backends()
        assert "claude-cli" in backends
        assert "agent-sdk-claude" in backends
        assert "codex-cli" in backends
        assert "agent-sdk-copilot" in backends


class TestEmptyDirectories:
    """All impls handle missing/empty dirs gracefully (no crash)."""

    @pytest.mark.asyncio
    async def test_claude_global_call_does_not_raise(self):
        """ClaudeAgentHistory uses the SDK directly — no claude_dir arg.
        We just verify the call returns a list (may be empty if the user
        has no Claude sessions on this machine)."""
        h = ClaudeAgentHistory()
        projects = await h.list_projects()
        assert isinstance(projects, list)

    @pytest.mark.asyncio
    async def test_codex_missing_dir(self, tmp_path):
        h = CodexAgentHistory(codex_dir=tmp_path / "does-not-exist")
        assert await h.list_projects() == []
        assert await h.list_sessions("any") == []
        assert await h.read_messages("sid", "any") == []


class TestCodexParsing:
    """Codex's rollout file walker — small synthetic session."""

    @pytest.mark.asyncio
    async def test_parses_session_meta_and_first_user(self, tmp_path):
        import json
        sessions_dir = tmp_path / "codex-sessions"
        sessions_dir.mkdir()
        rollout = sessions_dir / "rollout-2026-05-08.jsonl"
        rollout.write_text(
            json.dumps({
                "type": "session_meta",
                "payload": {"id": "abc-123", "cwd": "/work", "timestamp": "2026-05-08T10:00:00Z"},
            }) + "\n" +
            json.dumps({
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "hello world"},
            }) + "\n",
        )
        h = CodexAgentHistory(codex_dir=sessions_dir)
        sessions = await h.list_sessions("/work")
        assert len(sessions) == 1
        assert sessions[0].session_id == "abc-123"
        assert sessions[0].cwd == "/work"
        assert sessions[0].first_user == "hello world"

    @pytest.mark.asyncio
    async def test_filters_by_project_cwd(self, tmp_path):
        import json
        sessions_dir = tmp_path / "codex-sessions"
        sessions_dir.mkdir()
        for i, cwd in enumerate(["/a", "/b", "/a"]):
            (sessions_dir / f"rollout-{i}.jsonl").write_text(
                json.dumps({
                    "type": "session_meta",
                    "payload": {"id": f"sid-{i}", "cwd": cwd},
                }) + "\n",
            )
        h = CodexAgentHistory(codex_dir=sessions_dir)
        a_sessions = await h.list_sessions("/a")
        assert {s.session_id for s in a_sessions} == {"sid-0", "sid-2"}


class TestDataclasses:
    def test_project_info_minimal(self):
        p = ProjectInfo(project_id="proj1", label="proj1")
        assert p.cwd == ""
        assert p.session_count == 0

    def test_message_minimal(self):
        m = Message(role="user")
        assert m.text == ""
        assert m.args == {}

    def test_session_info_minimal(self):
        s = SessionInfo(session_id="sid")
        assert s.project_id == ""
        assert s.custom_title is None
