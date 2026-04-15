"""Unit tests for Router — auth, commands, dispatch."""

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from boxagent.channels.base import IncomingMessage
from boxagent.router import Router
from boxagent.router_callback import ChannelCallback


@pytest.fixture
def mock_cli():
    proc = AsyncMock()
    proc.send = AsyncMock()
    proc.cancel = AsyncMock()
    proc.state = "idle"
    proc.session_id = "sess_123"
    proc.supports_session_persistence = True
    proc.last_turn_failed = False
    proc.last_turn_error = ""
    proc.reset_session = AsyncMock(
        side_effect=lambda: setattr(proc, "session_id", None)
    )
    return proc


@pytest.fixture
def mock_channel():
    ch = AsyncMock()
    ch.send_text = AsyncMock()
    ch.stream_start = AsyncMock()
    ch.stream_update = AsyncMock()
    ch.stream_end = AsyncMock()
    ch.format_tool_call = lambda name, inp: f"tool:{name}"
    return ch


@pytest.fixture
def router(mock_cli, mock_channel):
    return Router(
        cli_process=mock_cli,
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
        mock_channel.send_text.assert_called_once()
        text = mock_channel.send_text.call_args[0][1].lower()
        assert "unauthorized" in text or "not allowed" in text

    async def test_authorized_passes(self, router, mock_cli):
        await router.handle_message(make_msg("hello"))
        mock_cli.send.assert_called_once()


class TestCommands:
    async def test_status_is_command(self, router, mock_channel, mock_cli):
        await router.handle_message(make_msg("/status"))
        mock_channel.send_text.assert_called_once()
        mock_cli.send.assert_not_called()

    async def test_new_is_command(self, router, mock_channel, mock_cli):
        await router.handle_message(make_msg("/new"))
        mock_channel.send_text.assert_called_once()
        mock_cli.send.assert_not_called()

    async def test_cancel_is_command(self, router, mock_channel, mock_cli):
        await router.handle_message(make_msg("/cancel"))
        mock_channel.send_text.assert_called_once()
        mock_cli.send.assert_not_called()

    async def test_start_uses_display_name(self, mock_channel, mock_cli):
        router = Router(
            cli_process=mock_cli,
            channel=mock_channel,
            allowed_users=[123456],
            bot_name="codex",
            display_name="Demo Bot",
        )

        await router.handle_message(make_msg("/start"))

        sent_text = mock_channel.send_text.call_args.args[1]
        assert "Welcome to Demo Bot!" in sent_text
        mock_cli.send.assert_not_called()

    async def test_resume_is_command(self, router, mock_channel, mock_cli):
        await router.handle_message(make_msg("/resume"))
        mock_channel.send_text.assert_called_once()
        mock_cli.send.assert_not_called()

    async def test_start_is_command(self, router, mock_channel, mock_cli):
        await router.handle_message(make_msg("/start"))
        mock_channel.send_text.assert_called_once()
        mock_cli.send.assert_not_called()

    async def test_help_is_command(self, router, mock_channel, mock_cli):
        await router.handle_message(make_msg("/help"))
        mock_channel.send_text.assert_called_once()
        mock_cli.send.assert_not_called()

    async def test_verbose_is_command(self, router, mock_channel, mock_cli):
        mock_channel.tool_calls_display = "summary"
        await router.handle_message(make_msg("/verbose"))
        mock_channel.send_text.assert_called_once()
        mock_cli.send.assert_not_called()

    async def test_sync_skills_is_command(self, router, mock_channel, mock_cli):
        await router.handle_message(make_msg("/sync_skills"))
        mock_channel.send_text.assert_called_once()
        mock_cli.send.assert_not_called()

    async def test_compact_is_command(self, router, mock_channel, mock_cli):
        mock_cli.session_id = None  # no session — quick return
        await router.handle_message(make_msg("/compact"))
        mock_channel.send_text.assert_called_once()
        mock_cli.send.assert_not_called()

    async def test_model_is_command(self, router, mock_channel, mock_cli):
        mock_cli.model = ""
        await router.handle_message(make_msg("/model"))
        mock_channel.send_text.assert_called_once()
        mock_cli.send.assert_not_called()

    async def test_unknown_slash_dispatched(self, router, mock_cli):
        await router.handle_message(make_msg("/foo bar"))
        mock_cli.send.assert_called_once()


class TestChannelCallback:
    async def test_inserts_paragraph_break_when_text_resumes_after_tool(self):
        channel = AsyncMock()
        handle = SimpleNamespace(message_id="m1", chat_id="123")
        channel.stream_start = AsyncMock(return_value=handle)
        channel.stream_update = AsyncMock()
        channel.send_text = AsyncMock()
        channel.format_tool_call = lambda name, inp: f"tool:{name}"

        cb = ChannelCallback(channel=channel, chat_id="123")

        await cb.on_stream("Before tool.")
        await cb.on_tool_call("Read", {"path": "x"}, "")
        await cb.on_stream("After tool.")

        assert cb.collected_text == "Before tool.\n\nAfter tool."
        assert channel.stream_update.mock_calls[0].args[1] == "Before tool."
        assert channel.stream_update.mock_calls[1].args[1] == "\ntool:Read\n"
        assert channel.stream_update.mock_calls[2].args[1] == "\n\nAfter tool."


@dataclass
class _StreamingBackend:
    stream_text: str
    session_id: str | None = None
    supports_session_persistence: bool = False

    async def send(self, prompt, callback, model="", chat_id="", append_system_prompt=""):
        await callback.on_stream(self.stream_text)

    async def cancel(self):
        return None


class TestDispatch:
    async def test_normal_dispatched(self, router, mock_cli):
        await router.handle_message(make_msg("explain this code"))
        mock_cli.send.assert_called_once()
        assert "explain this code" in mock_cli.send.call_args[0][0]

    async def test_dispatch_persists_supported_sessions(
        self, mock_channel, mock_cli
    ):
        storage = MagicMock()
        mock_cli.supports_session_persistence = True
        router = Router(
            cli_process=mock_cli,
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
        )

    async def test_stream_reply_does_not_use_display_name_prefix(self, mock_channel):
        backend = _StreamingBackend(stream_text="hello")
        handle = SimpleNamespace(message_id="m1", chat_id="123456")
        mock_channel.stream_start = AsyncMock(return_value=handle)

        router = Router(
            cli_process=backend,
            channel=mock_channel,
            allowed_users=[123456],
            bot_name="codex",
            display_name="Demo Bot",
        )

        await router.handle_message(make_msg("hi"))

        mock_channel.stream_update.assert_any_call(
            handle,
            "hello",
        )

    async def test_dispatch_persists_codex_session_reference_too(
        self, mock_channel, mock_cli
    ):
        storage = MagicMock()
        mock_cli.supports_session_persistence = False
        router = Router(
            cli_process=mock_cli,
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
        )

    async def test_failed_turn_logs_error_into_transcript(
        self, mock_channel, mock_cli, tmp_path
    ):
        async def fail_send(prompt, callback, model="", chat_id=""):
            mock_cli.last_turn_failed = True
            mock_cli.last_turn_error = "Claude CLI exit code 1: broken"
            await callback.on_error(mock_cli.last_turn_error)

        mock_cli.send.side_effect = fail_send
        router = Router(
            cli_process=mock_cli,
            channel=mock_channel,
            allowed_users=[123456],
            bot_name="bot-1",
            local_dir=tmp_path,
        )

        await router.handle_message(make_msg("hello"))

        transcript = (tmp_path / "transcripts" / "sess_123.jsonl").read_text()
        assert "Error: Claude CLI exit code 1: broken" in transcript
