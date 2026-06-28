"""Unit tests for workgroup.heartbeat."""

import time
from unittest.mock import AsyncMock

import pytest

from boxagent.workgroup.heartbeat import (
    HeartbeatManager,
    _build_heartbeat_prompt,
    _extract_action,
    is_silent_reply,
)

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


class TestBuildHeartbeatPrompt:
    def test_basic_prompt(self):
        prompt = _build_heartbeat_prompt("war-room", "- Check tasks")
        assert "HEARTBEAT CHECK" in prompt
        assert "war-room" in prompt
        assert "Check tasks" in prompt
        assert "<heartbeat_action>" in prompt

    def test_includes_uptime(self):
        prompt = _build_heartbeat_prompt("workgroup", "checklist", uptime_seconds=7500)
        assert "2h 5m" in prompt

    def test_short_uptime(self):
        prompt = _build_heartbeat_prompt("workgroup", "checklist", uptime_seconds=125)
        assert "2m 5s" in prompt

    def test_includes_running_tasks(self):
        tasks = [
            {"task_id": "dev-1", "target": "dev", "started_at": time.time() - 60, "active": True},
        ]
        prompt = _build_heartbeat_prompt("workgroup", "checklist", running_tasks=tasks)
        assert "dev-1" in prompt
        assert "[active]" in prompt

    def test_no_running_tasks(self):
        prompt = _build_heartbeat_prompt("workgroup", "checklist", running_tasks=[])
        assert "No specialist tasks currently running" in prompt

    def test_read_only_instruction(self):
        prompt = _build_heartbeat_prompt("workgroup", "checklist")
        assert "read-only" in prompt
        assert "NO execution permissions" in prompt


class TestHeartbeatReadMd:
    def test_reads_file(self, tmp_path):
        (tmp_path / "HEARTBEAT.md").write_text("- Check tasks\n- Review work")
        hb = HeartbeatManager(
            workgroup_name="workgroup", admin_pool=None, admin_router=None,
            workspace=str(tmp_path), interval_seconds=60,
        )
        content = hb._read_heartbeat_md()
        assert "Check tasks" in content

    def test_missing_file(self, tmp_path):
        hb = HeartbeatManager(
            workgroup_name="workgroup", admin_pool=None, admin_router=None,
            workspace=str(tmp_path), interval_seconds=60,
        )
        assert hb._read_heartbeat_md() is None

    def test_empty_file(self, tmp_path):
        (tmp_path / "HEARTBEAT.md").write_text("")
        hb = HeartbeatManager(
            workgroup_name="workgroup", admin_pool=None, admin_router=None,
            workspace=str(tmp_path), interval_seconds=60,
        )
        assert hb._read_heartbeat_md() is None

    def test_empty_workspace(self):
        hb = HeartbeatManager(
            workgroup_name="workgroup", admin_pool=None, admin_router=None,
            workspace="", interval_seconds=60,
        )
        assert hb._read_heartbeat_md() is None


# ---------------------------------------------------------------------------
# HeartbeatManager — log facade emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_emits_heartbeat_tick(tmp_path):
    from boxagent.events.bus import EventBus
    from boxagent.events.storage import EventStore
    from boxagent.log import log

    store = EventStore(tmp_path / "e.db")
    bus = EventBus(store, machine_id="m1")
    log.bind(bus)
    try:
        (tmp_path / "HEARTBEAT.md").write_text("- check things")
        admin_router = AsyncMock()
        admin_router.dispatch_sync = AsyncMock()
        hb = HeartbeatManager(
            workgroup_name="wg1", admin_pool=None, admin_router=admin_router,
            workspace=str(tmp_path), interval_seconds=60,
        )
        # Avoid the fork path — return a non-silent decision so dispatch fires.
        hb._fork_and_decide = AsyncMock(return_value=("do something", {}))
        hb._write_heartbeat_log = lambda *a, **kw: None
        await hb._tick()
        cats = [e.category for e in store.query()]
        assert "workgroup.heartbeat.tick" in cats
        assert "workgroup.heartbeat.drive" in cats
        tick = next(e for e in store.query() if e.category == "workgroup.heartbeat.tick")
        assert tick.meta.get("workgroup") == "wg1"
    finally:
        log.unbind()
        bus.close()


@pytest.mark.asyncio
async def test_tick_silent_decision_emits_pause(tmp_path):
    from boxagent.events.bus import EventBus
    from boxagent.events.storage import EventStore
    from boxagent.log import log

    store = EventStore(tmp_path / "e.db")
    bus = EventBus(store, machine_id="m1")
    log.bind(bus)
    try:
        (tmp_path / "HEARTBEAT.md").write_text("- check things")
        hb = HeartbeatManager(
            workgroup_name="wg1", admin_pool=None, admin_router=AsyncMock(),
            workspace=str(tmp_path), interval_seconds=60,
        )
        hb._fork_and_decide = AsyncMock(return_value=("NO_REPLY", {}))  # silent reply
        hb._write_heartbeat_log = lambda *a, **kw: None
        await hb._tick()
        cats = [e.category for e in store.query()]
        assert "workgroup.heartbeat.tick" in cats
        assert "workgroup.heartbeat.pause" in cats
        assert "workgroup.heartbeat.drive" not in cats
    finally:
        log.unbind()
        bus.close()


class TestHeartbeatLog:
    def test_writes_log(self, tmp_path):
        hb = HeartbeatManager(
            workgroup_name="workgroup", admin_pool=None, admin_router=None,
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
            workgroup_name="workgroup", admin_pool=None, admin_router=None,
            workspace=str(tmp_path), interval_seconds=60,
        )
        meta = {"source_session_id": "", "fork_session_id": "", "raw_response": "", "prompt": ""}
        hb._write_heartbeat_log("NO_REPLY", meta)
        hb._write_heartbeat_log("Do something", meta)
        log = (tmp_path / "heartbeat.log").read_text()
        assert log.count("===") == 4  # 2 entries × 2 separators each


class TestHeartbeatSkipsUnsupportedFork:
    async def test_codex_admin_skips_fork(self, tmp_path):
        """A codex-cli admin's heartbeat tick should NO_REPLY rather than
        spawn a Claude process (the previous hard-coded behaviour)."""
        from unittest.mock import MagicMock
        pool = MagicMock()
        pool._get_state = MagicMock(return_value=MagicMock(session_id="some-sid"))

        hb = HeartbeatManager(
            workgroup_name="wg", admin_pool=pool, admin_router=None,
            workspace=str(tmp_path), interval_seconds=60,
            ai_backend="codex-cli", model="", yolo=False,
            main_chat_id_provider=lambda: "main-chat",
        )
        action, meta = await hb._fork_and_decide("ping")
        assert action == "NO_REPLY"
        reason = meta.get("reason", "")
        assert "codex-cli" in reason or "fork" in reason.lower()

