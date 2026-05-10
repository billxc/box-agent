"""End-to-end workgroup test with web substrate, real Router.

Mocks ONLY the BaseCLIProcess (the subprocess boundary). Everything above it —
WorkgroupManager, real Router._dispatch, ChannelCallback, WebChannel,
WebWorkgroupAdapter, SessionPool — runs as in production. This is the level
existing test_workgroup_integration.py never reaches (it mocks Router itself).
"""

import asyncio
import contextlib
from pathlib import Path
from unittest.mock import patch

import pytest

from boxagent.transports.web import WebChannel
from boxagent.config import SpecialistConfig, WorkgroupConfig
from boxagent.workgroup.manager import WorkgroupManager


# ---------------------------------------------------------------------------
# Minimal fake CLI process — drives the real ChannelCallback API
# ---------------------------------------------------------------------------


class FakeCLIProcess:
    """Mimics BaseCLIProcess surface used by Router._dispatch_one + cleanup.

    On send(), drives the callback as a real backend would: a stream_start
    via on_stream chunks, then close. Records every prompt + the env passed
    in so tests can assert what the admin/specialist actually saw.
    """

    def __init__(self, *, response: str = "ok", workspace: str = "",
                 model: str = "", bot_name: str = ""):
        self.workspace = workspace
        self.model = model
        self.bot_name = bot_name
        self.session_id: str | None = None
        self.state = "idle"
        self.last_turn_failed = False
        self.last_turn_error = ""
        self.supports_session_persistence = True
        self.received_prompts: list[str] = []
        self.received_envs: list[object] = []
        self._response = response
        self._started = False
        self._stopped = False

    def set_response(self, text: str) -> None:
        self._response = text

    def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._stopped = True
        self.state = "dead"

    async def cancel(self) -> None:
        self.state = "idle"

    async def reset_session(self) -> None:
        await self.cancel()
        self.session_id = None

    async def wait_idle(self) -> None:
        return

    async def drain_output(self) -> None:
        return

    async def send(self, message, callback, model="", chat_id="",
                   append_system_prompt="", env=None):
        self.received_prompts.append(message)
        self.received_envs.append(env)
        self.state = "busy"
        try:
            # Drive the real callback API exactly as a streaming backend would.
            await callback.on_stream(self._response)
        finally:
            # ChannelCallback's close() flushes any open stream handle.
            self.state = "idle"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _make_manager(tmp_path: Path):
    """Build a WorkgroupManager with one workgroup + one specialist, web substrate.

    Yields ``(manager, fakes)``. Patches ``create_backend`` and
    ``ensure_git_repo`` inside the workgroup manager module for the lifetime
    of the ``with`` block so ``WorkgroupManager.start_workgroup`` /
    ``create_specialist`` use ``FakeCLIProcess`` instead of spawning real CLIs.
    """

    specialist_config = SpecialistConfig(
        name="sp1",
        model="",
        workspace=str(tmp_path / "wg" / "specialists" / "sp1"),
        ai_backend="claude-cli",
        display_name="sp1",
    )
    workgroup_config = WorkgroupConfig(
        name="wg",
        workspace=str(tmp_path / "wg-root"),
        ai_backend="claude-cli",
        specialists={"sp1": specialist_config},
    )
    Path(workgroup_config.admin_workspace).mkdir(parents=True, exist_ok=True)
    Path(specialist_config.workspace).mkdir(parents=True, exist_ok=True)

    fakes: dict[str, list[FakeCLIProcess]] = {}

    def _factory(bot_cfg, session_id=None):
        fp = FakeCLIProcess(
            response=f"<specialist_response>done by {bot_cfg.name}</specialist_response>",
            workspace=bot_cfg.workspace,
            model=bot_cfg.model,
            bot_name=bot_cfg.name,
        )
        fakes.setdefault(bot_cfg.name, []).append(fp)
        return fp

    with patch("boxagent.workgroup.manager.create_backend", side_effect=_factory), \
         patch("boxagent.workgroup.manager.ensure_git_repo"):
        manager = WorkgroupManager(
            config={"wg": workgroup_config},
            config_dir=str(tmp_path / "config"),
            node_id="test-node",
            local_dir=tmp_path / "local",
            start_time=0.0,
            storage=None,
        )
        # Pre-create the WebChannel exactly as Gateway does.
        manager.web_channels["wg"] = WebChannel(bot_name="wg")

        yield manager, fakes


