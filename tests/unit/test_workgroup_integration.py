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
    pool._active = {}
    pool._chat_contexts = {}
    return pool


def _make_manager(tmp_path, specialists=None):
    """Create a WorkgroupManager with a basic config."""
    wg_cfg = WorkgroupConfig(
        name="test-wg",
        workspace=str(tmp_path / "workspace"),
    )
    if specialists:
        for name in specialists:
            wg_cfg.specialists[name] = SpecialistConfig(
                name=name, model="sonnet",
                workspace=str(tmp_path / "specialists" / name),
            )

    mgr = WorkgroupManager(
        config={"test-wg": wg_cfg},
        local_dir=tmp_path / "local",
        start_time=time.time(),
    )
    return mgr, wg_cfg


# ---------------------------------------------------------------------------
# send_to_specialist
# ---------------------------------------------------------------------------


class TestSendToSpecialist:
    async def test_returns_task_id(self, tmp_path):
        mgr, wg_cfg = _make_manager(tmp_path, ["dev-1"])
        mgr.routers["dev-1"] = _mock_router("task done")
        mgr.pools["dev-1"] = _mock_pool()

        result = await mgr.send_to_specialist("dev-1", "do something", from_bot="admin")
        assert result["ok"] is True
        assert "task_id" in result
        assert result["specialist"] == "dev-1"

    async def test_task_marked_running(self, tmp_path):
        mgr, wg_cfg = _make_manager(tmp_path, ["dev-1"])
        mgr.routers["dev-1"] = _mock_router("done")
        mgr.pools["dev-1"] = _mock_pool()

        result = await mgr.send_to_specialist("dev-1", "work")
        task_id = result["task_id"]
        # Immediately after dispatch, status should be running
        info = mgr.get_task_result(task_id)
        assert info["ok"] is True
        assert info["status"] == "running"

    async def test_task_completes(self, tmp_path):
        mgr, wg_cfg = _make_manager(tmp_path, ["dev-1"])
        mgr.routers["dev-1"] = _mock_router("all done")
        mgr.pools["dev-1"] = _mock_pool()

        result = await mgr.send_to_specialist("dev-1", "work")
        task_id = result["task_id"]
        # Wait for background task to complete
        await mgr._tasks[task_id]

        info = mgr.get_task_result(task_id)
        assert info["status"] == "done"
        assert info["result"] == "all done"

    async def test_extracts_specialist_response(self, tmp_path):
        mgr, wg_cfg = _make_manager(tmp_path, ["dev-1"])
        router = _mock_router()
        router.dispatch_sync = AsyncMock(
            return_value="Thinking...\n<specialist_response>Final result</specialist_response>"
        )
        mgr.routers["dev-1"] = router
        mgr.pools["dev-1"] = _mock_pool()

        result = await mgr.send_to_specialist("dev-1", "work")
        await mgr._tasks[result["task_id"]]

        info = mgr.get_task_result(result["task_id"])
        assert info["result"] == "Final result"

    async def test_error_handling(self, tmp_path):
        mgr, wg_cfg = _make_manager(tmp_path, ["dev-1"])
        router = _mock_router()
        router.dispatch_sync = AsyncMock(side_effect=RuntimeError("boom"))
        mgr.routers["dev-1"] = router
        mgr.pools["dev-1"] = _mock_pool()

        result = await mgr.send_to_specialist("dev-1", "work")
        await mgr._tasks[result["task_id"]]

        info = mgr.get_task_result(result["task_id"])
        assert info["status"] == "error"
        assert "boom" in info["error"]

    async def test_unknown_specialist(self, tmp_path):
        mgr, _ = _make_manager(tmp_path, ["dev-1"])
        result = await mgr.send_to_specialist("nonexistent", "work")
        assert result["ok"] is False
        assert "not found" in result["error"]

    async def test_callback_to_admin(self, tmp_path):
        mgr, wg_cfg = _make_manager(tmp_path, ["dev-1"])
        admin_router = _mock_router()
        mgr.routers["test-wg"] = admin_router
        mgr.routers["dev-1"] = _mock_router("result text")
        mgr.pools["dev-1"] = _mock_pool()

        result = await mgr.send_to_specialist(
            "dev-1", "work", from_bot="admin", reply_chat_id="admin-ch",
        )
        await mgr._tasks[result["task_id"]]

        # Admin router should receive the task result callback
        admin_router.handle_message.assert_called_once()
        msg = admin_router.handle_message.call_args[0][0]
        assert "TaskResult from dev-1" in msg.text
        assert "result text" in msg.text

    async def test_wraps_prompt_with_xml_instruction(self, tmp_path):
        mgr, wg_cfg = _make_manager(tmp_path, ["dev-1"])
        router = _mock_router("ok")
        mgr.routers["dev-1"] = router
        mgr.pools["dev-1"] = _mock_pool()

        await mgr.send_to_specialist("dev-1", "implement auth")
        # Wait a tick for task to start
        await asyncio.sleep(0.01)

        call_args = router.dispatch_sync.call_args
        prompt = call_args[0][0]
        assert "implement auth" in prompt
        assert "<specialist_response>" in prompt

    async def test_increments_task_counter(self, tmp_path):
        mgr, wg_cfg = _make_manager(tmp_path, ["dev-1"])
        mgr.routers["dev-1"] = _mock_router("ok")
        mgr.pools["dev-1"] = _mock_pool()

        r1 = await mgr.send_to_specialist("dev-1", "task 1")
        r2 = await mgr.send_to_specialist("dev-1", "task 2")
        assert r1["task_id"] == "dev-1-1"
        assert r2["task_id"] == "dev-1-2"


