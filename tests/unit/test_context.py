"""Tests for boxagent.context — session context injection."""

import textwrap
from pathlib import Path

from boxagent.context import build_session_context, _read_boxagent_md


class TestBuildSessionContext:
    def test_basic_fields(self):
        ctx = build_session_context(
            bot_name="test-bot",
            display_name="Test Bot",
            node_id="my-node",
            ai_backend="claude-cli",
            model="opus",
            workspace="/tmp/ws",
            config_dir="/tmp/cfg",
        )
        assert "[BoxAgent Context]" in ctx
        assert "[/BoxAgent Context]" in ctx
        assert "bot: test-bot" in ctx
        assert "display_name: Test Bot" in ctx
        assert "node: my-node" in ctx
        assert "backend: claude-cli" in ctx
        assert "model: opus" in ctx
        assert "workspace: /tmp/ws" in ctx

    def test_empty_display_name_skipped(self):
        ctx = build_session_context(bot_name="test-bot", display_name="")
        assert "bot: test-bot" in ctx
        assert "display_name:" not in ctx

    def test_empty_node_shows_any(self):
        ctx = build_session_context(node_id="")
        assert "node: (any)" in ctx

    def test_empty_model_shows_default(self):
        ctx = build_session_context(model="")
        assert "model: default" in ctx

    def test_reads_config_boxagent_md(self, tmp_path):
        (tmp_path / "BOXAGENT.md").write_text("config instructions here")
        ctx = build_session_context(config_dir=str(tmp_path))
        assert "# From config/BOXAGENT.md" in ctx
        assert "config instructions here" in ctx

    def test_reads_workspace_boxagent_md(self, tmp_path):
        (tmp_path / "BOXAGENT.md").write_text("workspace instructions here")
        ctx = build_session_context(workspace=str(tmp_path))
        assert "# From workspace/BOXAGENT.md" in ctx
        assert "workspace instructions here" in ctx

    def test_reads_both_md_files(self, tmp_path):
        cfg_dir = tmp_path / "cfg"
        ws_dir = tmp_path / "ws"
        cfg_dir.mkdir()
        ws_dir.mkdir()
        (cfg_dir / "BOXAGENT.md").write_text("from config")
        (ws_dir / "BOXAGENT.md").write_text("from workspace")

        ctx = build_session_context(
            config_dir=str(cfg_dir),
            workspace=str(ws_dir),
        )
        assert "from config" in ctx
        assert "from workspace" in ctx

    def test_missing_md_files_no_error(self):
        ctx = build_session_context(
            config_dir="/nonexistent/path",
            workspace="/also/nonexistent",
        )
        assert "[BoxAgent Context]" in ctx
        assert "BOXAGENT.md" not in ctx

    def test_empty_md_file_skipped(self, tmp_path):
        (tmp_path / "BOXAGENT.md").write_text("   \n  ")
        ctx = build_session_context(config_dir=str(tmp_path))
        assert "BOXAGENT.md" not in ctx


class TestReadBoxagentMd:
    def test_reads_existing_file(self, tmp_path):
        (tmp_path / "BOXAGENT.md").write_text("hello")
        assert _read_boxagent_md(str(tmp_path)) == "hello"

    def test_returns_empty_for_missing(self, tmp_path):
        assert _read_boxagent_md(str(tmp_path)) == ""

    def test_returns_empty_for_empty_dir(self):
        assert _read_boxagent_md("") == ""

    def test_strips_whitespace(self, tmp_path):
        (tmp_path / "BOXAGENT.md").write_text("\n  content  \n\n")
        assert _read_boxagent_md(str(tmp_path)) == "content"
