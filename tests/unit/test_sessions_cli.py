"""Tests for sessions_cli — CLI subcommand handlers."""

import json
from argparse import Namespace
from pathlib import Path

import pytest

from boxagent.sessions_cli import (
    _load_all_sessions,
    _parse_jsonl_metadata,
    _truncate,
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


class TestLoadAllSessions:
    def test_empty_when_no_projects(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        assert _load_all_sessions() == []

    def test_empty_when_no_index_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        (tmp_path / "projects" / "some-project").mkdir(parents=True)
        assert _load_all_sessions() == []

    def test_loads_entries_from_index(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [_sample_entry("id-1")])
        result = _load_all_sessions()
        assert len(result) == 1
        assert result[0]["sessionId"] == "id-1"

    def test_loads_unindexed_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _write_jsonl(projects_dir, "proj", "sess-unindexed", [
            _make_user_message("unindexed prompt", cwd="/Users/test/proj"),
            _make_assistant_message(),
        ])
        result = _load_all_sessions()
        assert len(result) == 1
        assert result[0]["sessionId"] == "sess-unindexed"
        assert result[0]["firstPrompt"] == "unindexed prompt"
        assert result[0]["projectPath"] == "/Users/test/proj"

    def test_skips_indexed_jsonl(self, tmp_path, monkeypatch):
        """Jsonl files already covered by an index should not be duplicated."""
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [_sample_entry("id-1")])
        # Also create a .jsonl with the same session id
        _write_jsonl(projects_dir, "proj-a", "id-1", [
            _make_user_message("duplicate"),
        ])
        result = _load_all_sessions()
        assert len(result) == 1
        assert result[0]["summary"] == "Test session"  # from index, not jsonl

    def test_multiple_projects(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [
            _sample_entry("id-1", modified="2026-04-01T11:00:00.000Z"),
        ])
        _make_index(projects_dir, "proj-b", [
            _sample_entry("id-2", modified="2026-04-02T11:00:00.000Z"),
        ])
        result = _load_all_sessions()
        assert len(result) == 2
        # Sorted by modified desc
        assert result[0]["sessionId"] == "id-2"
        assert result[1]["sessionId"] == "id-1"

    def test_sets_project_path_from_original(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        entry = _sample_entry("id-1")
        del entry["projectPath"]
        _make_index(projects_dir, "proj-a", [entry], original_path="/Users/test/proj-a")
        result = _load_all_sessions()
        assert result[0]["projectPath"] == "/Users/test/proj-a"

    def test_skips_bad_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        proj_dir = tmp_path / "projects" / "bad"
        proj_dir.mkdir(parents=True)
        (proj_dir / "sessions-index.json").write_text("not json", encoding="utf-8")
        assert _load_all_sessions() == []


class TestTruncate:
    def test_short_string(self):
        assert _truncate("hello", 10) == "hello"

    def test_long_string(self):
        assert _truncate("a" * 50, 10) == "aaaaaaa..."

    def test_collapses_whitespace(self):
        assert _truncate("hello   world\nnew", 40) == "hello world new"


class TestSessionsList:
    def test_no_sessions(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        args = Namespace(project="", output_json=False)
        sessions_list(args)
        assert "No sessions found" in capsys.readouterr().out

    def test_table_output(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [_sample_entry("id-1")])
        args = Namespace(project="", output_json=False)
        sessions_list(args)
        out = capsys.readouterr().out
        assert "id-1" in out
        assert "SESSION_ID" in out

    def test_json_output(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [_sample_entry("id-1")])
        args = Namespace(project="", output_json=True)
        sessions_list(args)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert len(data) == 1
        assert data[0]["sessionId"] == "id-1"

    def test_project_filter(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("boxagent.sessions_cli.CLAUDE_DIR", tmp_path)
        projects_dir = tmp_path / "projects"
        _make_index(projects_dir, "proj-a", [
            _sample_entry("id-1", projectPath="/Users/test/proj-a"),
        ], original_path="/Users/test/proj-a")
        _make_index(projects_dir, "proj-b", [
            _sample_entry("id-2", projectPath="/Users/test/proj-b"),
        ], original_path="/Users/test/proj-b")
        args = Namespace(project="proj-a", output_json=True)
        sessions_list(args)
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 1
        assert data[0]["sessionId"] == "id-1"
