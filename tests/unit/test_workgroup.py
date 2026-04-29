"""Unit tests for workgroup package — manager, heartbeat, templates."""

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from boxagent.workgroup.manager import (
    WorkgroupManager,
    format_running_tasks,
    _extract_specialist_response,
)
from boxagent.workgroup.heartbeat import (
    HeartbeatManager,
    is_silent_reply,
    _build_heartbeat_prompt,
    _extract_action,
)
from boxagent.workgroup.workspace_templates import (
    seed_admin_workspace,
    seed_specialist_workspace,
    ADMIN_CLAUDE_MD,
    ADMIN_SKILL_MD,
    SPECIALIST_CLAUDE_MD,
    SPECIALIST_SKILL_MD,
    SUPERBOSS_REF,
    SUPERCREW_REF,
)


# ---------------------------------------------------------------------------
# format_running_tasks
# ---------------------------------------------------------------------------


class TestFormatRunningTasks:
    def test_empty_list(self):
        assert format_running_tasks([]) == "No specialist tasks currently running."

    def test_none(self):
        assert format_running_tasks(None) == "No specialist tasks currently running."

    def test_single_active_task(self):
        tasks = [
            {"task_id": "dev-1", "target": "dev", "started_at": time.time() - 90, "active": True},
        ]
        result = format_running_tasks(tasks)
        assert "dev-1" in result
        assert "dev" in result
        assert "[active]" in result
        assert "1m" in result

    def test_queued_task(self):
        tasks = [
            {"task_id": "pm-2", "target": "pm", "started_at": time.time() - 30, "active": False},
        ]
        result = format_running_tasks(tasks)
        assert "[queued]" in result

    def test_no_started_at(self):
        tasks = [{"task_id": "x-1", "target": "x", "active": False}]
        result = format_running_tasks(tasks)
        assert "x-1" in result
        # No elapsed time shown when started_at is missing
        assert "(running" not in result


# ---------------------------------------------------------------------------
# _extract_specialist_response
# ---------------------------------------------------------------------------


class TestExtractSpecialistResponse:
    def test_with_tags(self):
        text = "Thinking...\n<specialist_response>\nDone. Fixed the bug.\n</specialist_response>"
        assert _extract_specialist_response(text) == "Done. Fixed the bug."

    def test_without_tags_fallback(self):
        text = "Just a plain response"
        assert _extract_specialist_response(text) == "Just a plain response"

    def test_extra_content_after_tags(self):
        text = "<specialist_response>Result here</specialist_response>\ntrailing"
        assert _extract_specialist_response(text) == "Result here"

    def test_multiline_content(self):
        text = "<specialist_response>\nLine 1\nLine 2\nLine 3\n</specialist_response>"
        assert _extract_specialist_response(text) == "Line 1\nLine 2\nLine 3"

    def test_empty_tags(self):
        text = "<specialist_response></specialist_response>"
        assert _extract_specialist_response(text) == ""


# ---------------------------------------------------------------------------
# is_silent_reply
# ---------------------------------------------------------------------------


class TestIsSilentReply:
    def test_exact_no_reply(self):
        assert is_silent_reply("NO_REPLY") is True

    def test_exact_heartbeat_ok(self):
        assert is_silent_reply("HEARTBEAT_OK") is True

    def test_empty(self):
        assert is_silent_reply("") is True

    def test_whitespace(self):
        assert is_silent_reply("  \n  ") is True

    def test_embedded_no_reply(self):
        assert is_silent_reply("Some thinking...\n\nNO_REPLY") is True

    def test_embedded_heartbeat_ok(self):
        assert is_silent_reply("All good. HEARTBEAT_OK") is True

    def test_action_needed(self):
        assert is_silent_reply("Check dev-mac status") is False

    def test_case_insensitive(self):
        assert is_silent_reply("no_reply") is True


# ---------------------------------------------------------------------------
# _extract_action
# ---------------------------------------------------------------------------


class TestExtractAction:
    def test_with_tags(self):
        text = "Let me think...\n<heartbeat_action>Check pm-ux</heartbeat_action>"
        assert _extract_action(text) == "Check pm-ux"

    def test_no_reply_tag(self):
        text = "<heartbeat_action>NO_REPLY</heartbeat_action>"
        assert _extract_action(text) == "NO_REPLY"

    def test_without_tags_fallback(self):
        text = "NO_REPLY"
        assert _extract_action(text) == "NO_REPLY"

    def test_multiline_action(self):
        text = "<heartbeat_action>\nDo this\nThen that\n</heartbeat_action>"
        assert _extract_action(text) == "Do this\nThen that"


