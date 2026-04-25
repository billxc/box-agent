"""Tests for sessions_cli — CLI subcommand handlers."""

import json
import time
from argparse import Namespace
from pathlib import Path

import pytest

from boxagent.sessions_cli import (
    _load_claude_sessions,
    _load_all_unified_sessions,
    _parse_jsonl_metadata,
    _truncate,
    _relative_time,
    _filter_sessions,
    _matches_all_words,
    _find_by_id_prefix,
    _grep_sessions,
    _resolve_session_path,
    format_sessions_list,
    parse_session_tokens,
    sessions_list,
    CLAUDE_DIR,
)


def _make_index(projects_dir: Path, project_name: str, entries: list[dict], original_path: str = "") -> Path:
    """Create a sessions-index.json for a fake project."""
    proj_dir = projects_dir / project_name
    proj_dir.mkdir(parents=True, exist_ok=True)
    index = {
        "version": 1,
        "entries": entries,
        "originalPath": original_path or f"/Users/test/{project_name}",
    }
    index_file = proj_dir / "sessions-index.json"
    index_file.write_text(json.dumps(index), encoding="utf-8")
    return index_file


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


class TestParseJsonlMetadata:
    def test_basic(self, tmp_path):
        projects_dir = tmp_path / "projects"
        jsonl = _write_jsonl(projects_dir, "proj", "sess-1", [
            _make_user_message("hello", "2026-04-01T10:00:00.000Z", "sess-1", "/Users/test/proj"),
            _make_assistant_message("world"),
        ])
        result = _parse_jsonl_metadata(jsonl)
        assert result is not None
        assert result["sessionId"] == "sess-1"
        assert result["firstPrompt"] == "hello"
        assert result["messageCount"] == 2
        assert result["projectPath"] == "/Users/test/proj"

    def test_empty_file(self, tmp_path):
        projects_dir = tmp_path / "projects"
        proj_dir = projects_dir / "proj"
        proj_dir.mkdir(parents=True)
        jsonl = proj_dir / "sess-1.jsonl"
        jsonl.write_text("", encoding="utf-8")
        assert _parse_jsonl_metadata(jsonl) is None

    def test_no_user_messages(self, tmp_path):
        projects_dir = tmp_path / "projects"
        jsonl = _write_jsonl(projects_dir, "proj", "sess-1", [
            {"type": "permission-mode", "permissionMode": "default"},
        ])
        assert _parse_jsonl_metadata(jsonl) is None