# ---------------------------------------------------------------------------
# create_specialist
# ---------------------------------------------------------------------------


class TestCreateSpecialist:
    async def test_creates_specialist(self, tmp_path):
        mgr, wg_cfg = _make_manager(tmp_path)

        mock_cli = MagicMock()
        mock_cli.start = MagicMock()
        mgr._create_backend = MagicMock(return_value=mock_cli)
        mgr._ensure_git_repo = MagicMock()

        result = await mgr.create_specialist("test-wg", "new-dev")
        assert result["ok"] is True
        assert "new-dev" in mgr.routers
        assert "new-dev" in wg_cfg.specialists

    async def test_rejects_duplicate(self, tmp_path):
        mgr, wg_cfg = _make_manager(tmp_path, ["dev-1"])
        mgr.routers["dev-1"] = _mock_router()

        result = await mgr.create_specialist("test-wg", "dev-1")
        assert result["ok"] is False
        assert "already exists" in result["error"]

    async def test_rejects_unknown_workgroup(self, tmp_path):
        mgr, _ = _make_manager(tmp_path)
        result = await mgr.create_specialist("nonexistent", "dev-1")
        assert result["ok"] is False
        assert "not found" in result["error"]

    async def test_default_workspace(self, tmp_path):
        mgr, wg_cfg = _make_manager(tmp_path)

        mock_cli = MagicMock()
        mock_cli.start = MagicMock()
        mgr._create_backend = MagicMock(return_value=mock_cli)
        mgr._ensure_git_repo = MagicMock()

        await mgr.create_specialist("test-wg", "new-dev")
        sp = wg_cfg.specialists["new-dev"]
        expected = str(Path(wg_cfg.workgroup_dir) / "specialists" / "new-dev")
        assert sp.workspace == expected

    async def test_persists_specialist(self, tmp_path):
        mgr, wg_cfg = _make_manager(tmp_path)
        (tmp_path / "local").mkdir(exist_ok=True)

        mock_cli = MagicMock()
        mock_cli.start = MagicMock()
        mgr._create_backend = MagicMock(return_value=mock_cli)
        mgr._ensure_git_repo = MagicMock()

        await mgr.create_specialist("test-wg", "new-dev")

        loaded = mgr._load_saved_specialists("test-wg")
        assert "new-dev" in loaded


# ---------------------------------------------------------------------------
# delete_specialist
# ---------------------------------------------------------------------------


class TestDeleteSpecialist:
    async def test_deletes_dynamic_specialist(self, tmp_path):
        mgr, wg_cfg = _make_manager(tmp_path, ["dev-1"])
        mgr.routers["dev-1"] = _mock_router()
        mgr.pools["dev-1"] = _mock_pool()
        mgr.procs["dev-1"] = AsyncMock()
        # Not a builtin
        mgr._builtin_specialists["test-wg"] = set()

        result = await mgr.delete_specialist("dev-1")
        assert result["ok"] is True
        assert "dev-1" not in mgr.routers
        assert "dev-1" not in mgr.pools
        assert "dev-1" not in mgr.procs
        assert "dev-1" not in wg_cfg.specialists

    async def test_rejects_builtin(self, tmp_path):
        mgr, wg_cfg = _make_manager(tmp_path, ["dev-1"])
        mgr.routers["dev-1"] = _mock_router()
        mgr._builtin_specialists["test-wg"] = {"dev-1"}

        result = await mgr.delete_specialist("dev-1")
        assert result["ok"] is False
        assert "built-in" in result["error"]

    async def test_rejects_unknown(self, tmp_path):
        mgr, _ = _make_manager(tmp_path)
        result = await mgr.delete_specialist("nonexistent")
        assert result["ok"] is False
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# reset_specialist
# ---------------------------------------------------------------------------


