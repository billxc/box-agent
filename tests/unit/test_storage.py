"""Unit tests for storage — sessions.yaml management."""

import json
import os
import signal
from pathlib import Path

import pytest

from boxagent.sessions import Storage


@pytest.fixture
def storage(tmp_path):
    return Storage(local_dir=tmp_path / "boxagent-local")


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


class TestSessionTracking:
    def test_save_and_load_session(self, storage):
        """Save a session_id, then load it back."""
        storage.save_session("test-bot", "sess_abc")
        loaded = storage.load_session("test-bot")
        assert loaded["session_id"] == "sess_abc"

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
        assert storage.load_session("bot-a")["session_id"] == "sess_1"
        assert storage.load_session("bot-b")["session_id"] == "sess_2"

    def test_save_session_tracks_history(self, storage):
        storage.save_session("test-bot", "sess_1")
        storage.save_session("test-bot", "sess_2")

        history = storage.list_session_history("test-bot")

        assert [entry["session_id"] for entry in history] == [
            "sess_2",
            "sess_1",
        ]
        assert "saved_at" in history[0]

    def test_save_session_chains_previous_sids_per_chat(self, storage):
        # First two turns share sid_a → no chain
        storage.save_session("bot", "sid_a", chat_id="c1")
        storage.save_session("bot", "sid_a", chat_id="c1")
        entry = storage.load_session("bot", "c1")
        assert entry["session_id"] == "sid_a"
        assert "previous_session_ids" not in entry

        # /compact rotates to sid_b → sid_a appended to chain
        storage.save_session("bot", "sid_b", chat_id="c1")
        entry = storage.load_session("bot", "c1")
        assert entry["session_id"] == "sid_b"
        assert entry["previous_session_ids"] == ["sid_a"]

        # Another rotation → sid_b at head, sid_a behind it
        storage.save_session("bot", "sid_c", chat_id="c1")
        entry = storage.load_session("bot", "c1")
        assert entry["session_id"] == "sid_c"
        assert entry["previous_session_ids"] == ["sid_b", "sid_a"]

        # Same sid again → no duplicate appended
        storage.save_session("bot", "sid_c", chat_id="c1")
        assert storage.load_session("bot", "c1")["previous_session_ids"] == ["sid_b", "sid_a"]

    def test_chain_is_per_chat_not_per_bot(self, storage):
        storage.save_session("bot", "sid_x", chat_id="c1")
        storage.save_session("bot", "sid_y", chat_id="c2")
        storage.save_session("bot", "sid_x2", chat_id="c1")
        assert storage.load_session("bot", "c1")["previous_session_ids"] == ["sid_x"]
        assert "previous_session_ids" not in storage.load_session("bot", "c2")


class TestCodexSessionTracking:
    """Codex session listing moved out of Storage in commit 3 of the
    history refactor — it's now in :class:`boxagent.history.CodexAgentHistory`.
    Equivalent coverage lives in tests/unit/test_history.py
    (TestCodexParsing) and tests/unit/test_codex_history_path.py."""


class TestAutoCreateDirs:
    def test_dirs_created_on_first_access(self, tmp_path):
        """Local directory tree auto-created."""
        local_dir = tmp_path / "boxagent-local"
        storage = Storage(local_dir=local_dir)
        storage.save_session("test", "session")

        assert (local_dir / "sessions.yaml").exists()


class TestLegacyWorkgroupPrefixMigration:
    """Pre-2026-05-10 specialist chat_ids used the prefix ``wg:``; renamed to
    ``workgroup:`` to drop the abbreviation. Storage migrates on init."""

    def test_renames_legacy_keys(self, tmp_path):
        import yaml as _yaml
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        (local_dir / "sessions.yaml").write_text(_yaml.safe_dump({
            "alice:wg:dev-1:": {"backend": "claude-cli", "session_id": "s1"},
            "bob:foo:": {"backend": "codex-cli", "session_id": "s2"},  # untouched
        }))

        Storage(local_dir=local_dir)  # triggers migration

        data = _yaml.safe_load((local_dir / "sessions.yaml").read_text())
        assert "alice:workgroup:dev-1:" in data
        assert "alice:wg:dev-1:" not in data
        assert "bob:foo:" in data  # non-wg keys preserved verbatim
        assert data["alice:workgroup:dev-1:"]["session_id"] == "s1"

    def test_idempotent_when_no_legacy_keys(self, tmp_path):
        import yaml as _yaml
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        original = {"alice:workgroup:dev-1:": {"session_id": "s1"}}
        (local_dir / "sessions.yaml").write_text(_yaml.safe_dump(original))
        original_mtime = (local_dir / "sessions.yaml").stat().st_mtime

        Storage(local_dir=local_dir)

        # File should be untouched (no rewrite triggered).
        assert (local_dir / "sessions.yaml").stat().st_mtime == original_mtime

    def test_skips_when_no_sessions_yaml(self, tmp_path):
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        # Just instantiate — should not raise.
        Storage(local_dir=local_dir)