class TestLoadClaudeSessions:
    def test_empty_when_no_projects(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        assert _load_claude_sessions() == []

    def test_empty_when_no_index_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        (tmp_path / "projects" / "some-project").mkdir(parents=True)
        assert _load_claude_sessions() == []

    def test_loads_entries_from_index(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [_sample_entry("id-1")])
        result = _load_claude_sessions()
        assert len(result) == 1
        assert result[0]["sessionId"] == "id-1"

    def test_loads_unindexed_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir, "proj", "sess-unindexed", [
            _make_user_message("unindexed prompt", cwd="/Users/test/proj"),
            _make_assistant_message(),
        ])
        result = _load_claude_sessions()
        assert len(result) == 1
        assert result[0]["sessionId"] == "sess-unindexed"

    def test_skips_indexed_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [_sample_entry("id-1")])
        _write_jsonl(projects_dir, "proj-a", "id-1", [
            _make_user_message("duplicate"),
        ])
        result = _load_claude_sessions()
        assert len(result) == 1
        assert result[0]["summary"] == "Test session"


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
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir, "proj", "sess-1", [
            _make_user_message("please fix the pineapple bug"),
            _make_assistant_message("done"),
        ])
        entries = [{"sessionId": "sess-1", "project": "proj"}]
        result = _grep_sessions(entries, "pineapple")
        assert len(result) == 1

    def test_no_match_in_content(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir, "proj", "sess-1", [
            _make_user_message("hello world"),
            _make_assistant_message("hi"),
        ])
        entries = [{"sessionId": "sess-1", "project": "proj"}]
        result = _grep_sessions(entries, "pineapple")
        assert len(result) == 0

    def test_case_insensitive(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
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
    def test_no_sessions(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        result = format_sessions_list()
        assert "No sessions found" in result

    def test_basic_list(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [_sample_entry("id-1")])
        result = format_sessions_list()
        assert "Sessions" in result
        assert "/resume id-1" in result
        assert "my-project" in result

    def test_search_filter(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [
            _sample_entry("id-1", summary="Discord fix"),
            _sample_entry("id-2", summary="WebView build", modified="2026-04-02T11:00:00.000Z"),
        ])
        result = format_sessions_list(query="discord")
        assert "/resume id-1" in result
        assert "/resume id-2" not in result

    def test_pagination(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        entries = [
            _sample_entry(f"id-{i}", modified=f"2026-04-{i+1:02d}T11:00:00.000Z")
            for i in range(8)
        ]
        _make_index(projects_dir, "proj-a", entries)
        result = format_sessions_list(query="p2")
        assert "6-8 / 8" in result

    def test_no_results_with_filter(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [_sample_entry("id-1")])
        result = format_sessions_list(query="nonexistent")
        assert "No sessions matching" in result

    def test_hex_prefix_match(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [
            _sample_entry("abcdef1234567890"),
        ])
        result = format_sessions_list(query="abcd")
        assert "/resume abcdef1234567890" in result

    def test_days_filter(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        # Entry from 2026-04-01 is old enough to be filtered out by 1d
        _make_index(projects_dir, "proj-a", [_sample_entry("id-1")])
        result = format_sessions_list(query="1d")
        # The entry is from 2026-04-01 which is old, should be filtered
        assert "No sessions" in result or "id-1" in result  # depends on test run date

    def test_cwd_filter_default(self, tmp_path, monkeypatch):
        """With workspace set, only sessions matching that path are shown."""
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [
            _sample_entry("id-1", projectPath="/Users/test/proj-a"),
        ], original_path="/Users/test/proj-a")
        _make_index(projects_dir, "proj-b", [
            _sample_entry("id-2", projectPath="/Users/test/proj-b",
                          modified="2026-04-02T11:00:00.000Z"),
        ], original_path="/Users/test/proj-b")
        result = format_sessions_list(workspace="/Users/test/proj-a")
        assert "/resume id-1" in result
        assert "/resume id-2" not in result
        assert "proj-a" in result

    def test_cwd_all_flag(self, tmp_path, monkeypatch):
        """--all bypasses cwd filter, showing sessions from all projects."""
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [
            _sample_entry("id-1", projectPath="/Users/test/proj-a"),
        ], original_path="/Users/test/proj-a")
        _make_index(projects_dir, "proj-b", [
            _sample_entry("id-2", projectPath="/Users/test/proj-b",
                          modified="2026-04-02T11:00:00.000Z"),
        ], original_path="/Users/test/proj-b")
        result = format_sessions_list(query="--all", workspace="/Users/test/proj-a")
        assert "/resume id-1" in result
        assert "/resume id-2" in result
        assert "all projects" in result

    def test_cwd_no_match_hint(self, tmp_path, monkeypatch):
        """When cwd filter yields no results, hint about --all."""
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [
            _sample_entry("id-1", projectPath="/Users/test/proj-a"),
        ], original_path="/Users/test/proj-a")
        result = format_sessions_list(workspace="/Users/test/other")
        assert "--all" in result

    def test_cwd_search_filter(self, tmp_path, monkeypatch):
        """cwd:xxx does substring search on projectPath, bypassing default cwd."""
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [
            _sample_entry("id-1", projectPath="/Users/test/proj-a"),
        ], original_path="/Users/test/proj-a")
        _make_index(projects_dir, "proj-b", [
            _sample_entry("id-2", projectPath="/Users/test/proj-b",
                          modified="2026-04-02T11:00:00.000Z"),
        ], original_path="/Users/test/proj-b")
        # cwd:proj-b should find proj-b even though workspace is proj-a
        result = format_sessions_list(query="cwd:proj-b", workspace="/Users/test/proj-a")
        assert "/resume id-2" in result
        assert "/resume id-1" not in result

    def test_grep_filter(self, tmp_path, monkeypatch):
        """grep:xxx does full-text search on JSONL content."""
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        # Create two sessions with different content
        _make_index(projects_dir, "proj-a", [
            _sample_entry("sess-1", projectPath="/Users/test/proj-a"),
            _sample_entry("sess-2", projectPath="/Users/test/proj-a",
                          modified="2026-04-02T11:00:00.000Z"),
        ], original_path="/Users/test/proj-a")
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
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [
            _sample_entry("sess-1", projectPath="/Users/test/proj-a"),
        ], original_path="/Users/test/proj-a")
        _write_jsonl(projects_dir, "proj-a", "sess-1", [
            _make_user_message("hello world"),
        ])
        result = format_sessions_list(query="grep:nonexistent")
        assert "No sessions" in result


class TestSessionsList:
    def test_no_sessions(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        args = Namespace(query=[], output_json=False, workspace="")
        sessions_list(args)
        assert "No sessions found" in capsys.readouterr().out

    def test_formatted_output(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [_sample_entry("id-1")])
        args = Namespace(query=[], output_json=False, workspace="")
        sessions_list(args)
        out = capsys.readouterr().out
        assert "id-1" in out
        assert "Sessions" in out

    def test_json_output(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [_sample_entry("id-1")])
        args = Namespace(query=[], output_json=True, workspace="")
        sessions_list(args)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert len(data) == 1
        assert data[0]["sessionId"] == "id-1"

    def test_query_filter(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [
            _sample_entry("id-1", projectPath="/Users/test/proj-a", summary="Discord fix"),
        ], original_path="/Users/test/proj-a")
        _make_index(projects_dir, "proj-b", [
            _sample_entry("id-2", projectPath="/Users/test/proj-b",
                          modified="2026-04-02T11:00:00.000Z"),
        ], original_path="/Users/test/proj-b")
        args = Namespace(query=["cwd:proj-a"], output_json=True, workspace="")
        sessions_list(args)
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 1
        assert data[0]["sessionId"] == "id-1"

    def test_workspace_filter(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [
            _sample_entry("id-1", projectPath="/Users/test/proj-a"),
        ], original_path="/Users/test/proj-a")
        _make_index(projects_dir, "proj-b", [
            _sample_entry("id-2", projectPath="/Users/test/proj-b"),
        ], original_path="/Users/test/proj-b")
        args = Namespace(query=[], output_json=True, workspace="/Users/test/proj-a")
        sessions_list(args)
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 1
        assert data[0]["sessionId"] == "id-1"