# ---------------------------------------------------------------------------
# _build_heartbeat_prompt
# ---------------------------------------------------------------------------


class TestBuildHeartbeatPrompt:
    def test_basic_prompt(self):
        prompt = _build_heartbeat_prompt("war-room", "- Check tasks")
        assert "HEARTBEAT CHECK" in prompt
        assert "war-room" in prompt
        assert "Check tasks" in prompt
        assert "<heartbeat_action>" in prompt

    def test_includes_uptime(self):
        prompt = _build_heartbeat_prompt("wg", "checklist", uptime_seconds=7500)
        assert "2h 5m" in prompt

    def test_short_uptime(self):
        prompt = _build_heartbeat_prompt("wg", "checklist", uptime_seconds=125)
        assert "2m 5s" in prompt

    def test_includes_running_tasks(self):
        tasks = [
            {"task_id": "dev-1", "target": "dev", "started_at": time.time() - 60, "active": True},
        ]
        prompt = _build_heartbeat_prompt("wg", "checklist", running_tasks=tasks)
        assert "dev-1" in prompt
        assert "[active]" in prompt

    def test_no_running_tasks(self):
        prompt = _build_heartbeat_prompt("wg", "checklist", running_tasks=[])
        assert "No specialist tasks currently running" in prompt

    def test_read_only_instruction(self):
        prompt = _build_heartbeat_prompt("wg", "checklist")
        assert "read-only" in prompt
        assert "NO execution permissions" in prompt


# ---------------------------------------------------------------------------
# seed_admin_workspace
# ---------------------------------------------------------------------------


class TestSeedAdminWorkspace:
    def test_creates_all_files(self, tmp_path):
        ws = str(tmp_path / "admin")
        created = seed_admin_workspace(ws, "test-wg", ["spec-1", "spec-2"])
        assert ".claude/CLAUDE.md" in created
        assert ".claude/skills/superboss/SKILL.md" in created
        assert ".claude/skills/superboss/references/templates.md" in created
        assert "HEARTBEAT.md" in created

    def test_claude_md_contains_wg_name(self, tmp_path):
        ws = str(tmp_path / "admin")
        seed_admin_workspace(ws, "my-workgroup", ["dev-1"])
        content = (tmp_path / "admin" / ".claude" / "CLAUDE.md").read_text()
        assert "my-workgroup" in content
        assert "dev-1" in content

    def test_system_layer_overwrites(self, tmp_path):
        ws = str(tmp_path / "admin")
        seed_admin_workspace(ws, "wg", ["s1"])
        # Modify system file
        claude_md = tmp_path / "admin" / ".claude" / "CLAUDE.md"
        claude_md.write_text("custom content")
        # Re-seed should overwrite system files
        written = seed_admin_workspace(ws, "wg", ["s1"])
        assert ".claude/CLAUDE.md" in written
        assert claude_md.read_text() != "custom content"

    def test_user_layer_not_overwritten(self, tmp_path):
        ws = str(tmp_path / "admin")
        seed_admin_workspace(ws, "wg", ["s1"])
        # Modify user file
        heartbeat = tmp_path / "admin" / "HEARTBEAT.md"
        heartbeat.write_text("my custom checklist")
        # Re-seed should NOT overwrite user files
        seed_admin_workspace(ws, "wg", ["s1"])
        assert heartbeat.read_text() == "my custom checklist"

    def test_system_layer_skip_if_unchanged(self, tmp_path):
        ws = str(tmp_path / "admin")
        seed_admin_workspace(ws, "wg", ["s1"])
        # Re-seed with same content should report nothing changed
        written = seed_admin_workspace(ws, "wg", ["s1"])
        assert ".claude/CLAUDE.md" not in written

    def test_empty_workspace_returns_empty(self):
        assert seed_admin_workspace("", "wg", []) == []

    def test_no_specialists(self, tmp_path):
        ws = str(tmp_path / "admin")
        seed_admin_workspace(ws, "wg", [])
        content = (tmp_path / "admin" / ".claude" / "CLAUDE.md").read_text()
        assert "No specialists configured" in content

    def test_worktrees_dir_in_claude_md(self, tmp_path):
        ws = str(tmp_path / "wg" / "admin")
        seed_admin_workspace(ws, "wg", ["s1"])
        content = (tmp_path / "wg" / "admin" / ".claude" / "CLAUDE.md").read_text()
        assert "worktrees" in content


