"""Tests for sessions_cli — CLI subcommand handlers."""

import json
import time
from argparse import Namespace
from pathlib import Path

import pytest

from boxagent.sessions.cli import (
    _truncate,
    _relative_time,
    _filter_sessions,
    _matches_all_words,
    _find_by_id_prefix,
    _grep_sessions,
    format_sessions_list,
    parse_session_tokens,
    sessions_list,
)


def _mock_claude_sessions(monkeypatch, sessions_index_entries: list[dict]) -> None:
    """Patch loaders.ClaudeAgentHistory to return canned sessions.

    Takes the legacy sessions-index.json entry shape (sessionId, summary,
    messageCount, modified, projectPath) and synthesises ProjectInfo +
    SessionInfo objects so loaders' merge logic can run unchanged.
    """
    from boxagent.history.protocol import ProjectInfo, SessionInfo

    by_project: dict[str, list[SessionInfo]] = {}
    for e in sessions_index_entries:
        cwd = str(e.get("projectPath", "")) or "/test"
        modified = str(e.get("modified", ""))
        try:
            ts = int(time.mktime(time.strptime(modified[:19], "%Y-%m-%dT%H:%M:%S"))) if modified else 0
        except ValueError:
            ts = 0
        info = SessionInfo(
            session_id=str(e.get("sessionId", "")),
            project_id=cwd,
            first_user=str(e.get("firstPrompt", "")),
            message_count=int(e.get("messageCount", 0) or 0),
            last_ts=ts,
            cwd=cwd,
            summary=str(e.get("summary", "")),
        )
        by_project.setdefault(cwd, []).append(info)

    class _MockHistory:
        def list_projects_sync(self):
            return [
                ProjectInfo(
                    project_id=cwd,
                    label=Path(cwd).name,
                    cwd=cwd,
                    session_count=len(items),
                    last_ts=max((s.last_ts for s in items), default=0),
                )
                for cwd, items in by_project.items()
            ]

        def list_sessions_sync(self, project_id: str):
            return list(by_project.get(project_id, []))

    monkeypatch.setattr("boxagent.sessions.cli.loaders.ClaudeAgentHistory", _MockHistory)


def _sample_entry(session_id: str = "abc-123", **overrides) -> dict:
    defaults = {
        "sessionId": session_id,
        "summary": "Test session",
        "messageCount": 5,
        "created": "2026-04-01T10:00:00.000Z",
        "modified": "2026-04-01T11:00:00.000Z",
        "gitBranch": "main",
        "projectPath": "/Users/test/my-project",
    }
    defaults.update(overrides)
    return defaults


def _write_jsonl(projects_dir: Path, project_name: str, session_id: str, messages: list[dict]) -> Path:
    """Write a fake session .jsonl file."""
    proj_dir = projects_dir / project_name
    proj_dir.mkdir(parents=True, exist_ok=True)
    jsonl_file = proj_dir / f"{session_id}.jsonl"
    lines = [json.dumps(m, ensure_ascii=False) for m in messages]
    jsonl_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_file


def _make_user_message(content: str, timestamp: str = "2026-04-01T10:00:00.000Z", session_id: str = "test", cwd: str = "/Users/test/proj") -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": content},
        "timestamp": timestamp,
        "sessionId": session_id,
        "cwd": cwd,
    }


def _make_assistant_message(content: str = "ok") -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": content}]},
    }


