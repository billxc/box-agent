"""Integration tests for workgroup async operations.

Tests WorkgroupManager async methods (send_to_specialist, create_specialist,
delete_specialist) and HeartbeatManager tick cycle with mocked backends.
"""

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from boxagent.config import WorkgroupConfig, SpecialistConfig
from boxagent.workgroup.manager import WorkgroupManager
from boxagent.workgroup.heartbeat import HeartbeatManager


# ---------------------------------------------------------------------------
# Module-wide setup: stub the workspace setup helpers so create_specialist
# tests don't actually spawn backends or write .git skeletons. Per-test
# overrides set the return value via the ``mock_backend`` fixture.
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_backend():
    """A MagicMock that ``create_backend`` will return for the test."""
    cli = MagicMock()
    cli.start = MagicMock()
    return cli


@pytest.fixture(autouse=True)
def _patch_workspace_setup(mock_backend):
    """Auto-patch backend factory + git skeleton helper inside the workgroup
    manager module for every test. Tests that need to inspect the backend
    receive it via the ``mock_backend`` fixture."""
    with patch("boxagent.workgroup.manager.create_backend", return_value=mock_backend), \
         patch("boxagent.workgroup.manager.ensure_git_repo"):
        yield


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mock_router(response_text="done"):
    """Create a mock Router with dispatch_sync."""
    router = AsyncMock()
    router.dispatch_sync = AsyncMock(
        return_value=f"<specialist_response>{response_text}</specialist_response>"
    )
    router.handle_message = AsyncMock()
    router.workgroup_agents = []
    return router


def _mock_pool():
    """Create a mock SessionPool."""
    pool = MagicMock()
    pool.clear_session = MagicMock()
    pool.stop = AsyncMock()
    pool._active = {}
    pool._chat_states = {}
    return pool


def _make_manager(tmp_path, specialists=None):
    """Create a WorkgroupManager with a basic config."""
    workgroup_config = WorkgroupConfig(
        name="test-workgroup",
        workspace=str(tmp_path / "workspace"),
    )
    if specialists:
        for name in specialists:
            workgroup_config.specialists[name] = SpecialistConfig(
                name=name, model="sonnet",
                workspace=str(tmp_path / "specialists" / name),
            )

    manager = WorkgroupManager(
        config={"test-workgroup": workgroup_config},
        local_dir=tmp_path / "local",
        start_time=time.time(),
    )
    return manager, workgroup_config


# ---------------------------------------------------------------------------
# send_to_specialist
# ---------------------------------------------------------------------------