class TestResetSpecialist:
    def test_resets_session(self, tmp_path):
        mgr, wg_cfg = _make_manager(tmp_path, ["dev-1"])
        pool = _mock_pool()
        mgr.pools["dev-1"] = pool

        result = mgr.reset_specialist("dev-1")
        assert result["ok"] is True
        pool.clear_session.assert_called_once()

    def test_unknown_specialist(self, tmp_path):
        mgr, _ = _make_manager(tmp_path)
        result = mgr.reset_specialist("nonexistent")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# HeartbeatManager — tick cycle
# ---------------------------------------------------------------------------


class TestHeartbeatTick:
    async def test_tick_with_silent_reply(self, tmp_path):
        (tmp_path / "HEARTBEAT.md").write_text("- Check tasks")

        hb = HeartbeatManager(
            wg_name="wg", admin_pool=MagicMock(), admin_router=AsyncMock(),
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
            wg_name="wg", admin_pool=MagicMock(), admin_router=admin_router,
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
            wg_name="wg", admin_pool=MagicMock(), admin_router=AsyncMock(),
            workspace=str(tmp_path), interval_seconds=60, yolo=True,
        )
        hb._is_ticking = True

        with patch.object(hb, "_fork_and_decide", new_callable=AsyncMock) as mock_fork:
            await hb._tick()
            mock_fork.assert_not_called()

    async def test_tick_no_heartbeat_md(self, tmp_path):
        hb = HeartbeatManager(
            wg_name="wg", admin_pool=MagicMock(), admin_router=AsyncMock(),
            workspace=str(tmp_path), interval_seconds=60, yolo=True,
        )

        with patch.object(hb, "_fork_and_decide", new_callable=AsyncMock) as mock_fork:
            await hb._tick()
            mock_fork.assert_not_called()

    async def test_display_heartbeat_via_webhook(self, tmp_path):
        (tmp_path / "HEARTBEAT.md").write_text("- Check tasks")

        dc = AsyncMock()
        wh = AsyncMock()
        dc._ensure_webhook = AsyncMock(return_value=wh)

        hb = HeartbeatManager(
            wg_name="wg", admin_pool=MagicMock(), admin_router=AsyncMock(),
            workspace=str(tmp_path), interval_seconds=60, yolo=True,
            discord_channel=dc, discord_chat_id="12345",
            display_heartbeat=True,
        )

        with patch.object(hb, "_fork_and_decide", new_callable=AsyncMock) as mock_fork:
            mock_fork.return_value = ("NO_REPLY", {
                "source_session_id": "", "fork_session_id": "",
                "raw_response": "", "prompt": "",
            })
            await hb._tick()

        # Webhook should be used for display
        dc._ensure_webhook.assert_called()

    async def test_find_fork_session_from_pool(self, tmp_path):
        from boxagent.session_pool import SessionPool, ChatContext

        pool = MagicMock(spec=SessionPool)
        ctx = ChatContext(session_id="sess-123")
        pool._get_ctx = MagicMock(return_value=ctx)
        pool._chat_contexts = {"12345": ctx}

        hb = HeartbeatManager(
            wg_name="wg", admin_pool=pool, admin_router=AsyncMock(),
            workspace=str(tmp_path), interval_seconds=60,
            discord_chat_id="12345",
        )

        sid = hb._find_fork_session_id()
        assert sid == "sess-123"
        pool._get_ctx.assert_called_once_with("12345")

    async def test_find_fork_session_no_pool(self, tmp_path):
        hb = HeartbeatManager(
            wg_name="wg", admin_pool=None, admin_router=AsyncMock(),
            workspace=str(tmp_path), interval_seconds=60,
        )
        assert hb._find_fork_session_id() is None