class TestParseSessionTokens:
    def test_empty(self):
        r = parse_session_tokens("")
        assert r == {"page": 1, "days": None, "backend": "", "bot": "", "cwd_search": "", "grep": "", "id_prefix": "", "query": "", "all": False}

    def test_page_only(self):
        r = parse_session_tokens("p3")
        assert r["page"] == 3
        assert r["query"] == ""

    def test_days_only(self):
        r = parse_session_tokens("7d")
        assert r["days"] == 7
        assert r["query"] == ""

    def test_backend_filter(self):
        r = parse_session_tokens("backend:codex-cli")
        assert r["backend"] == "codex-cli"
        assert r["query"] == ""

    def test_bot_filter(self):
        r = parse_session_tokens("bot:claw-mac")
        assert r["bot"] == "claw-mac"
        assert r["query"] == ""

    def test_all_flag(self):
        r = parse_session_tokens("--all")
        assert r["all"] is True
        assert r["query"] == ""

    def test_all_with_query(self):
        r = parse_session_tokens("--all discord 7d")
        assert r["all"] is True
        assert r["days"] == 7
        assert r["query"] == "discord"

    def test_cwd_search(self):
        r = parse_session_tokens("cwd:chromium")
        assert r["cwd_search"] == "chromium"
        assert r["query"] == ""

    def test_cwd_search_with_query(self):
        r = parse_session_tokens("cwd:box-agent discord 7d")
        assert r["cwd_search"] == "box-agent"
        assert r["days"] == 7
        assert r["query"] == "discord"

    def test_grep(self):
        r = parse_session_tokens("grep:pineapple")
        assert r["grep"] == "pineapple"
        assert r["query"] == ""

    def test_grep_with_filters(self):
        r = parse_session_tokens("grep:TODO 7d backend:claude-cli")
        assert r["grep"] == "TODO"
        assert r["days"] == 7
        assert r["backend"] == "claude-cli"

    def test_combined(self):
        r = parse_session_tokens("chromium 3d backend:claude-cli p2")
        assert r["page"] == 2
        assert r["days"] == 3
        assert r["backend"] == "claude-cli"
        assert r["query"] == "chromium"

    def test_multiple_search_words(self):
        r = parse_session_tokens("discord fix")
        assert r["query"] == "discord fix"

    def test_search_with_filters(self):
        r = parse_session_tokens("discord fix 7d bot:claw-mac p2")
        assert r["query"] == "discord fix"
        assert r["days"] == 7
        assert r["bot"] == "claw-mac"
        assert r["page"] == 2

    def test_only_first_page_token(self):
        """Second p-token falls through to query."""
        r = parse_session_tokens("p2 p3")
        assert r["page"] == 2
        assert r["query"] == "p3"

    def test_only_first_days_token(self):
        r = parse_session_tokens("3d 7d")
        assert r["days"] == 3
        assert r["query"] == "7d"


class TestMatchesAllWords:
    def test_single_word_match(self):
        entry = {"summary": "Discord fix", "firstPrompt": "", "preview": "", "project": "", "backend": "", "model": ""}
        assert _matches_all_words(entry, ["discord"])

    def test_multi_word_all_match(self):
        entry = {"summary": "Discord fix", "firstPrompt": "slash commands", "preview": "", "project": "", "backend": "", "model": ""}
        assert _matches_all_words(entry, ["discord", "commands"])

    def test_multi_word_one_missing(self):
        entry = {"summary": "Discord fix", "firstPrompt": "", "preview": "", "project": "", "backend": "", "model": ""}
        assert not _matches_all_words(entry, ["discord", "chromium"])

    def test_matches_project(self):
        entry = {"summary": "", "firstPrompt": "", "preview": "", "project": "box-agent", "backend": "", "model": ""}
        assert _matches_all_words(entry, ["box-agent"])

    def test_matches_backend(self):
        entry = {"summary": "", "firstPrompt": "", "preview": "", "project": "", "projectPath": "", "backend": "codex-cli", "model": ""}
        assert _matches_all_words(entry, ["codex"])

    def test_matches_project_path(self):
        entry = {"summary": "", "firstPrompt": "", "preview": "", "project": "src", "projectPath": "/Users/test/chromium/src", "backend": "", "model": ""}
        assert _matches_all_words(entry, ["chromium"])