class TestSendToSpecialist:
    async def test_returns_task_id(self, tmp_path):
        manager, workgroup_config = _make_manager(tmp_path, ["dev-1"])
        manager.routers["dev-1"] = _mock_router("task done")
        manager.pools["dev-1"] = _mock_pool()

        result = await manager.send_to_specialist("dev-1", "do something", from_bot="admin")
        assert result["ok"] is True
        assert "task_id" in result
        assert result["specialist"] == "dev-1"

    async def test_task_marked_running(self, tmp_path):
        manager, workgroup_config = _make_manager(tmp_path, ["dev-1"])
        manager.routers["dev-1"] = _mock_router("done")
        manager.pools["dev-1"] = _mock_pool()

        result = await manager.send_to_specialist("dev-1", "work")
        task_id = result["task_id"]
        # Immediately after dispatch, status should be running
        info = manager.get_task_result(task_id)
        assert info["ok"] is True
        assert info["status"] == "running"

    async def test_task_completes(self, tmp_path):
        manager, workgroup_config = _make_manager(tmp_path, ["dev-1"])
        manager.routers["dev-1"] = _mock_router("all done")
        manager.pools["dev-1"] = _mock_pool()

        result = await manager.send_to_specialist("dev-1", "work")
        task_id = result["task_id"]
        # Wait for background task to complete
        await manager.tasks._tasks[task_id]

        info = manager.get_task_result(task_id)
        assert info["status"] == "done"
        assert info["result"] == "all done"

    async def test_extracts_specialist_response(self, tmp_path):
        manager, workgroup_config = _make_manager(tmp_path, ["dev-1"])
        router = _mock_router()
        router.dispatch_sync = AsyncMock(
            return_value="Thinking...\n<specialist_response>Final result</specialist_response>"
        )
        manager.routers["dev-1"] = router
        manager.pools["dev-1"] = _mock_pool()

        result = await manager.send_to_specialist("dev-1", "work")
        await manager.tasks._tasks[result["task_id"]]

        info = manager.get_task_result(result["task_id"])
        assert info["result"] == "Final result"

    async def test_error_handling(self, tmp_path):
        manager, workgroup_config = _make_manager(tmp_path, ["dev-1"])
        router = _mock_router()
        router.dispatch_sync = AsyncMock(side_effect=RuntimeError("boom"))
        manager.routers["dev-1"] = router
        manager.pools["dev-1"] = _mock_pool()

        result = await manager.send_to_specialist("dev-1", "work")
        await manager.tasks._tasks[result["task_id"]]

        info = manager.get_task_result(result["task_id"])
        assert info["status"] == "error"
        assert "boom" in info["error"]

    async def test_unknown_specialist(self, tmp_path):
        manager, _ = _make_manager(tmp_path, ["dev-1"])
        result = await manager.send_to_specialist("nonexistent", "work")
        assert result["ok"] is False
        assert "not found" in result["error"]

    async def test_callback_to_admin(self, tmp_path):
        manager, workgroup_config = _make_manager(tmp_path, ["dev-1"])
        admin_router = _mock_router()
        manager.routers["test-workgroup"] = admin_router
        manager.routers["dev-1"] = _mock_router("result text")
        manager.pools["dev-1"] = _mock_pool()

        result = await manager.send_to_specialist(
            "dev-1", "work", from_bot="admin", reply_chat_id="admin-channel",
        )
        await manager.tasks._tasks[result["task_id"]]

        # Admin router should receive the task result callback
        admin_router.handle_message.assert_called_once()
        msg = admin_router.handle_message.call_args[0][0]
        assert "TaskResult from dev-1" in msg.text
        assert "result text" in msg.text

    async def test_wraps_prompt_with_xml_instruction(self, tmp_path):
        manager, workgroup_config = _make_manager(tmp_path, ["dev-1"])
        router = _mock_router("ok")
        manager.routers["dev-1"] = router
        manager.pools["dev-1"] = _mock_pool()

        await manager.send_to_specialist("dev-1", "implement auth")
        # Wait a tick for task to start
        await asyncio.sleep(0.01)

        call_args = router.dispatch_sync.call_args
        prompt = call_args[0][0]
        assert "implement auth" in prompt
        assert "<specialist_response>" in prompt

    async def test_increments_task_counter(self, tmp_path):
        manager, workgroup_config = _make_manager(tmp_path, ["dev-1"])
        manager.routers["dev-1"] = _mock_router("ok")
        manager.pools["dev-1"] = _mock_pool()

        r1 = await manager.send_to_specialist("dev-1", "task 1")
        r2 = await manager.send_to_specialist("dev-1", "task 2")
        assert r1["task_id"] == "dev-1-1"
        assert r2["task_id"] == "dev-1-2"


# ---------------------------------------------------------------------------
# create_specialist
# ---------------------------------------------------------------------------


