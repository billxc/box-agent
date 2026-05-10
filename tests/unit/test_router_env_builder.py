"""env_builder is testable without instantiating Router."""

from types import SimpleNamespace

from boxagent.agent_env import ChannelInfo
from boxagent.transports.base import IncomingMessage
from boxagent.router.env_builder import build_env


def _make_router(**overrides):
    base = dict(
        pool=None,
        backend=SimpleNamespace(model="default-model", yolo=False),
        workspace="/tmp/ws",
        bot_name="test-bot",
        display_name="Test",
        node_id="node-1",
        config_dir="/tmp/config",
        local_dir="/tmp/local",
        telegram_token="abc",
        has_peer_channel=False,
        workgroup_role="",
        workgroup_agents=[],
        ai_backend="claude-cli",
        passthrough=False,
        get_running_tasks=lambda: [],
        get_peers=lambda: [],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_msg(**overrides):
    base = dict(
        channel="telegram",
        chat_id="111",
        user_id="111",
        text="hi",
        trusted=True,
        via_workgroup=False,
        channel_info=None,
    )
    base.update(overrides)
    return IncomingMessage(**base)


def test_uses_backend_model_when_no_pool():
    env = build_env(_make_msg(), _make_router())
    assert env.model == "default-model"
    assert env.workspace == "/tmp/ws"
    assert env.bot_name == "test-bot"


def test_pool_overrides_model_and_workspace():
    pool = SimpleNamespace(
        get_model=lambda chat_id: "pool-model",
        get_workspace=lambda chat_id: "/tmp/pool-ws",
    )
    env = build_env(_make_msg(), _make_router(pool=pool))
    assert env.model == "pool-model"
    assert env.workspace == "/tmp/pool-ws"


def test_falls_back_to_router_workspace_when_pool_returns_none():
    pool = SimpleNamespace(
        get_model=lambda chat_id: None,
        get_workspace=lambda chat_id: None,
    )
    env = build_env(_make_msg(), _make_router(pool=pool))
    # Pool returns None → fall back to router.workspace
    assert env.workspace == "/tmp/ws"
    # model degrades to "" when pool says None (backend.model NOT used)
    assert env.model == ""


def test_msg_channel_info_wins_over_msg_channel():
    info = ChannelInfo(platform="web")
    env = build_env(_make_msg(channel="telegram", channel_info=info), _make_router())
    assert env.channel.platform == "web"


def test_running_tasks_and_peers_become_tuples():
    router = _make_router(
        get_running_tasks=lambda: [{"task_id": "t1"}],
        get_peers=lambda: [{"name": "p1"}],
    )
    env = build_env(_make_msg(), router)
    assert env.running_tasks == ({"task_id": "t1"},)
    assert env.peers == ({"name": "p1"},)
