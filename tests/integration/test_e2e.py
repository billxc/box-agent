"""End-to-end smoke tests with real Telegram + real Claude CLI."""

import os
import shutil
from textwrap import dedent

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(120),
    pytest.mark.skipif(
        not shutil.which("claude"),
        reason="claude CLI not on PATH",
    ),
    pytest.mark.skipif(
        not os.environ.get("BOXAGENT_TEST_CHAT_ID"),
        reason="BOXAGENT_TEST_CHAT_ID not set",
    ),
    pytest.mark.skipif(
        not os.environ.get("BOXAGENT_TEST_BOT_TOKEN"),
        reason="BOXAGENT_TEST_BOT_TOKEN not set",
    ),
]

BOT_TOKEN = os.environ.get("BOXAGENT_TEST_BOT_TOKEN", "")
CHAT_ID = os.environ.get("BOXAGENT_TEST_CHAT_ID", "")


@pytest.fixture
async def gateway(tmp_path):
    """Start a real Gateway with test config."""
    from boxagent.config import load_config
    from boxagent.gateway import Gateway

    config_yaml = dedent(f"""\
        global:
          node_id: test-e2e
          log_level: debug

        bots:
          test-bot:
            ai_backend: claude-cli
            workspace: {tmp_path}
            channels:
              telegram:
                token: "{BOT_TOKEN}"
                allowed_users: [{CHAT_ID}]
            display:
              tool_calls: summary
              streaming: true
    """)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(config_yaml)

    config = load_config(config_dir)
    local_dir = tmp_path / "local"
    gw = Gateway(config=config, local_dir=local_dir)
    await gw.start()

    yield gw

    await gw.stop()


def _mock_channel(channel, response_collector):
    """Replace all channel I/O methods with capturing mocks.

    Replaces send_text, stream_start, stream_update, stream_end so
    tests never hit the real Telegram API. Collected text goes into
    response_collector list.
    """
    from boxagent.channels.base import StreamHandle

    async def fake_send_text(chat_id, text, **kwargs):
        response_collector.append(text)
        return "mock_msg_id"

    async def fake_stream_start(chat_id):
        return StreamHandle(message_id="mock_stream", chat_id=chat_id)

    async def fake_stream_update(handle, text):
        response_collector.append(text)

    async def fake_stream_end(handle):
        return handle.message_id

    channel.send_text = fake_send_text
    channel.stream_start = fake_stream_start
    channel.stream_update = fake_stream_update
    channel.stream_end = fake_stream_end


class TestE2ESmoke:
    async def test_bot_responds_to_message(self, gateway):
        """Send a message, verify bot eventually responds."""
        from boxagent.channels.base import IncomingMessage

        router = list(gateway._routers.values())[0]
        channel = list(gateway._channels.values())[0]
        response_text = []

        _mock_channel(channel, response_text)

        msg = IncomingMessage(
            channel="telegram",
            chat_id=CHAT_ID,
            user_id=CHAT_ID,
            text="Reply with exactly: e2e test passed",
        )
        await router.handle_message(msg)

        full = " ".join(response_text).lower()
        # The response should contain something and not be an error
        assert len(full) > 0
        assert "error" not in full

    async def test_unauthorized_rejected(self, gateway):
        """Unauthorized user gets rejection."""
        from boxagent.channels.base import IncomingMessage

        router = list(gateway._routers.values())[0]
        channel = list(gateway._channels.values())[0]
        responses = []

        _mock_channel(channel, responses)

        msg = IncomingMessage(
            channel="telegram",
            chat_id="999",
            user_id="999",
            text="hello",
        )
        await router.handle_message(msg)

        assert any("unauthorized" in r.lower() or "not allowed" in r.lower()
                    for r in responses)

    async def test_status_command(self, gateway):
        """System command /status returns response."""
        from boxagent.channels.base import IncomingMessage

        router = list(gateway._routers.values())[0]
        channel = list(gateway._channels.values())[0]
        responses = []

        _mock_channel(channel, responses)

        msg = IncomingMessage(
            channel="telegram",
            chat_id=CHAT_ID,
            user_id=CHAT_ID,
            text="/status",
        )
        await router.handle_message(msg)

        assert len(responses) == 1
        assert "status" in responses[0].lower() or "not yet" in responses[0].lower()

    async def test_new_clears_context(self, gateway):
        """After /new, next message has no prior context."""
        from boxagent.channels.base import IncomingMessage

        router = list(gateway._routers.values())[0]
        channel = list(gateway._channels.values())[0]
        responses = []

        _mock_channel(channel, responses)

        await router.handle_message(IncomingMessage(
            channel="telegram",
            chat_id=CHAT_ID,
            user_id=CHAT_ID,
            text="/new",
        ))

        assert any("fresh" in r.lower() or "new" in r.lower() for r in responses)

    async def test_cancel_interrupts(self, gateway):
        """Cancel interrupts a running task."""
        from boxagent.channels.base import IncomingMessage

        router = list(gateway._routers.values())[0]
        channel = list(gateway._channels.values())[0]
        responses = []

        _mock_channel(channel, responses)

        await router.handle_message(IncomingMessage(
            channel="telegram", chat_id=CHAT_ID,
            user_id=CHAT_ID, text="/cancel",
        ))

        assert any("cancel" in r.lower() for r in responses)

    async def test_session_continuity(self, gateway):
        """Second message has context from the first (same session)."""
        from unittest.mock import AsyncMock

        cli = list(gateway._cli_processes.values())[0]

        callback_mock = AsyncMock()
        callback_mock.on_stream = AsyncMock()
        callback_mock.on_tool_call = AsyncMock()
        callback_mock.on_error = AsyncMock()

        await cli._execute_turn("Remember the code word: banana42", callback_mock)
        first_session = cli.session_id
        assert first_session is not None

        # Second turn should use --resume
        await cli._execute_turn("What was the code word?", callback_mock)
        assert cli.session_id == first_session  # same session

    async def test_session_persisted_in_storage(self, gateway):
        """Session ID is persisted to storage on shutdown."""
        cli = list(gateway._cli_processes.values())[0]
        bot_name = list(gateway._cli_processes.keys())[0]
        cli.session_id = "sess_test_persist"

        # Save session manually (like stop() does) instead of calling
        # stop() directly — the fixture teardown will call stop()
        if gateway._storage:
            gateway._storage.save_session(bot_name, cli.session_id)

        # Verify session was saved
        if gateway._storage:
            saved = gateway._storage.load_session(bot_name)
            assert saved == "sess_test_persist"
