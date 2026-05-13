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


class TestClaudeListProjectsFastScan:
    """``_list_projects_sync`` must NOT call ``sdk_list_sessions()`` —
    that path parses every jsonl globally and timed out on hosts with
    thousands of session files (cluster RPC 504). It should scan dirs
    + read just the first line of the newest jsonl per dir for cwd."""

    @pytest.mark.asyncio
    async def test_returns_one_project_per_dir_with_cwd_from_first_line(self, tmp_path):
        import json

        proj_a = tmp_path / "-Users-bill-code-foo"
        proj_a.mkdir()
        (proj_a / "s1.jsonl").write_text(
            json.dumps({"type": "user", "cwd": "/Users/bill/code/foo"}) + "\n"
            + json.dumps({"type": "assistant"}) + "\n"
        )
        (proj_a / "s2.jsonl").write_text(
            json.dumps({"type": "user", "cwd": "/Users/bill/code/foo"}) + "\n"
        )
        proj_b = tmp_path / "-tmp-bar"
        proj_b.mkdir()
        (proj_b / "s3.jsonl").write_text(
            json.dumps({"type": "user", "cwd": "/tmp/bar"}) + "\n"
        )
        (tmp_path / "empty-dir").mkdir()

        h = ClaudeAgentHistory(claude_dir=tmp_path)
        projects = await h.list_projects()
        by_id = {p.project_id: p for p in projects}
        assert set(by_id) == {"/Users/bill/code/foo", "/tmp/bar"}
        assert by_id["/Users/bill/code/foo"].session_count == 2
        assert by_id["/Users/bill/code/foo"].label == "foo"
        assert by_id["/tmp/bar"].session_count == 1

    @pytest.mark.asyncio
    async def test_missing_cwd_falls_back_to_dir_name(self, tmp_path):
        proj = tmp_path / "weird-dir"
        proj.mkdir()
        (proj / "x.jsonl").write_text('{"type": "user"}\n')
        h = ClaudeAgentHistory(claude_dir=tmp_path)
        projects = await h.list_projects()
        assert len(projects) == 1
        assert projects[0].project_id == "weird-dir"
        assert projects[0].cwd == ""

    @pytest.mark.asyncio
    async def test_missing_root_returns_empty(self, tmp_path):
        h = ClaudeAgentHistory(claude_dir=tmp_path / "nope")
        assert await h.list_projects() == []

    @pytest.mark.asyncio
    async def test_does_not_call_sdk_list_sessions(self, tmp_path, monkeypatch):
        import json
        from boxagent.history import claude as claude_mod

        proj = tmp_path / "p"
        proj.mkdir()
        (proj / "a.jsonl").write_text(json.dumps({"cwd": "/p"}) + "\n")

        def boom(*a, **kw):
            raise AssertionError("sdk_list_sessions must not be called for project listing")

        monkeypatch.setattr(claude_mod, "sdk_list_sessions", boom)
        h = ClaudeAgentHistory(claude_dir=tmp_path)
        projects = await h.list_projects()
        assert len(projects) == 1


class TestClaudeListSessionsPaginated:
    """``list_sessions_paginated`` reads sessions lazily — sorts by mtime
    (cheap stat) and only invokes ``sdk_get_session_info`` for the
    requested slice. Avoids the full ``sdk_list_sessions`` JSONL parse."""

    @pytest.mark.asyncio
    async def test_slice_and_total(self, tmp_path, monkeypatch):
        import json
        import os
        import time
        from boxagent.history import claude as claude_mod

        cwd = "/Users/bill/code/foo"
        from claude_agent_sdk._internal.sessions import project_key_for_directory
        proj_dir = tmp_path / project_key_for_directory(cwd)
        proj_dir.mkdir(parents=True)
        for i in range(5):
            path = proj_dir / f"sid-{i}.jsonl"
            path.write_text(json.dumps({"cwd": cwd}) + "\n")
            mtime = time.time() - (5 - i)  # sid-4 newest
            os.utime(path, (mtime, mtime))

        def boom(*a, **kw):
            raise AssertionError("sdk_list_sessions must not be called for paginated listing")

        monkeypatch.setattr(claude_mod, "sdk_list_sessions", boom)

        loaded: list[str] = []

        def fake_get_info(session_id, directory):
            loaded.append(session_id)
            return claude_mod.SDKSessionInfo(
                session_id=session_id,
                summary=f"summary-{session_id}",
                last_modified=int(time.time() * 1000),
                first_prompt=f"hi from {session_id}",
                cwd=cwd,
            )

        monkeypatch.setattr(claude_mod, "sdk_get_session_info", fake_get_info)
        h = ClaudeAgentHistory(claude_dir=tmp_path)
        sessions, total = await h.list_sessions_paginated(cwd, offset=0, limit=2)
        assert total == 5
        assert [s.session_id for s in sessions] == ["sid-4", "sid-3"]
        assert loaded == ["sid-4", "sid-3"]

        sessions2, total2 = await h.list_sessions_paginated(cwd, offset=2, limit=2)
        assert total2 == 5
        assert [s.session_id for s in sessions2] == ["sid-2", "sid-1"]

    @pytest.mark.asyncio
    async def test_unknown_project_returns_empty(self, tmp_path):
        h = ClaudeAgentHistory(claude_dir=tmp_path)
        sessions, total = await h.list_sessions_paginated("/no/such/dir", 0, 50)
        assert sessions == []
        assert total == 0
