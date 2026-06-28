"""Unit tests for workgroup.workspace_templates."""

from boxagent.workgroup.workspace_templates import (
    ADMIN_CLAUDE_MD,
    ADMIN_SKILL_MD,
    SPECIALIST_CLAUDE_MD,
    SPECIALIST_SKILL_MD,
    SUPERBOSS_REF,
    SUPERCREW_REF,
    seed_admin_workspace,
    seed_specialist_workspace,
)

class TestSeedAdminWorkspace:
    def test_creates_all_files(self, tmp_path):
        ws = str(tmp_path / "admin")
        created = seed_admin_workspace(ws, "test-workgroup")
        assert ".claude/CLAUDE.md" in created
        assert ".claude/skills/superboss/SKILL.md" in created
        assert ".claude/skills/superboss/references/templates.md" in created
        assert "HEARTBEAT.md" in created

    def test_claude_md_contains_workgroup_name(self, tmp_path):
        ws = str(tmp_path / "admin")
        seed_admin_workspace(ws, "my-workgroup")
        content = (tmp_path / "admin" / ".claude" / "CLAUDE.md").read_text()
        assert "my-workgroup" in content

    def test_system_layer_overwrites(self, tmp_path):
        ws = str(tmp_path / "admin")
        seed_admin_workspace(ws, "workgroup")
        # Modify system file
        claude_md = tmp_path / "admin" / ".claude" / "CLAUDE.md"
        claude_md.write_text("custom content")
        # Re-seed should overwrite system files
        written = seed_admin_workspace(ws, "workgroup")
        assert ".claude/CLAUDE.md" in written
        assert claude_md.read_text() != "custom content"

    def test_user_layer_not_overwritten(self, tmp_path):
        ws = str(tmp_path / "admin")
        seed_admin_workspace(ws, "workgroup")
        # Modify user file
        heartbeat = tmp_path / "admin" / "HEARTBEAT.md"
        heartbeat.write_text("my custom checklist")
        # Re-seed should NOT overwrite user files
        seed_admin_workspace(ws, "workgroup")
        assert heartbeat.read_text() == "my custom checklist"

    def test_system_layer_skip_if_unchanged(self, tmp_path):
        ws = str(tmp_path / "admin")
        seed_admin_workspace(ws, "workgroup")
        # Re-seed with same content should report nothing changed
        written = seed_admin_workspace(ws, "workgroup")
        assert ".claude/CLAUDE.md" not in written

    def test_empty_workspace_returns_empty(self):
        assert seed_admin_workspace("", "workgroup") == []

    def test_worktrees_dir_in_claude_md(self, tmp_path):
        ws = str(tmp_path / "workgroup" / "admin")
        seed_admin_workspace(ws, "workgroup")
        content = (tmp_path / "workgroup" / "admin" / ".claude" / "CLAUDE.md").read_text()
        assert "worktrees" in content


class TestSeedSpecialistWorkspace:
    def test_creates_all_files(self, tmp_path):
        ws = str(tmp_path / "specialists" / "dev-1")
        created = seed_specialist_workspace(ws, "dev-1", "test-workgroup")
        assert ".claude/CLAUDE.md" in created
        assert ".claude/skills/supercrew/SKILL.md" in created
        assert ".claude/skills/supercrew/references/templates.md" in created

    def test_contains_specialist_name(self, tmp_path):
        ws = str(tmp_path / "specialists" / "dev-alice")
        seed_specialist_workspace(ws, "dev-alice", "my-workgroup")
        content = (tmp_path / "specialists" / "dev-alice" / ".claude" / "CLAUDE.md").read_text()
        assert "dev-alice" in content
        assert "my-workgroup" in content

    def test_empty_workspace_returns_empty(self):
        assert seed_specialist_workspace("", "dev", "workgroup") == []


class TestTemplateFormat:
    """Ensure all templates can be .format()-ed without KeyError."""

    def test_admin_claude_md(self):
        result = ADMIN_CLAUDE_MD.format(
            workgroup_name="test", worktrees_dir="/tmp/wt",
        )
        assert "test" in result

    def test_admin_skill_md(self):
        result = ADMIN_SKILL_MD.format(superboss_ref=SUPERBOSS_REF)
        assert "Super Boss" in result

    def test_specialist_claude_md(self):
        result = SPECIALIST_CLAUDE_MD.format(
            specialist_name="dev-1", workgroup_name="workgroup",
            supercrew_ref=SUPERCREW_REF, worktrees_dir="/tmp/wt",
        )
        assert "dev-1" in result

    def test_specialist_skill_md(self):
        result = SPECIALIST_SKILL_MD.format(supercrew_ref=SUPERCREW_REF, workgroup_name="test-workgroup")
        assert "Super Crew" in result

