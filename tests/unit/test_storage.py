"""Unit tests for storage — sessions.yaml management."""

import json
import os
import signal
from pathlib import Path

import pytest

from boxagent.storage import Storage


@pytest.fixture
def storage(tmp_path):
    return Storage(
        local_dir=tmp_path / "boxagent-local",
        codex_sessions_dir=tmp_path / "codex-sessions",
    )


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


class TestSessionTracking:
    def test_save_and_load_session(self, storage):
        """Save a session_id, then load it back."""
        storage.save_session("test-bot", "sess_abc")
        assert storage.load_session("test-bot") == "sess_abc"

    def test_scoped_sessions_are_isolated_by_backend_and_workspace(self, storage):
        storage.save_session(
            "test-bot",
            "sess_claude_a",
            backend="claude-cli",
            workspace="/tmp/work-a",
        )
        storage.save_session(
            "test-bot",
            "sess_claude_b",
            backend="claude-cli",
            workspace="/tmp/work-b",
        )
        storage.save_session(
            "test-bot",
            "sess_codex_a",
            backend="codex-cli",
            workspace="/tmp/work-a",
        )

        assert storage.load_session(
            "test-bot",
            backend="claude-cli",
            workspace="/tmp/work-a",
        ) == "sess_claude_a"
        assert storage.load_session(
            "test-bot",
            backend="claude-cli",
            workspace="/tmp/work-b",
        ) == "sess_claude_b"
        assert storage.load_session(
            "test-bot",
            backend="codex-cli",
            workspace="/tmp/work-a",
        ) == "sess_codex_a"
        assert storage.load_session(
            "test-bot",
            backend="claude-cli",
            workspace="/tmp/missing",
        ) is None

    def test_load_missing_session(self, storage):
        """Loading nonexistent bot returns None."""
        assert storage.load_session("nonexistent") is None

    def test_clear_session(self, storage):
        """Clear removes the session entry."""
        storage.save_session("test-bot", "sess_abc")
        storage.clear_session("test-bot")
        assert storage.load_session("test-bot") is None

    def test_multiple_bots(self, storage):
        """Multiple bots can have independent sessions."""
        storage.save_session("bot-a", "sess_1")
        storage.save_session("bot-b", "sess_2")
        assert storage.load_session("bot-a") == "sess_1"
        assert storage.load_session("bot-b") == "sess_2"

    def test_save_session_tracks_history(self, storage):
        storage.save_session("test-bot", "sess_1")
        storage.save_session("test-bot", "sess_2")

        history = storage.list_session_history("test-bot")

        assert [entry["session_id"] for entry in history] == [
            "sess_2",
            "sess_1",
        ]
        assert "saved_at" in history[0]

    def test_list_session_history_filters_by_scope(self, storage):
        storage.save_session(
            "test-bot",
            "sess_a",
            backend="claude-cli",
            workspace="/tmp/work-a",
        )
        storage.save_session(
            "test-bot",
            "sess_b",
            backend="claude-cli",
            workspace="/tmp/work-b",
        )

        history = storage.list_session_history(
            "test-bot",
            backend="claude-cli",
            workspace="/tmp/work-a",
        )

        assert [entry["session_id"] for entry in history] == ["sess_a"]
        assert history[0]["workspace"] == str(Path("/tmp/work-a").resolve())


class TestCodexSessionTracking:
    def test_lists_codex_sessions_for_workspace(self, storage, tmp_path):
        work_a = tmp_path / "workspace-a"
        work_b = tmp_path / "workspace-b"
        work_a.mkdir()
        work_b.mkdir()

        sessions_dir = tmp_path / "codex-sessions" / "2026" / "03" / "22"
        matching = sessions_dir / "rollout-matching.jsonl"
        other = sessions_dir / "rollout-other.jsonl"

        _write_jsonl(
            matching,
            [
                {
                    "type": "session_meta",
                    "timestamp": "2026-03-22T13:54:18.381Z",
                    "payload": {
                        "id": "sess-match",
                        "timestamp": "2026-03-22T13:54:17.836Z",
                        "cwd": str(work_a),
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "fix /cancel creating a new conversation",
                    },
                },
            ],
        )
        _write_jsonl(
            other,
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "sess-other",
                        "timestamp": "2026-03-22T11:00:00.000Z",
                        "cwd": str(work_b),
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "this should be filtered out",
                    },
                },
            ],
        )
        os.utime(matching, (200, 200))
        os.utime(other, (100, 100))

        entries = storage.list_codex_session_history(str(work_a))

        assert len(entries) == 1
        assert entries[0]["session_id"] == "sess-match"
        assert entries[0]["cwd"] == str(work_a)
        assert entries[0]["preview"] == "fix /cancel creating a new conversation"
        assert entries[0]["path"] == str(matching)

    def test_build_codex_resume_context_recovers_transcript(
        self, storage, tmp_path
    ):
        workspace = tmp_path / "workspace-a"
        workspace.mkdir()
        session_path = (
            tmp_path
            / "codex-sessions"
            / "2026"
            / "03"
            / "22"
            / "rollout-sample.jsonl"
        )
        _write_jsonl(
            session_path,
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "sess-soft-resume",
                        "timestamp": "2026-03-22T13:54:17.836Z",
                        "cwd": str(workspace),
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "看看 /cancel 为什么会直接新建对话",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "message": "我先检查 router 和 cancel 流程。",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "turn_aborted",
                        "reason": "interrupted",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "顺手加个 /resume",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "last_agent_message": "已经定位到问题，接下来会补测试。",
                    },
                },
            ],
        )

        context = storage.build_codex_resume_context(session_path)

        assert "soft restore" in context.lower()
        assert "sess-soft-resume" in context
        assert "看看 /cancel 为什么会直接新建对话" in context
        assert "已经定位到问题，接下来会补测试。" in context
        assert "interrupted turn" in context.lower()


class TestAutoCreateDirs:
    def test_dirs_created_on_first_access(self, tmp_path):
        """Local directory tree auto-created."""
        local_dir = tmp_path / "boxagent-local"
        storage = Storage(local_dir=local_dir)
        storage.save_session("test", "sess")

        assert (local_dir / "sessions.yaml").exists()