class TestCreateSpecialist:
    async def test_creates_specialist(self, tmp_path):
        manager, workgroup_config = _make_manager(tmp_path)


        result = await manager.create_specialist("test-workgroup", "new-dev")
        assert result["ok"] is True
        assert "new-dev" in manager.routers
        assert "new-dev" in workgroup_config.specialists

    async def test_rejects_duplicate(self, tmp_path):
        manager, workgroup_config = _make_manager(tmp_path, ["dev-1"])
        manager.routers["dev-1"] = _mock_router()

        result = await manager.create_specialist("test-workgroup", "dev-1")
        assert result["ok"] is False
        assert "already exists" in result["error"]

    async def test_rejects_unknown_workgroup(self, tmp_path):
        manager, _ = _make_manager(tmp_path)
        result = await manager.create_specialist("nonexistent", "dev-1")
        assert result["ok"] is False
        assert "not found" in result["error"]

    async def test_rejects_invalid_name_with_colon(self, tmp_path):
        """Names with ':' would collide with the workgroup:<name> chat_id namespace."""
        manager, _ = _make_manager(tmp_path)
        result = await manager.create_specialist("test-workgroup", "workgroup:foo")
        assert result["ok"] is False
        assert "invalid" in result["error"]

    async def test_rejects_empty_name(self, tmp_path):
        manager, _ = _make_manager(tmp_path)
        result = await manager.create_specialist("test-workgroup", "")
        assert result["ok"] is False

    async def test_rejects_uppercase_or_spaces(self, tmp_path):
        manager, _ = _make_manager(tmp_path)
        for bad in ("Dev-1", "my dev", ".hidden", "-leading", "way-too-long-specialist-name-exceeds-31-chars"):
            result = await manager.create_specialist("test-workgroup", bad)
            assert result["ok"] is False, f"name {bad!r} should be rejected"

    async def test_default_workspace(self, tmp_path):
        manager, workgroup_config = _make_manager(tmp_path)


        await manager.create_specialist("test-workgroup", "new-dev")
        specialist = workgroup_config.specialists["new-dev"]
        expected = str(Path(workgroup_config.workgroup_dir) / "specialists" / "new-dev")
        assert specialist.workspace == expected

    async def test_persists_specialist(self, tmp_path):
        manager, workgroup_config = _make_manager(tmp_path)
        (tmp_path / "local").mkdir(exist_ok=True)


        await manager.create_specialist("test-workgroup", "new-dev")

        loaded = manager._load_saved_specialists("test-workgroup")
        assert "new-dev" in loaded


# ---------------------------------------------------------------------------
# delete_specialist
# ---------------------------------------------------------------------------


class TestDeleteSpecialist:
    async def test_deletes_dynamic_specialist(self, tmp_path):
        manager, workgroup_config = _make_manager(tmp_path, ["dev-1"])
        manager.routers["dev-1"] = _mock_router()
        manager.pools["dev-1"] = _mock_pool()
        manager.procs["dev-1"] = AsyncMock()

        result = await manager.delete_specialist("dev-1")
        assert result["ok"] is True
        assert "dev-1" not in manager.routers
        assert "dev-1" not in manager.pools
        assert "dev-1" not in manager.procs
        assert "dev-1" not in workgroup_config.specialists

    async def test_rejects_unknown(self, tmp_path):
        manager, _ = _make_manager(tmp_path)
        result = await manager.delete_specialist("nonexistent")
        assert result["ok"] is False
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# reset_specialist
# ---------------------------------------------------------------------------


class TestResetSpecialist:
    def test_resets_session(self, tmp_path):
        manager, workgroup_config = _make_manager(tmp_path, ["dev-1"])
        pool = _mock_pool()
        manager.pools["dev-1"] = pool

        result = manager.reset_specialist("dev-1")
        assert result["ok"] is True
        pool.clear_session.assert_called_once()

    def test_unknown_specialist(self, tmp_path):
        manager, _ = _make_manager(tmp_path)
        result = manager.reset_specialist("nonexistent")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# HeartbeatManager — tick cycle
# ---------------------------------------------------------------------------