class TestSeedSpecialistWorkspace:
    def test_creates_all_files(self, tmp_path):
        ws = str(tmp_path / "specialists" / "dev-1")
        created = seed_specialist_workspace(ws, "dev-1", "test-wg")
        assert ".claude/CLAUDE.md" in created
        assert ".claude/skills/supercrew/SKILL.md" in created
        assert ".claude/skills/supercrew/references/templates.md" in created

    def test_contains_specialist_name(self, tmp_path):
        ws = str(tmp_path / "specialists" / "dev-alice")
        seed_specialist_workspace(ws, "dev-alice", "my-wg")
        content = (tmp_path / "specialists" / "dev-alice" / ".claude" / "CLAUDE.md").read_text()
        assert "dev-alice" in content
        assert "my-wg" in content

    def test_empty_workspace_returns_empty(self):
        assert seed_specialist_workspace("", "dev", "wg") == []


# ---------------------------------------------------------------------------
# Template format safety
# ---------------------------------------------------------------------------


class TestTemplateFormat:
    """Ensure all templates can be .format()-ed without KeyError."""

    def test_admin_claude_md(self):
        result = ADMIN_CLAUDE_MD.format(
            wg_name="test", specialists_block="- s1",
            superboss_ref=SUPERBOSS_REF, worktrees_dir="/tmp/wt",
        )
        assert "test" in result

    def test_admin_skill_md(self):
        result = ADMIN_SKILL_MD.format(superboss_ref=SUPERBOSS_REF)
        assert "Super Boss" in result

    def test_specialist_claude_md(self):
        result = SPECIALIST_CLAUDE_MD.format(
            sp_name="dev-1", wg_name="wg",
            supercrew_ref=SUPERCREW_REF, worktrees_dir="/tmp/wt",
        )
        assert "dev-1" in result

    def test_specialist_skill_md(self):
        result = SPECIALIST_SKILL_MD.format(supercrew_ref=SUPERCREW_REF, wg_name="test-wg")
        assert "Super Crew" in result


# ---------------------------------------------------------------------------
# WorkgroupManager — unit tests for pure methods
# ---------------------------------------------------------------------------


