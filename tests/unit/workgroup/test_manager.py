"""Unit tests for workgroup.manager (pure methods)."""

import time

from boxagent.workgroup.manager import WorkgroupManager

class TestWorkgroupManagerPureMethods:
    def _make_manager(self, tmp_path):
        from boxagent.config import WorkgroupConfig, SpecialistConfig
        workgroup_config = WorkgroupConfig(
            name="test-workgroup",
            workspace=str(tmp_path / "workspace"),
        )
        workgroup_config.specialists["dev-1"] = SpecialistConfig(
            name="dev-1", model="sonnet", workspace=str(tmp_path / "dev-1"),
        )
        manager = WorkgroupManager(
            config={"test-workgroup": workgroup_config},
            local_dir=tmp_path / "local",
            start_time=time.time(),
        )
        return manager

    def test_list_specialists_empty(self, tmp_path):
        from boxagent.config import WorkgroupConfig
        manager = WorkgroupManager(
            config={"workgroup": WorkgroupConfig(name="workgroup", workspace=str(tmp_path))},
            local_dir=tmp_path,
        )
        result = manager.list_specialists("workgroup")
        assert result["ok"] is True
        assert result["specialists"] == []

    def test_list_specialists_with_entries(self, tmp_path):
        manager = self._make_manager(tmp_path)
        result = manager.list_specialists("test-workgroup")
        assert result["ok"] is True
        assert len(result["specialists"]) == 1
        assert result["specialists"][0]["name"] == "dev-1"

    def test_list_specialists_wrong_workgroup(self, tmp_path):
        manager = self._make_manager(tmp_path)
        result = manager.list_specialists("nonexistent")
        assert result["ok"] is True
        assert result["specialists"] == []

    def test_get_task_result_not_found(self, tmp_path):
        manager = self._make_manager(tmp_path)
        result = manager.get_task_result("fake-id")
        assert result["ok"] is False

    def test_get_task_result_found(self, tmp_path):
        manager = self._make_manager(tmp_path)
        manager.tasks._results["dev-1-1"] = {"status": "done", "result": "ok"}
        result = manager.get_task_result("dev-1-1")
        assert result["ok"] is True
        assert result["status"] == "done"

    def test_get_running_tasks_none(self, tmp_path):
        manager = self._make_manager(tmp_path)
        assert manager._get_running_tasks("test-workgroup") == []

    def test_get_running_tasks_with_running(self, tmp_path):
        manager = self._make_manager(tmp_path)
        manager.tasks._results["dev-1-1"] = {
            "status": "running", "target": "dev-1", "started_at": time.time(),
        }
        # No pool, so active=False
        tasks = manager._get_running_tasks("test-workgroup")
        assert len(tasks) == 1
        assert tasks[0]["task_id"] == "dev-1-1"
        assert tasks[0]["active"] is False

    def test_get_running_tasks_ignores_done(self, tmp_path):
        manager = self._make_manager(tmp_path)
        manager.tasks._results["dev-1-1"] = {"status": "done", "target": "dev-1"}
        assert manager._get_running_tasks("test-workgroup") == []

    def test_save_and_load_specialists(self, tmp_path):
        manager = self._make_manager(tmp_path)
        (tmp_path / "local").mkdir(exist_ok=True)
        from boxagent.config import SpecialistConfig
        specialist = SpecialistConfig(name="dynamic-1", model="haiku", workspace="/tmp/dyn")
        manager._save_specialist("test-workgroup", specialist)
        loaded = manager._load_saved_specialists("test-workgroup")
        assert "dynamic-1" in loaded
        assert loaded["dynamic-1"].model == "haiku"

    def test_remove_saved_specialist(self, tmp_path):
        manager = self._make_manager(tmp_path)
        (tmp_path / "local").mkdir(exist_ok=True)
        from boxagent.config import SpecialistConfig
        specialist = SpecialistConfig(name="dynamic-1", model="haiku")
        manager._save_specialist("test-workgroup", specialist)
        manager._remove_saved_specialist("test-workgroup", "dynamic-1")
        loaded = manager._load_saved_specialists("test-workgroup")
        assert "dynamic-1" not in loaded

    def test_reset_specialist_not_found(self, tmp_path):
        manager = self._make_manager(tmp_path)
        result = manager.reset_specialist("nonexistent")
        assert result["ok"] is False