class TestHeartbeatTick:
    async def test_tick_with_silent_reply(self, tmp_path):
        (tmp_path / "HEARTBEAT.md").write_text("- Check tasks")

        hb = HeartbeatManager(
            workgroup_name="workgroup", admin_pool=MagicMock(), admin_router=AsyncMock(),
            workspace=str(tmp_path), interval_seconds=60, yolo=True,
        )

        with patch.object(hb, "_fork_and_decide", new_callable=AsyncMock) as mock_fork:
            mock_fork.return_value = ("NO_REPLY", {
                "source_session_id": "", "fork_session_id": "abc",
                "raw_response": "<heartbeat_action>NO_REPLY</heartbeat_action>",
                "prompt": "test",
            })
            await hb._tick()

        # Admin router should NOT be called for silent reply
        hb.admin_router.dispatch_sync.assert_not_called()
        # Log should be written
        assert (tmp_path / "heartbeat.log").exists()

    async def test_tick_with_action(self, tmp_path):
        (tmp_path / "HEARTBEAT.md").write_text("- Check tasks")

        admin_router = AsyncMock()
        admin_router.dispatch_sync = AsyncMock(return_value="executed")

        hb = HeartbeatManager(
            workgroup_name="workgroup", admin_pool=MagicMock(), admin_router=admin_router,
            workspace=str(tmp_path), interval_seconds=60, yolo=True,
        )

        with patch.object(hb, "_fork_and_decide", new_callable=AsyncMock) as mock_fork:
            mock_fork.return_value = ("Check dev-mac status", {
                "source_session_id": "src", "fork_session_id": "fork",
                "raw_response": "<heartbeat_action>Check dev-mac status</heartbeat_action>",
                "prompt": "test",
            })
            await hb._tick()

        # Admin router SHOULD be called with the decision
        admin_router.dispatch_sync.assert_called_once()
        call_args = admin_router.dispatch_sync.call_args
        assert "Check dev-mac status" in call_args[0][0]

    async def test_tick_skipped_when_busy(self, tmp_path):
        (tmp_path / "HEARTBEAT.md").write_text("- Check tasks")

        hb = HeartbeatManager(
            workgroup_name="workgroup", admin_pool=MagicMock(), admin_router=AsyncMock(),
            workspace=str(tmp_path), interval_seconds=60, yolo=True,
        )
        hb._is_ticking = True

        with patch.object(hb, "_fork_and_decide", new_callable=AsyncMock) as mock_fork:
            await hb._tick()
            mock_fork.assert_not_called()

    async def test_tick_no_heartbeat_md(self, tmp_path):
        hb = HeartbeatManager(
            workgroup_name="workgroup", admin_pool=MagicMock(), admin_router=AsyncMock(),
            workspace=str(tmp_path), interval_seconds=60, yolo=True,
        )

        with patch.object(hb, "_fork_and_decide", new_callable=AsyncMock) as mock_fork:
            await hb._tick()
            mock_fork.assert_not_called()

    async def test_display_heartbeat_via_web_channel(self, tmp_path):
        """Heartbeat display publishes to WebChannel under
        chat_id 'heartbeat:<workgroup_name>'."""
        (tmp_path / "HEARTBEAT.md").write_text("- Check tasks")

        wc = AsyncMock()
        wc.send_text = AsyncMock()

        hb = HeartbeatManager(
            workgroup_name="my-workgroup", admin_pool=MagicMock(), admin_router=AsyncMock(),
            workspace=str(tmp_path), interval_seconds=60, yolo=True,
            web_channel=wc,
            display_heartbeat=True,
        )

        with patch.object(hb, "_fork_and_decide", new_callable=AsyncMock) as mock_fork:
            mock_fork.return_value = ("NO_REPLY", {
                "source_session_id": "", "fork_session_id": "",
                "raw_response": "", "prompt": "",
            })
            await hb._tick()

        wc.send_text.assert_awaited()
        first = wc.send_text.await_args_list[0]
        assert first.args[0] == "heartbeat:my-workgroup"

    async def test_find_fork_session_via_main_chat_provider(self, tmp_path):
        """Fork source = pool ctx for the chat_id returned by main_chat_id_provider."""
        from boxagent.sessions.pool import SessionPool
        from boxagent.sessions.base_pool import ChatState

        pool = MagicMock(spec=SessionPool)
        ctx = ChatState(session_id="session-main")
        pool._get_state = MagicMock(return_value=ctx)
        pool._chat_states = {}

        hb = HeartbeatManager(
            workgroup_name="workgroup", admin_pool=pool, admin_router=AsyncMock(),
            workspace=str(tmp_path), interval_seconds=60,
            main_chat_id_provider=lambda: "main-workgroup-1",
        )

        assert hb._find_fork_session_id() == "session-main"
        pool._get_state.assert_called_once_with("main-workgroup-1")

    async def test_find_fork_session_no_provider_does_not_scan_pool(self, tmp_path):
        """No provider → return None without scanning pool (no silent fallback)."""
        from boxagent.sessions.pool import SessionPool
        from boxagent.sessions.base_pool import ChatState

        pool = MagicMock(spec=SessionPool)
        pool._get_state = MagicMock(return_value=ChatState(session_id="should-not-be-used"))
        pool._chat_states = {"some-chat": ChatState(session_id="should-not-be-used")}

        hb = HeartbeatManager(
            workgroup_name="workgroup", admin_pool=pool, admin_router=AsyncMock(),
            workspace=str(tmp_path), interval_seconds=60,
        )

        assert hb._find_fork_session_id() is None
        pool._get_state.assert_not_called()

    async def test_find_fork_session_main_chat_has_no_session(self, tmp_path):
        """Provider returns chat_id but ctx has no session yet → None, no pool scan."""
        from boxagent.sessions.pool import SessionPool
        from boxagent.sessions.base_pool import ChatState

        pool = MagicMock(spec=SessionPool)
        pool._get_state = MagicMock(return_value=ChatState(session_id=""))
        pool._chat_states = {"other": ChatState(session_id="leak")}

        hb = HeartbeatManager(
            workgroup_name="workgroup", admin_pool=pool, admin_router=AsyncMock(),
            workspace=str(tmp_path), interval_seconds=60,
            main_chat_id_provider=lambda: "main-workgroup-1",
        )

        assert hb._find_fork_session_id() is None
        pool._get_state.assert_called_once_with("main-workgroup-1")

    async def test_find_fork_session_no_pool(self, tmp_path):
        hb = HeartbeatManager(
            workgroup_name="workgroup", admin_pool=None, admin_router=AsyncMock(),
            workspace=str(tmp_path), interval_seconds=60,
        )
        assert hb._find_fork_session_id() is None


