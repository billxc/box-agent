"""Unit tests for Router — auth, commands, dispatch."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from boxagent.transports.base import IncomingMessage
from boxagent.router import Router
from boxagent.router import ChannelCallback
from boxagent.testing.mocks import MockBackend, MockChannel


@pytest.fixture
def mock_cli():
    return MockBackend(session_id="sess_123", supports_session_persistence=True)


@pytest.fixture
def mock_channel():
    return MockChannel()


@pytest.fixture
def router(mock_cli, mock_channel):
    return Router(
        backend=mock_cli,
        channel=mock_channel,
        allowed_users=[123456],
    )


def make_msg(text, user_id="123456", chat_id="123456"):
    return IncomingMessage(
        channel="telegram", chat_id=chat_id,
        user_id=user_id, text=text,
    )


class TestAuth:
    async def test_unauthorized_rejected(self, router, mock_channel):
        await router.handle_message(make_msg("hello", user_id="999"))
        assert len(mock_channel.sent_texts) == 1
        text = mock_channel.sent_texts[0][1].lower()
        assert "unauthorized" in text or "not allowed" in text

    async def test_authorized_passes(self, router, mock_cli):
        await router.handle_message(make_msg("hello"))
        assert len(mock_cli.sends) == 1


class TestCommands:
    async def test_status_is_command(self, router, mock_channel, mock_cli):
        await router.handle_message(make_msg("/status"))
        assert len(mock_channel.sent_texts) == 1
        assert mock_cli.sends == []

    async def test_new_is_command(self, router, mock_channel, mock_cli):
        await router.handle_message(make_msg("/new"))
        assert len(mock_channel.sent_texts) == 1
        assert mock_cli.sends == []

    async def test_cancel_is_command(self, router, mock_channel, mock_cli):
        await router.handle_message(make_msg("/cancel"))
        assert len(mock_channel.sent_texts) == 1
        assert mock_cli.sends == []

    async def test_start_uses_display_name(self, mock_channel, mock_cli):
        router = Router(
            backend=mock_cli,
            channel=mock_channel,
            allowed_users=[123456],
            bot_name="codex",
            display_name="Demo Bot",
        )

        await router.handle_message(make_msg("/start"))

        assert "Welcome to Demo Bot!" in mock_channel.sent_texts[0][1]
        assert mock_cli.sends == []

    async def test_resume_is_command(self, router, mock_channel, mock_cli):
        await router.handle_message(make_msg("/resume"))
        assert len(mock_channel.sent_texts) == 1
        assert mock_cli.sends == []

    async def test_start_is_command(self, router, mock_channel, mock_cli):
        await router.handle_message(make_msg("/start"))
        assert len(mock_channel.sent_texts) == 1
        assert mock_cli.sends == []

    async def test_help_is_command(self, router, mock_channel, mock_cli):
        await router.handle_message(make_msg("/help"))
        assert len(mock_channel.sent_texts) == 1
        assert mock_cli.sends == []

    async def test_verbose_is_command(self, router, mock_channel, mock_cli):
        mock_channel.tool_calls_display = "summary"
        await router.handle_message(make_msg("/verbose"))
        assert len(mock_channel.sent_texts) == 1
        assert mock_cli.sends == []

    async def test_sync_skills_is_command(self, router, mock_channel, mock_cli):
        await router.handle_message(make_msg("/sync_skills"))
        assert len(mock_channel.sent_texts) == 1
        assert mock_cli.sends == []

    async def test_compact_is_command(self, router, mock_channel, mock_cli):
        mock_cli.session_id = None  # no session — quick return
        await router.handle_message(make_msg("/compact"))
        assert len(mock_channel.sent_texts) == 1
        assert mock_cli.sends == []

    async def test_model_is_command(self, router, mock_channel, mock_cli):
        mock_cli.model = ""
        await router.handle_message(make_msg("/model"))
        assert len(mock_channel.sent_texts) == 1
        assert mock_cli.sends == []

    async def test_unknown_slash_dispatched(self, router, mock_cli):
        await router.handle_message(make_msg("/foo bar"))
        assert len(mock_cli.sends) == 1


class TestChannelCallback:
    async def test_inserts_paragraph_break_when_text_resumes_after_tool(self):
        # ChannelCallback's contract is wider than the Channel Protocol —
        # it expects channels to expose a (transport-specific)
        # ``format_tool_call`` and a polymorphic ``on_tool_call``. Stick
        # with hand-rolled AsyncMock here so we can wire that custom
        # behaviour without polluting MockChannel.
        channel = AsyncMock()
        handle = SimpleNamespace(message_id="m1", chat_id="123")
        channel.stream_start = AsyncMock(return_value=handle)
        channel.stream_update = AsyncMock()
        channel.send_text = AsyncMock()
        channel.format_tool_call = lambda name, inp: f"tool:{name}"

        async def _on_tool_call(chat_id, tool_id, name, inp, result, *, stream_handle=None, webhook_name=""):
            fmt = channel.format_tool_call(name, inp)
            await channel.stream_update(stream_handle, f"\n{fmt}\n")
            return True
        channel.on_tool_call = _on_tool_call

        callback = ChannelCallback(channel=channel, chat_id="123")

        await callback.on_stream("Before tool.")
        await callback.on_tool_call("Read", {"path": "x"}, "")
        await callback.on_stream("After tool.")

        assert callback.collected_text == "Before tool.\n\nAfter tool."
        assert channel.stream_update.mock_calls[0].args[1] == "Before tool."
        assert channel.stream_update.mock_calls[1].args[1] == "\ntool:Read\n"
        assert channel.stream_update.mock_calls[2].args[1] == "\n\nAfter tool."


class TestDispatch:
    async def test_normal_dispatched(self, router, mock_cli):
        await router.handle_message(make_msg("explain this code"))
        assert len(mock_cli.sends) == 1
        assert "explain this code" in mock_cli.sends[0].message

    async def test_dispatch_persists_supported_sessions(
        self, mock_channel, mock_cli
    ):
        storage = MagicMock()
        mock_cli.supports_session_persistence = True
        router = Router(
            backend=mock_cli,
            channel=mock_channel,
            allowed_users=[123456],
            storage=storage,
            bot_name="bot-1",
        )

        await router.handle_message(make_msg("hello"))

        storage.save_session.assert_called_once_with(
            "bot-1",
            "sess_123",
            preview="hello",
            backend="claude-cli",
            chat_id="123456",
            model="",
            workspace="",
        )
        backend = MockBackend()
        backend.script(["hello"])

        router = Router(
            backend=backend,
            channel=mock_channel,
            allowed_users=[123456],
            bot_name="codex",
            display_name="Demo Bot",
        )

        await router.handle_message(make_msg("hi"))

        # The streaming backend's "hello" chunk lands in MockChannel's
        # latest stream record.
        assert mock_channel.streams[-1].chunks[-1] == "hello"

    async def test_dispatch_persists_codex_session_reference_too(
        self, mock_channel, mock_cli
    ):
        storage = MagicMock()
        mock_cli.supports_session_persistence = False
        router = Router(
            backend=mock_cli,
            channel=mock_channel,
            allowed_users=[123456],
            storage=storage,
            bot_name="bot-1",
        )

        await router.handle_message(make_msg("hello"))

        storage.save_session.assert_called_once_with(
            "bot-1",
            "sess_123",
            preview="hello",
            backend="claude-cli",
            chat_id="123456",
            model="",
            workspace="",
        )

    async def test_failed_turn_logs_error_into_transcript(
        self, mock_channel, mock_cli, tmp_path
    ):
        async def fail_send(message, callback, **kwargs):
            await callback.on_error("Claude CLI exit code 1: broken")

        mock_cli.script_handler(fail_send)
        mock_cli.fail_next_turn("Claude CLI exit code 1: broken")

        router = Router(
            backend=mock_cli,
            channel=mock_channel,
            allowed_users=[123456],
            bot_name="bot-1",
            local_dir=tmp_path,
        )

        await router.handle_message(make_msg("hello"))

        transcript = (tmp_path / "transcripts" / "sess_123.jsonl").read_text()
        assert "Error: Claude CLI exit code 1: broken" in transcript
