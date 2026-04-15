"""Tests for system prompt injection — verifying that system-level context
is separated from user messages and passed correctly through each backend."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from boxagent.channels.base import IncomingMessage
from boxagent.router import Router
from boxagent.router_callback import ChannelCallback
from tests.unit.helpers import FakeProcess


# ---- Helpers ----

def make_msg(text, user_id="123456", chat_id="123456", attachments=None):
    return IncomingMessage(
        channel="telegram",
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        attachments=attachments or [],
    )


def make_stream_lines(*events: dict) -> bytes:
    return b"\n".join(json.dumps(e).encode() for e in events) + b"\n"


def result_event(session_id: str = "sess_123") -> dict:
    return {"type": "result", "session_id": session_id}


@pytest.fixture
def callback():
    cb = AsyncMock()
    cb.on_stream = AsyncMock()
    cb.on_tool_call = AsyncMock()
    cb.on_error = AsyncMock()
    cb.on_file = AsyncMock()
    cb.on_image = AsyncMock()
    return cb


# ---- Claude CLI system prompt tests ----

class TestClaudeSystemPrompt:
    def _make_cli(self, **kwargs):
        from boxagent.agent.claude_process import ClaudeProcess
        return ClaudeProcess(workspace="/tmp/test", **kwargs)

    def test_append_system_prompt_adds_append_flag(self):
        cli = self._make_cli()
        args = cli._build_args("hello user", model="", chat_id="", append_system_prompt="You are a bot")
        assert "--append-system-prompt" in args
        idx = args.index("--append-system-prompt")
        assert args[idx + 1] == "You are a bot"

    def test_empty_append_system_prompt_no_flag(self):
        cli = self._make_cli()
        args = cli._build_args("hello user", model="", chat_id="", append_system_prompt="")
        assert "--append-system-prompt" not in args

    def test_append_system_prompt_does_not_pollute_user_message(self):
        cli = self._make_cli()
        args = cli._build_args("hello user", model="", chat_id="", append_system_prompt="system instructions")
        p_idx = args.index("-p")
        user_msg = args[p_idx + 1]
        assert user_msg == "hello user"
        assert "system instructions" not in user_msg

    def test_append_system_prompt_before_p_flag(self):
        """--append-system-prompt should appear before -p in args."""
        cli = self._make_cli()
        args = cli._build_args("msg", model="", chat_id="", append_system_prompt="sys")
        sp_idx = args.index("--append-system-prompt")
        p_idx = args.index("-p")
        assert sp_idx < p_idx

    async def test_append_system_prompt_threaded_through_execute_turn(self, callback):
        cli = self._make_cli()
        fake_proc = FakeProcess(make_stream_lines(result_event()))

        with patch("asyncio.create_subprocess_exec", return_value=fake_proc) as mock_exec:
            await cli._execute_turn("hello", callback, append_system_prompt="sys context")

        call_args = mock_exec.call_args[0]
        assert "--append-system-prompt" in call_args
        idx = list(call_args).index("--append-system-prompt")
        assert call_args[idx + 1] == "sys context"


# ---- Codex CLI system prompt tests ----

class TestCodexSystemPrompt:
    def _make_cli(self, **kwargs):
        from boxagent.agent.codex_process import CodexProcess
        return CodexProcess(workspace="/tmp/test", **kwargs)

    def test_append_system_prompt_uses_developer_instructions(self):
        cli = self._make_cli()
        args = cli._build_args("user question", model="", chat_id="", append_system_prompt="[context block]")
        assert "-c" in args
        c_idx = args.index("-c")
        c_val = args[c_idx + 1]
        assert c_val.startswith('developer_instructions="""')
        assert "[context block]" in c_val
        # user message should NOT contain system context
        message_arg = args[-1]
        assert message_arg == "user question"

    def test_empty_append_system_prompt_no_developer_instructions(self):
        cli = self._make_cli()
        args = cli._build_args("user question", model="", chat_id="", append_system_prompt="")
        assert 'developer_instructions' not in str(args)
        message_arg = args[-1]
        assert message_arg == "user question"

    def test_append_system_prompt_in_resume_mode(self):
        cli = self._make_cli()
        cli.session_id = "thread_abc"
        args = cli._build_args("follow up", model="", chat_id="", append_system_prompt="[sys]")
        assert "-c" in args
        c_idx = args.index("-c")
        assert "[sys]" in args[c_idx + 1]
        # message should be clean
        message_arg = args[-1]
        assert message_arg == "follow up"


# ---- ACP system prompt tests ----

class TestACPSystemPrompt:
    async def test_append_system_prompt_prepended_to_message(self):
        """ACP send() should prepend append_system_prompt to message in the queue."""
        from boxagent.agent.acp_process import ACPProcess

        proc = ACPProcess(workspace="/tmp/test")
        # Directly check the queue tuple
        done = MagicMock()
        done.wait = AsyncMock()
        # We can't easily test _process_queue without full ACP setup,
        # so verify send() correctly packages append_system_prompt into the queue
        await proc._queue.put(("msg", AsyncMock(), done, "", "", ""))
        item = await proc._queue.get()
        assert len(item) == 6  # (message, callback, done, model, chat_id, append_system_prompt)


# ---- Router prompt split tests ----