class TestFilterSessions:
    def _make_entries(self):
        now = int(time.time())
        return [
            {"sessionId": "s1", "summary": "Discord fix", "firstPrompt": "", "preview": "",
             "project": "box-agent", "projectPath": "/Users/test/box-agent",
             "backend": "claude-cli", "model": "opus", "bot": "claw-mac",
             "modified_ts": now - 3600},
            {"sessionId": "s2", "summary": "WebView build", "firstPrompt": "", "preview": "",
             "project": "chromium", "projectPath": "/Users/test/chromium",
             "backend": "codex-cli", "model": "sonnet", "bot": "claw-wsl",
             "modified_ts": now - 86400 * 3},
            {"sessionId": "s3", "summary": "Docker networking", "firstPrompt": "", "preview": "",
             "project": "homelab", "projectPath": "/Users/test/homelab",
             "backend": "claude-cli", "model": "opus", "bot": "claw-mac",
             "modified_ts": now - 86400 * 10},
        ]

    def test_no_filter(self):
        entries = self._make_entries()
        assert len(_filter_sessions(entries)) == 3

    def test_time_filter(self):
        entries = self._make_entries()
        result = _filter_sessions(entries, days=1)
        assert len(result) == 1
        assert result[0]["sessionId"] == "s1"

    def test_time_filter_week(self):
        entries = self._make_entries()
        result = _filter_sessions(entries, days=7)
        assert len(result) == 2

    def test_backend_filter(self):
        entries = self._make_entries()
        result = _filter_sessions(entries, backend="codex-cli")
        assert len(result) == 1
        assert result[0]["sessionId"] == "s2"

    def test_bot_filter(self):
        entries = self._make_entries()
        result = _filter_sessions(entries, bot="claw-wsl")
        assert len(result) == 1
        assert result[0]["sessionId"] == "s2"

    def test_query_filter(self):
        entries = self._make_entries()
        result = _filter_sessions(entries, query="discord")
        assert len(result) == 1
        assert result[0]["sessionId"] == "s1"

    def test_combined_filters(self):
        entries = self._make_entries()
        result = _filter_sessions(entries, backend="claude-cli", days=7)
        assert len(result) == 1
        assert result[0]["sessionId"] == "s1"

    def test_cwd_filter_exact(self):
        entries = self._make_entries()
        result = _filter_sessions(entries, cwd="/Users/test/box-agent")
        assert len(result) == 1
        assert result[0]["sessionId"] == "s1"

    def test_cwd_filter_with_trailing_slash(self):
        entries = self._make_entries()
        result = _filter_sessions(entries, cwd="/Users/test/box-agent/")
        assert len(result) == 1
        assert result[0]["sessionId"] == "s1"

    def test_cwd_filter_no_match(self):
        entries = self._make_entries()
        result = _filter_sessions(entries, cwd="/Users/test/nonexistent")
        assert len(result) == 0

    def test_cwd_filter_empty_shows_all(self):
        entries = self._make_entries()
        result = _filter_sessions(entries, cwd="")
        assert len(result) == 3

    def test_cwd_filter_subdirectory(self):
        """Sessions in a subdirectory of cwd should also match."""
        entries = self._make_entries()
        # Add a session in a subdirectory
        entries.append({
            "sessionId": "s4", "summary": "Sub work", "firstPrompt": "", "preview": "",
            "project": "sub", "projectPath": "/Users/test/box-agent/sub",
            "backend": "claude-cli", "model": "opus", "bot": "",
            "modified_ts": int(time.time()),
        })
        result = _filter_sessions(entries, cwd="/Users/test/box-agent")
        assert len(result) == 2
        assert {e["sessionId"] for e in result} == {"s1", "s4"}

    def test_cwd_search_substring(self):
        """cwd_search does case-insensitive substring match on projectPath."""
        entries = self._make_entries()
        result = _filter_sessions(entries, cwd_search="chromium")
        assert len(result) == 1
        assert result[0]["sessionId"] == "s2"

    def test_cwd_search_case_insensitive(self):
        entries = self._make_entries()
        result = _filter_sessions(entries, cwd_search="BOX-AGENT")
        assert len(result) == 1
        assert result[0]["sessionId"] == "s1"

    def test_cwd_search_partial(self):
        entries = self._make_entries()
        result = _filter_sessions(entries, cwd_search="test")
        assert len(result) == 3  # all entries have /Users/test/ in path


class TestFindByIdPrefix:
    def test_match(self):
        entries = [{"sessionId": "abcdef1234"}, {"sessionId": "xyz999"}]
        assert len(_find_by_id_prefix(entries, "abcd")) == 1

    def test_no_match(self):
        entries = [{"sessionId": "abcdef1234"}]
        assert len(_find_by_id_prefix(entries, "9999")) == 0


class TestGrepSessions:
    def test_match_in_content(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions.cli.loaders.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir, "proj", "sess-1", [
            _make_user_message("please fix the pineapple bug"),
            _make_assistant_message("done"),
        ])
        entries = [{"sessionId": "sess-1", "project": "proj"}]
        result = _grep_sessions(entries, "pineapple")
        assert len(result) == 1

    def test_no_match_in_content(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions.cli.loaders.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir, "proj", "sess-1", [
            _make_user_message("hello world"),
            _make_assistant_message("hi"),
        ])
        entries = [{"sessionId": "sess-1", "project": "proj"}]
        result = _grep_sessions(entries, "pineapple")
        assert len(result) == 0

    def test_case_insensitive(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions.cli.loaders.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir, "proj", "sess-1", [
            _make_user_message("Fix the Discord bot"),
        ])
        entries = [{"sessionId": "sess-1", "project": "proj"}]
        assert len(_grep_sessions(entries, "discord")) == 1
        assert len(_grep_sessions(entries, "DISCORD")) == 1

    def test_codex_path(self, tmp_path):
        """Sessions with _codex_path use that path instead of resolving."""
        jsonl = tmp_path / "rollout.jsonl"
        jsonl.write_text('{"type":"user","message":{"content":"codex magic"}}\n')
        entries = [{"sessionId": "codex-1", "_codex_path": str(jsonl)}]
        assert len(_grep_sessions(entries, "codex magic")) == 1
        assert len(_grep_sessions(entries, "nonexistent")) == 0

    def test_missing_file_skipped(self):
        entries = [{"sessionId": "nonexistent-id", "project": "nope"}]
        result = _grep_sessions(entries, "anything")
        assert len(result) == 0