# ---------------------------------------------------------------------------
# Scenario 1 — the smoking gun
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_dispatch_to_specialist_e2e(tmp_path):
    """Admin → specialist over web substrate."""
    with _make_manager(tmp_path) as (manager, fakes):

        # Subscribe to the specialist's virtual chat_id BEFORE dispatch so we can
        # assert the streaming events the admin's web UI would see.
        web = manager.web_channels["wg"]
        sp_queue = web.subscribe("workgroup:sp1")
        admin_queue = web.subscribe("admin-chat")

        await manager.start_workgroup("wg", manager.config["wg"])

        assert "wg" in fakes, "admin backend not created"
        assert "sp1" in fakes, "specialist backend not created"
        assert all(fp._started for fp in fakes["wg"]), "admin backends not started"
        assert all(fp._started for fp in fakes["sp1"]), "specialist backends not started"

        # Sanity: the adapter is the Web one (not Null) and Routers got the WebChannel.
        from boxagent.workgroup.channel_adapter import WebWorkgroupAdapter
        assert isinstance(manager.adapters["wg"], WebWorkgroupAdapter)
        assert manager.routers["sp1"].channel is web

        # The actual call the admin's `send_to_agent` MCP tool makes.
        result = await manager.send_to_specialist(
            target="sp1",
            text="please check the build",
            from_bot="wg",
            reply_chat_id="admin-chat",
        )
        assert result == {"ok": True, "task_id": "sp1-1", "specialist": "sp1"}

        # Wait for the background _run task to complete.
        await asyncio.wait_for(manager.tasks._tasks["sp1-1"], timeout=5.0)

        # 1) Specialist backend received the wrapped prompt.
        sp_prompts = [p for fp in fakes["sp1"] for p in fp.received_prompts]
        assert len(sp_prompts) == 1, f"specialist not invoked exactly once: {sp_prompts}"
        assert "please check the build" in sp_prompts[0]
        assert "<specialist_response>" in sp_prompts[0]  # SYSTEM wrapper present

        # 2) Specialist's stream events landed on its `wg:sp1` chat for admin web UI.
        sp_events: list[dict] = []
        while not sp_queue.empty():
            sp_events.append(sp_queue.get_nowait())
        event_types = [e.get("type") for e in sp_events]
        # The admin's task post (post_task) → "message" with role=user
        assert "message" in event_types, f"missing post_task user message; got {event_types}"
        user_msgs = [e for e in sp_events if e.get("type") == "message" and e.get("role") == "user"]
        assert user_msgs and "please check the build" in user_msgs[0]["text"]
        # Specialist's actual streaming output.
        assert "stream_start" in event_types, f"specialist did not stream; got {event_types}"
        assert "stream_end" in event_types, f"specialist stream not closed; got {event_types}"

        # 3) Task result recorded.
        info = manager.tasks._results["sp1-1"]
        assert info["status"] == "done", info
        assert info["result"] == "done by sp1"

        # 4) Admin received the [TaskResult …] callback message.
        # send_to_specialist routes it via admin_router.handle_message → real
        # _dispatch → pool.acquire → backend.send. Aggregate prompts across all
        # admin pool members.
        await asyncio.sleep(0)
        admin_prompts = [p for fp in fakes["wg"] for p in fp.received_prompts]
        assert any("[TaskResult from sp1]" in p for p in admin_prompts), (
            f"admin never received task callback; admin_prompts={admin_prompts!r}"
        )

        # 5) Admin's chat got the short notification (notify_admin via WebChannel.send_text).
        notifications = []
        while not admin_queue.empty():
            notifications.append(admin_queue.get_nowait())
        assert any(
            n.get("type") == "message" and "[sp1]" in n.get("text", "")
            for n in notifications
        ), f"admin chat did not receive notify_admin; got {notifications}"


# ---------------------------------------------------------------------------
# Scenario 2 — env propagation regression test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_env_carries_workgroup_role(tmp_path):
    """Regression: env.is_workgroup_admin must be True for admin's turns,
    otherwise claude_process never injects the /mcp/admin endpoint and the
    admin AI literally has no `send_to_agent` tool — silent failure."""
    with _make_manager(tmp_path) as (manager, fakes):
        await manager.start_workgroup("wg", manager.config["wg"])

        # Drive a turn through the admin router with a normal user message.
        admin_router = manager.routers["wg"]
        await admin_router.dispatch_sync("hello", "admin-chat", from_bot="user")

        envs = [e for fp in fakes["wg"] for e in fp.received_envs]
        assert envs, "admin backend.send was never called"
        env = envs[0]
        assert env is not None, "router did not pass an AgentEnv"
        assert env.workgroup_role == "admin", f"role lost: {env.workgroup_role!r}"
        assert env.is_workgroup_admin is True


@pytest.mark.asyncio
async def test_specialist_env_carries_workgroup_role(tmp_path):
    """Symmetric to the admin regression: specialist Router must propagate
    workgroup_role='specialist' so env.is_specialist works. Was previously
    dead code because manager._create_specialist_agent never set the role."""
    with _make_manager(tmp_path) as (manager, fakes):
        await manager.start_workgroup("wg", manager.config["wg"])

        # Find any specialist router built by the fixture.
        spec_name = next(n for n in manager.routers if n != "wg")
        spec_router = manager.routers[spec_name]
        await spec_router.dispatch_sync("hello", f"workgroup:{spec_name}", from_bot="wg")

        envs = [e for fp in fakes[spec_name] for e in fp.received_envs]
        assert envs, "specialist backend.send was never called"
        env = envs[0]
        assert env.workgroup_role == "specialist", f"role lost: {env.workgroup_role!r}"
        assert env.is_specialist is True
        assert env.is_workgroup_admin is False


# ---------------------------------------------------------------------------
# Cleanup helper (manager has no async stop in some tests; ensure tasks die).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _cancel_dangling_tasks():
    yield
    for t in asyncio.all_tasks() - {asyncio.current_task()}:
        if t.done():
            continue
        t.cancel()
        with contextlib.suppress(BaseException):
            await t