# ---------------------------------------------------------------------------
# Template system
# ---------------------------------------------------------------------------


def _seed_template(workgroup_dir: Path, name: str, claude_md_body: str = "TEMPLATE PROMPT") -> Path:
    tdir = workgroup_dir / "templates" / name
    tdir.mkdir(parents=True)
    (tdir / "description.md").write_text(f"{name} desc")
    (tdir / "CLAUDE.md").write_text(claude_md_body)
    return tdir


class TestTemplateIntegration:
    async def test_create_with_template_writes_snapshot_and_appends_prompt(self, tmp_path, monkeypatch):
        # Anchor boxagent_dir to tmp_path so relative path resolution stays inside the test sandbox.
        monkeypatch.setenv("BOX_AGENT_DIR", str(tmp_path))
        manager, workgroup_config = _make_manager(tmp_path)
        _seed_template(Path(workgroup_config.workgroup_dir), "planner", "## Planner role\nDecompose tasks.")

        result = await manager.create_specialist(
            "test-workgroup", "p1", template="planner",
        )
        assert result["ok"] is True

        specialist_config = workgroup_config.specialists["p1"]
        assert specialist_config.template == "planner"

        # Snapshot file written
        snapshot = Path(specialist_config.workspace) / ".boxagent-meta" / "template-snapshot.md"
        assert snapshot.is_file()
        assert "Decompose tasks" in snapshot.read_text()

        # CLAUDE.md includes both system layer (specialist name marker) and template body
        claude_md = (Path(specialist_config.workspace) / ".claude" / "CLAUDE.md").read_text()
        assert "Decompose tasks" in claude_md

    async def test_create_with_unknown_template_fails(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOX_AGENT_DIR", str(tmp_path))
        manager, workgroup_config = _make_manager(tmp_path)
        result = await manager.create_specialist(
            "test-workgroup", "p1", template="does-not-exist",
        )
        assert result["ok"] is False
        assert "not found" in result["error"]
        assert "p1" not in manager.routers

    async def test_template_field_persisted_and_restored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOX_AGENT_DIR", str(tmp_path))
        (tmp_path / "local").mkdir(exist_ok=True)
        manager, workgroup_config = _make_manager(tmp_path)
        _seed_template(Path(workgroup_config.workgroup_dir), "planner")

        await manager.create_specialist(
            "test-workgroup", "p1", template="planner",
            extra_skill_dirs=["/tmp/some-skills"],
        )
        loaded = manager._load_saved_specialists("test-workgroup")
        assert loaded["p1"].template == "planner"
        # Resolution preserved as-is for absolute paths
        assert "/tmp/some-skills" in loaded["p1"].extra_skill_dirs

    async def test_list_templates_returns_sorted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOX_AGENT_DIR", str(tmp_path))
        manager, workgroup_config = _make_manager(tmp_path)
        _seed_template(Path(workgroup_config.workgroup_dir), "planner")
        _seed_template(Path(workgroup_config.workgroup_dir), "auditor")

        result = manager.list_templates("test-workgroup")
        assert result["ok"] is True
        names = [t["name"] for t in result["templates"]]
        assert names == ["auditor", "planner"]

    async def test_delete_specialist_removes_workspace(self, tmp_path):
        manager, workgroup_config = _make_manager(tmp_path, ["dev-1"])
        # Create a fake workspace so delete has something to wipe.
        ws_path = Path(workgroup_config.specialists["dev-1"].workspace)
        ws_path.mkdir(parents=True, exist_ok=True)
        (ws_path / "marker").write_text("x")
        manager.routers["dev-1"] = _mock_router()
        manager.pools["dev-1"] = _mock_pool()
        manager.procs["dev-1"] = AsyncMock()

        result = await manager.delete_specialist("dev-1")
        assert result["ok"] is True
        assert not ws_path.exists()

    async def test_delete_specialist_force_cleans_even_if_backend_stop_raises(self, tmp_path):
        """Even when backend.stop() raises (e.g. SIGKILL syscall fails),
        delete must still proceed — workspace gone, dicts cleared,
        persistence updated. The user's whole point in calling delete is
        to make the specialist disappear.

        BaseCLIProcess.stop() already escalates SIGTERM → SIGKILL with
        a 3s timeout; if even that fails, log + continue rather than
        leaving zombie state half-deleted."""
        manager, workgroup_config = _make_manager(tmp_path, ["dev-1"])
        ws_path = Path(workgroup_config.specialists["dev-1"].workspace)
        ws_path.mkdir(parents=True, exist_ok=True)
        (ws_path / "marker").write_text("x")

        wedged_backend = AsyncMock()
        wedged_backend.stop = AsyncMock(side_effect=RuntimeError("kill syscall failed"))
        manager.routers["dev-1"] = _mock_router()
        manager.pools["dev-1"] = _mock_pool()
        manager.procs["dev-1"] = wedged_backend

        result = await manager.delete_specialist("dev-1")
        assert result["ok"] is True
        # Specialist gone from in-memory state and disk regardless.
        assert "dev-1" not in manager.routers
        assert "dev-1" not in manager.pools
        assert "dev-1" not in manager.procs
        assert not ws_path.exists()