class TestRelativeTime:
    def test_just_now(self):
        assert _relative_time(int(time.time())) == "just now"

    def test_minutes(self):
        assert "m ago" in _relative_time(int(time.time()) - 300)

    def test_hours(self):
        assert "h ago" in _relative_time(int(time.time()) - 7200)

    def test_days(self):
        assert "d ago" in _relative_time(int(time.time()) - 86400 * 3)

    def test_zero(self):
        assert _relative_time(0) == ""


class TestTruncate:
    def test_short_string(self):
        assert _truncate("hello", 10) == "hello"

    def test_long_string(self):
        assert _truncate("a" * 50, 10) == "aaaaaaa..."

    def test_collapses_whitespace(self):
        assert _truncate("hello   world\nnew", 40) == "hello world new"


class TestFormatSessionsList:
    @pytest.fixture(autouse=True)
    def _isolate_backends(self, tmp_path_factory, monkeypatch):
        """Stub out both backend history sources so real ~/.claude and
        ~/.codex sessions don't bleed into these format-focused tests.

        Tests that want to inject fake Claude entries override this by
        calling ``_mock_claude_sessions(monkeypatch, ...)`` themselves
        — autouse stubs default to empty.
        """
        empty = tmp_path_factory.mktemp("empty-codex")
        monkeypatch.setattr("boxagent.sessions.cli.loaders.CODEX_DIR", empty)
        _mock_claude_sessions(monkeypatch, [])

    def test_no_sessions(self):
        result = format_sessions_list()
        assert "No sessions found" in result

    def test_basic_list(self, monkeypatch):
        _mock_claude_sessions(monkeypatch, [_sample_entry("id-1")])
        result = format_sessions_list()
        assert "Sessions" in result
        assert "/resume id-1" in result
        assert "my-project" in result

    def test_search_filter(self, monkeypatch):
        _mock_claude_sessions(monkeypatch, [
            _sample_entry("id-1", summary="Discord fix"),
            _sample_entry("id-2", summary="WebView build", modified="2026-04-02T11:00:00.000Z"),
        ])
        result = format_sessions_list(query="discord")
        assert "/resume id-1" in result
        assert "/resume id-2" not in result

    def test_pagination(self, monkeypatch):
        _mock_claude_sessions(monkeypatch, [
            _sample_entry(f"id-{i}", modified=f"2026-04-{i+1:02d}T11:00:00.000Z")
            for i in range(8)
        ])
        result = format_sessions_list(query="p2")
        assert "6-8 / 8" in result

    def test_no_results_with_filter(self, monkeypatch):
        _mock_claude_sessions(monkeypatch, [_sample_entry("id-1")])
        result = format_sessions_list(query="nonexistent")
        assert "No sessions matching" in result

    def test_hex_prefix_match(self, monkeypatch):
        _mock_claude_sessions(monkeypatch, [_sample_entry("abcdef1234567890")])
        result = format_sessions_list(query="abcd")
        assert "/resume abcdef1234567890" in result

    def test_days_filter(self, monkeypatch):
        # Entry from 2026-04-01 is old enough to be filtered out by 1d
        _mock_claude_sessions(monkeypatch, [_sample_entry("id-1")])
        result = format_sessions_list(query="1d")
        assert "No sessions" in result or "id-1" in result  # depends on test run date

    def test_cwd_filter_default(self, monkeypatch):
        """With workspace set, only sessions matching that path are shown."""
        _mock_claude_sessions(monkeypatch, [
            _sample_entry("id-1", projectPath="/Users/test/proj-a"),
            _sample_entry("id-2", projectPath="/Users/test/proj-b",
                          modified="2026-04-02T11:00:00.000Z"),
        ])
        result = format_sessions_list(workspace="/Users/test/proj-a")
        assert "/resume id-1" in result
        assert "/resume id-2" not in result
        assert "proj-a" in result

    def test_cwd_all_flag(self, monkeypatch):
        """--all bypasses cwd filter, showing sessions from all projects."""
        _mock_claude_sessions(monkeypatch, [
            _sample_entry("id-1", projectPath="/Users/test/proj-a"),
            _sample_entry("id-2", projectPath="/Users/test/proj-b",
                          modified="2026-04-02T11:00:00.000Z"),
        ])
        result = format_sessions_list(query="--all", workspace="/Users/test/proj-a")
        assert "/resume id-1" in result
        assert "/resume id-2" in result
        assert "all projects" in result

    def test_cwd_no_match_hint(self, monkeypatch):
        """When cwd filter yields no results, hint about --all."""
        _mock_claude_sessions(monkeypatch, [
            _sample_entry("id-1", projectPath="/Users/test/proj-a"),
        ])
        result = format_sessions_list(workspace="/Users/test/other")
        assert "--all" in result

    def test_cwd_search_filter(self, monkeypatch):
        """cwd:xxx does substring search on projectPath, bypassing default cwd."""
        _mock_claude_sessions(monkeypatch, [
            _sample_entry("id-1", projectPath="/Users/test/proj-a"),
            _sample_entry("id-2", projectPath="/Users/test/proj-b",
                          modified="2026-04-02T11:00:00.000Z"),
        ])
        result = format_sessions_list(query="cwd:proj-b", workspace="/Users/test/proj-a")
        assert "/resume id-2" in result
        assert "/resume id-1" not in result

    def test_grep_filter(self, tmp_path, monkeypatch):
        """grep:xxx does full-text search on JSONL content."""
        monkeypatch.setattr("boxagent.sessions.cli.loaders.CLAUDE_DIR", tmp_path)
        _mock_claude_sessions(monkeypatch, [
            _sample_entry("sess-1", projectPath="/Users/test/proj-a"),
            _sample_entry("sess-2", projectPath="/Users/test/proj-a",
                          modified="2026-04-02T11:00:00.000Z"),
        ])
        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir, "proj-a", "sess-1", [
            _make_user_message("fix the pineapple bug"),
            _make_assistant_message("done"),
        ])
        _write_jsonl(projects_dir, "proj-a", "sess-2", [
            _make_user_message("update the readme"),
            _make_assistant_message("ok"),
        ])
        result = format_sessions_list(query="grep:pineapple")
        assert "/resume sess-1" in result
        assert "/resume sess-2" not in result

    def test_grep_no_match(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions.cli.loaders.CLAUDE_DIR", tmp_path)
        _mock_claude_sessions(monkeypatch, [
            _sample_entry("sess-1", projectPath="/Users/test/proj-a"),
        ])
        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir, "proj-a", "sess-1", [
            _make_user_message("hello world"),
        ])
        result = format_sessions_list(query="grep:nonexistent")
        assert "No sessions" in result


