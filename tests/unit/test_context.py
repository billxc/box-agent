"""Tests for boxagent.context — session context injection."""

import textwrap
from pathlib import Path

from boxagent.router.context import build_session_context, _read_boxagent_md


class TestBuildSessionContext:
    def test_basic_fields(self):
        ctx = build_session_context(
            bot_name="test-bot",
            display_name="Test Bot",
            node_id="my-node",
            workspace="/tmp/ws",
            config_dir="/tmp/cfg",
        )
        assert "[BoxAgent Context]" in ctx
        assert "[/BoxAgent Context]" in ctx
        assert "bot: test-bot" in ctx
        assert "display_name: Test Bot" in ctx
        assert "node: my-node" in ctx
        assert "workspace: /tmp/ws" in ctx

    def test_empty_display_name_skipped(self):
        ctx = build_session_context(bot_name="test-bot", display_name="")
        assert "bot: test-bot" in ctx
        assert "display_name:" not in ctx

    def test_empty_node_shows_any(self):
        ctx = build_session_context(node_id="")
        assert "node: (any)" in ctx

    def test_reads_config_boxagent_md(self, tmp_path):
        (tmp_path / "BOXAGENT.md").write_text("config instructions here")
        ctx = build_session_context(config_dir=str(tmp_path))
        assert "BOXAGENT.md" in ctx
        assert "config instructions here" in ctx

    def test_reads_workspace_boxagent_md(self, tmp_path):
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        (ws_dir / "BOXAGENT.md").write_text("workspace instructions here")
        ctx = build_session_context(workspace=str(ws_dir))
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

    def test_dedup_same_boxagent_md(self, tmp_path):
        """When config and workspace point to the same dir, content appears only once."""
        (tmp_path / "BOXAGENT.md").write_text("shared content")
        ctx = build_session_context(
            config_dir=str(tmp_path),
            workspace=str(tmp_path),
        )
        assert ctx.count("shared content") == 1

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


class TestPeerInjection:
    """Peer list comes from cluster registry (env.peers), not peers.yaml."""

    def _env(self, *, peers=(), has_peer_channel=True):
        from boxagent.agent_env import AgentEnv
        return AgentEnv(
            bot_name="war-room",
            workgroup_role="admin",
            has_peer_channel=has_peer_channel,
            peers=tuple(peers),
        )

    def test_peer_section_omitted_when_no_peer_channel(self):
        env = self._env(has_peer_channel=False, peers=[
            {"name": "other", "machine": "mac", "online": True},
        ])
        ctx = build_session_context(env=env)
        assert "[Peer Messaging]" not in ctx

    def test_peer_section_present_with_no_peers(self):
        ctx = build_session_context(env=self._env(peers=[]))
        assert "[Peer Messaging]" in ctx
        assert "send_to_peer" in ctx
        assert "Peers:" not in ctx

    def test_peer_list_renders_local_and_remote(self):
        peers = [
            {"name": "war-room-2", "machine": "local", "online": True,
             "kind": "workgroup", "description": "local backup"},
            {"name": "mac-mini-wg", "machine": "macmini", "online": True,
             "kind": "workgroup", "description": "Mac Mini Admin"},
        ]
        ctx = build_session_context(env=self._env(peers=peers))
        assert "- war-room-2 (local) — local backup" in ctx
        assert "- mac-mini-wg (@macmini) — Mac Mini Admin" in ctx

    def test_offline_peer_marked(self):
        peers = [{"name": "old-mbp", "machine": "old-mbp", "online": False,
                  "kind": "workgroup"}]
        ctx = build_session_context(env=self._env(peers=peers))
        assert "(offline)" in ctx
        assert "old-mbp" in ctx

    def test_peers_yaml_no_longer_read(self, tmp_path):
        """Regression: peers.yaml on disk must NOT inject anything when env
        has empty peers list. Source of truth is cluster registry only."""
        (tmp_path / "peers.yaml").write_text(
            "ghost-peer:\n  description: this should not appear\n"
        )
        from boxagent.agent_env import AgentEnv
        env = AgentEnv(
            bot_name="war-room",
            workgroup_role="admin",
            has_peer_channel=True,
            config_dir=str(tmp_path),
            peers=(),
        )
        ctx = build_session_context(env=env)
        assert "ghost-peer" not in ctx
        assert "this should not appear" not in ctx