class TestWorkgroupManagerPureMethods:
    def _make_manager(self, tmp_path):
        from boxagent.config import WorkgroupConfig, SpecialistConfig
        wg_cfg = WorkgroupConfig(
            name="test-wg",
            workspace=str(tmp_path / "workspace"),
        )
        wg_cfg.specialists["dev-1"] = SpecialistConfig(
            name="dev-1", model="sonnet", workspace=str(tmp_path / "dev-1"),
        )
        mgr = WorkgroupManager(
            config={"test-wg": wg_cfg},
            local_dir=tmp_path / "local",
            start_time=time.time(),
        )
        return mgr

    def test_list_specialists_empty(self, tmp_path):
        from boxagent.config import WorkgroupConfig
        mgr = WorkgroupManager(
            config={"wg": WorkgroupConfig(name="wg", workspace=str(tmp_path))},
            local_dir=tmp_path,
        )
        result = mgr.list_specialists("wg")
        assert result["ok"] is True
        assert result["specialists"] == []

    def test_list_specialists_with_entries(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.list_specialists("test-wg")
        assert result["ok"] is True
        assert len(result["specialists"]) == 1
        assert result["specialists"][0]["name"] == "dev-1"

    def test_list_specialists_wrong_wg(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.list_specialists("nonexistent")
        assert result["ok"] is True
        assert result["specialists"] == []

    def test_get_task_result_not_found(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.get_task_result("fake-id")
        assert result["ok"] is False

    def test_get_task_result_found(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        mgr._task_results["dev-1-1"] = {"status": "done", "result": "ok"}
        result = mgr.get_task_result("dev-1-1")
        assert result["ok"] is True
        assert result["status"] == "done"

    def test_get_running_tasks_none(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        assert mgr._get_running_tasks("test-wg") == []

    def test_get_running_tasks_with_running(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        mgr._task_results["dev-1-1"] = {
            "status": "running", "target": "dev-1", "started_at": time.time(),
        }
        # No pool, so active=False
        tasks = mgr._get_running_tasks("test-wg")
        assert len(tasks) == 1
        assert tasks[0]["task_id"] == "dev-1-1"
        assert tasks[0]["active"] is False

    def test_get_running_tasks_ignores_done(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        mgr._task_results["dev-1-1"] = {"status": "done", "target": "dev-1"}
        assert mgr._get_running_tasks("test-wg") == []

    def test_save_and_load_specialists(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        (tmp_path / "local").mkdir(exist_ok=True)
        from boxagent.config import SpecialistConfig
        sp = SpecialistConfig(name="dynamic-1", model="haiku", workspace="/tmp/dyn")
        mgr._save_specialist("test-wg", sp)
        loaded = mgr._load_saved_specialists("test-wg")
        assert "dynamic-1" in loaded
        assert loaded["dynamic-1"].model == "haiku"

    def test_remove_saved_specialist(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        (tmp_path / "local").mkdir(exist_ok=True)
        from boxagent.config import SpecialistConfig
        sp = SpecialistConfig(name="dynamic-1", model="haiku")
        mgr._save_specialist("test-wg", sp)
        mgr._remove_saved_specialist("test-wg", "dynamic-1")
        loaded = mgr._load_saved_specialists("test-wg")
        assert "dynamic-1" not in loaded

    def test_reset_specialist_not_found(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.reset_specialist("nonexistent")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# HeartbeatManager — read_heartbeat_md
# ---------------------------------------------------------------------------


class TestHeartbeatReadMd:
    def test_reads_file(self, tmp_path):
        (tmp_path / "HEARTBEAT.md").write_text("- Check tasks\n- Review work")
        hb = HeartbeatManager(
            wg_name="wg", admin_pool=None, admin_router=None,
            workspace=str(tmp_path), interval_seconds=60,
        )
        content = hb._read_heartbeat_md()
        assert "Check tasks" in content

    def test_missing_file(self, tmp_path):
        hb = HeartbeatManager(
            wg_name="wg", admin_pool=None, admin_router=None,
            workspace=str(tmp_path), interval_seconds=60,
        )
        assert hb._read_heartbeat_md() is None

    def test_empty_file(self, tmp_path):
        (tmp_path / "HEARTBEAT.md").write_text("")
        hb = HeartbeatManager(
            wg_name="wg", admin_pool=None, admin_router=None,
            workspace=str(tmp_path), interval_seconds=60,
        )
        assert hb._read_heartbeat_md() is None

    def test_empty_workspace(self):
        hb = HeartbeatManager(
            wg_name="wg", admin_pool=None, admin_router=None,
            workspace="", interval_seconds=60,
        )
        assert hb._read_heartbeat_md() is None


# ---------------------------------------------------------------------------
# HeartbeatManager — write_heartbeat_log
# ---------------------------------------------------------------------------


class TestHeartbeatLog:
    def test_writes_log(self, tmp_path):
        hb = HeartbeatManager(
            wg_name="wg", admin_pool=None, admin_router=None,
            workspace=str(tmp_path), interval_seconds=60,
        )
        hb._write_heartbeat_log("NO_REPLY", {
            "source_session_id": "abc",
            "fork_session_id": "def",
            "raw_response": "<heartbeat_action>NO_REPLY</heartbeat_action>",
            "prompt": "test prompt",
        })
        log = (tmp_path / "heartbeat.log").read_text()
        assert "source_session: abc" in log
        assert "fork_session:   def" in log
        assert "silent: True" in log
        assert "test prompt" in log

    def test_appends_multiple(self, tmp_path):
        hb = HeartbeatManager(
            wg_name="wg", admin_pool=None, admin_router=None,
            workspace=str(tmp_path), interval_seconds=60,
        )
        meta = {"source_session_id": "", "fork_session_id": "", "raw_response": "", "prompt": ""}
        hb._write_heartbeat_log("NO_REPLY", meta)
        hb._write_heartbeat_log("Do something", meta)
        log = (tmp_path / "heartbeat.log").read_text()
        assert log.count("===") == 4  # 2 entries × 2 separators each