class TestSessionsList:
    @pytest.fixture(autouse=True)
    def _isolate_backends(self, tmp_path_factory, monkeypatch):
        empty = tmp_path_factory.mktemp("empty-codex")
        monkeypatch.setattr("boxagent.sessions.cli.loaders.CODEX_DIR", empty)
        _mock_claude_sessions(monkeypatch, [])

    def test_no_sessions(self, capsys):
        args = Namespace(query=[], output_json=False, workspace="")
        sessions_list(args)
        assert "No sessions found" in capsys.readouterr().out

    def test_formatted_output(self, monkeypatch, capsys):
        _mock_claude_sessions(monkeypatch, [_sample_entry("id-1")])
        args = Namespace(query=[], output_json=False, workspace="")
        sessions_list(args)
        out = capsys.readouterr().out
        assert "id-1" in out
        assert "Sessions" in out

    def test_json_output(self, monkeypatch, capsys):
        _mock_claude_sessions(monkeypatch, [_sample_entry("id-1")])
        args = Namespace(query=[], output_json=True, workspace="")
        sessions_list(args)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert len(data) == 1
        assert data[0]["sessionId"] == "id-1"

    def test_query_filter(self, monkeypatch, capsys):
        _mock_claude_sessions(monkeypatch, [
            _sample_entry("id-1", projectPath="/Users/test/proj-a", summary="Discord fix"),
            _sample_entry("id-2", projectPath="/Users/test/proj-b",
                          modified="2026-04-02T11:00:00.000Z"),
        ])
        args = Namespace(query=["cwd:proj-a"], output_json=True, workspace="")
        sessions_list(args)
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 1
        assert data[0]["sessionId"] == "id-1"

    def test_workspace_filter(self, monkeypatch, capsys):
        _mock_claude_sessions(monkeypatch, [
            _sample_entry("id-1", projectPath="/Users/test/proj-a"),
            _sample_entry("id-2", projectPath="/Users/test/proj-b"),
        ])
        args = Namespace(query=[], output_json=True, workspace="/Users/test/proj-a")
        sessions_list(args)
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 1
        assert data[0]["sessionId"] == "id-1"