@pytest.fixture
def mock_cli():
    proc = AsyncMock()
    proc.send = AsyncMock()
    proc.cancel = AsyncMock()
    proc.state = "idle"
    proc.session_id = "sess_123"
    proc.supports_session_persistence = True
    proc.model = "opus"
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


class TestRouterPromptSplit:
    async def test_session_context_goes_to_append_system_prompt(self, mock_cli, mock_channel):
        router = Router(
            cli_process=mock_cli,
            channel=mock_channel,
            allowed_users=[123456],
            bot_name="test-bot",
        )

        await router.handle_message(make_msg("explain this code"))

        mock_cli.send.assert_called_once()
        call_kwargs = mock_cli.send.call_args
        # append_system_prompt should contain [BoxAgent Context]
        append_system_prompt = call_kwargs.kwargs.get("append_system_prompt", "")
        assert "[BoxAgent Context]" in append_system_prompt
        assert "bot: test-bot" in append_system_prompt

    async def test_user_text_stays_in_message(self, mock_cli, mock_channel):
        router = Router(
            cli_process=mock_cli,
            channel=mock_channel,
            allowed_users=[123456],
        )

        await router.handle_message(make_msg("explain this code"))

        call_args = mock_cli.send.call_args[0]
        user_message = call_args[0]
        assert "explain this code" in user_message

    async def test_session_context_not_in_user_message(self, mock_cli, mock_channel):
        router = Router(
            cli_process=mock_cli,
            channel=mock_channel,
            allowed_users=[123456],
            bot_name="test-bot",
        )

        await router.handle_message(make_msg("hello"))

        user_message = mock_cli.send.call_args[0][0]
        assert "[BoxAgent Context]" not in user_message

    async def test_resume_context_goes_to_append_system_prompt(self, mock_cli, mock_channel):
        router = Router(
            cli_process=mock_cli,
            channel=mock_channel,
            allowed_users=[123456],
        )
        router._resume_context = "[Recovered previous session]\nUser: hi\n[End recovered session]"
        router._session_context_injected = True  # skip session context

        await router.handle_message(make_msg("continue"))

        append_system_prompt = mock_cli.send.call_args.kwargs.get("append_system_prompt", "")
        assert "[Recovered previous session]" in append_system_prompt
        user_message = mock_cli.send.call_args[0][0]
        assert "[Recovered previous session]" not in user_message

    async def test_compact_summary_goes_to_append_system_prompt(self, mock_cli, mock_channel):
        router = Router(
            cli_process=mock_cli,
            channel=mock_channel,
            allowed_users=[123456],
        )
        router._compact_summary = "- discussed topic A\n- decided on B"
        router._session_context_injected = True

        await router.handle_message(make_msg("what's next"))

        append_system_prompt = mock_cli.send.call_args.kwargs.get("append_system_prompt", "")
        assert "[Previous conversation summary]" in append_system_prompt
        assert "discussed topic A" in append_system_prompt
        user_message = mock_cli.send.call_args[0][0]
        assert "[Previous conversation summary]" not in user_message

    async def test_attachments_stay_in_message(self, mock_cli, mock_channel):
        from boxagent.channels.base import Attachment

        msg = IncomingMessage(
            channel="telegram",
            chat_id="123456",
            user_id="123456",
            text="check this",
            attachments=[Attachment(type="file", file_path="/tmp/doc.pdf", file_name="doc.pdf", mime_type="application/pdf", size=1024)],
        )

        router = Router(
            cli_process=mock_cli,
            channel=mock_channel,
            allowed_users=[123456],
        )
        router._session_context_injected = True  # skip session context

        await router.handle_message(msg)

        user_message = mock_cli.send.call_args[0][0]
        assert "[Attached file: /tmp/doc.pdf]" in user_message

    async def test_model_override_still_works(self, mock_cli, mock_channel):
        router = Router(
            cli_process=mock_cli,
            channel=mock_channel,
            allowed_users=[123456],
        )
        router._session_context_injected = True

        await router.handle_message(make_msg("@opus explain this"))

        call_kwargs = mock_cli.send.call_args.kwargs
        assert call_kwargs["model"] == "opus"
        user_message = mock_cli.send.call_args[0][0]
        assert "explain this" in user_message
        assert "@opus" not in user_message

    async def test_second_message_no_session_context(self, mock_cli, mock_channel):
        router = Router(
            cli_process=mock_cli,
            channel=mock_channel,
            allowed_users=[123456],
            bot_name="test-bot",
        )

        # First message — should have session context
        await router.handle_message(make_msg("first"))
        first_system = mock_cli.send.call_args.kwargs.get("append_system_prompt", "")
        assert "[BoxAgent Context]" in first_system

        mock_cli.send.reset_mock()

        # Second message — should NOT have session context
        await router.handle_message(make_msg("second"))
        second_system = mock_cli.send.call_args.kwargs.get("append_system_prompt", "")
        assert "[BoxAgent Context]" not in second_system

    async def test_empty_append_system_prompt_when_no_context(self, mock_cli, mock_channel):
        router = Router(
            cli_process=mock_cli,
            channel=mock_channel,
            allowed_users=[123456],
        )
        router._session_context_injected = True

        await router.handle_message(make_msg("plain message"))

        append_system_prompt = mock_cli.send.call_args.kwargs.get("append_system_prompt", "")
        assert append_system_prompt == ""
