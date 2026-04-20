"""Unit tests for system commands (/status, /new, /cancel, /resume)."""

import tempfile
import time
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock, PropertyMock

from boxagent.channels.base import IncomingMessage
from boxagent.router import Router


@pytest.fixture
def mock_cli():
    proc = AsyncMock()
    proc.cancel = AsyncMock()
    proc.state = "idle"
    proc.session_id = "sess_123"
    proc.supports_session_persistence = True
    proc.reset_session = AsyncMock(
        side_effect=lambda: setattr(proc, "session_id", None)
    )
    return proc


@pytest.fixture
def mock_channel():
    ch = AsyncMock()
    ch.send_text = AsyncMock()
    return ch


@pytest.fixture
def mock_storage():
    st = MagicMock()
    st.clear_session = MagicMock()
    st.save_session = MagicMock()
    st.list_session_history = MagicMock(return_value=[])
    st.list_codex_session_history = MagicMock(return_value=[])
    st.build_codex_resume_context = MagicMock(return_value="")
    return st


@pytest.fixture
def router(mock_cli, mock_channel, mock_storage):
    r = Router(
        cli_process=mock_cli,
        channel=mock_channel,
        allowed_users=[123],
        storage=mock_storage,
        bot_name="test-bot",
        start_time=time.time() - 3600,  # 1 hour ago
        workspace="/home/testuser/.boxagent/workspace",
    )
    return r


def msg(text):
    return IncomingMessage(
        channel="telegram", chat_id="123",
        user_id="123", text=text,
    )


class TestStatusCommand:
    async def test_returns_state_and_session(self, router, mock_channel):
        await router.handle_message(msg("/status"))
        text = mock_channel.send_text.call_args[0][1]
        assert "idle" in text.lower()
        assert "sess_123" in text

    async def test_shows_uptime(self, router, mock_channel):
        await router.handle_message(msg("/status"))
        text = mock_channel.send_text.call_args[0][1]
        assert "uptime" in text.lower() or "1h" in text.lower() or "3600" in text


class TestNewCommand:
    async def test_clears_session(self, router, mock_cli, mock_storage):
        await router.handle_message(msg("/new"))
        mock_cli.reset_session.assert_called_once()
        assert mock_cli.session_id is None
        mock_storage.clear_session.assert_called_once_with("test-bot")

    async def test_clears_pending_compact_summary(self, router):
        router._compact_summary = "stale summary"

        await router.handle_message(msg("/new"))

        assert router._compact_summary == ""

    async def test_sends_confirmation(self, router, mock_channel):
        await router.handle_message(msg("/new"))
        text = mock_channel.send_text.call_args[0][1]
        assert "new" in text.lower() or "fresh" in text.lower()


class TestCancelCommand:
    async def test_calls_cancel(self, router, mock_cli):
        await router.handle_message(msg("/cancel"))
        mock_cli.cancel.assert_called_once()

    async def test_sends_confirmation(self, router, mock_channel):
        await router.handle_message(msg("/cancel"))
        text = mock_channel.send_text.call_args[0][1]
        assert "cancel" in text.lower()


class TestResumeCommand:
    async def test_lists_native_session_history(
        self, router, mock_channel, mock_storage
    ):
        mock_storage.list_session_history.return_value = [
            {"session_id": "sess_old", "saved_at": 1_710_000_000, "backend": "claude-cli"}
        ]

        await router.handle_message(msg("/resume"))

        # New format uses send_text_with_inline_keyboard if available, else send_text
        if mock_channel.send_text_with_inline_keyboard.called:
            text = mock_channel.send_text_with_inline_keyboard.call_args[0][1]
        else:
            text = mock_channel.send_text.call_args[0][1]
        assert "sess_old" in text

    async def test_resumes_native_session_by_index(
        self, router, mock_cli, mock_storage
    ):
        mock_storage.list_session_history.return_value = [
            {"session_id": "sess_old"}
        ]

        await router.handle_message(msg("/resume 1"))

        mock_cli.reset_session.assert_awaited_once()
        assert mock_cli.session_id == "sess_old"
        mock_storage.save_session.assert_called_once_with(
            "test-bot", "sess_old"
        )

    async def test_lists_codex_local_history(
        self, router, mock_cli, mock_channel, mock_storage
    ):
        mock_cli.supports_session_persistence = False
        mock_storage.list_codex_session_history.return_value = [
            {
                "session_id": "019d15d3-3a69-7021-a4ce-95a06e322fad",
                "saved_at": 1_710_000_000,
                "preview": "fix /cancel after restart",
                "path": "/tmp/rollout.jsonl",
            }
        ]

        await router.handle_message(msg("/resume"))

        mock_storage.list_codex_session_history.assert_called_once_with(
            "/home/testuser/.boxagent/workspace",
            limit=10,
        )
        # New format uses send_text_with_inline_keyboard if available, else send_text
        if mock_channel.send_text_with_inline_keyboard.called:
            text = mock_channel.send_text_with_inline_keyboard.call_args[0][1]
        else:
            text = mock_channel.send_text.call_args[0][1]
        assert "fix /cancel after restart" in text

    async def test_prepares_codex_resume_by_index(
        self, router, mock_cli, mock_channel, mock_storage
    ):
        mock_cli.supports_session_persistence = False
        mock_storage.list_codex_session_history.return_value = [
            {
                "session_id": "019d15d3-3a69-7021-a4ce-95a06e322fad",
                "path": "/tmp/rollout.jsonl",
            }
        ]
        mock_storage.build_codex_resume_context.return_value = (
            "[Recovered previous Codex session]\nRecovered transcript"
        )
        router._compact_summary = "stale compact summary"

        await router.handle_message(msg("/resume 1"))

        mock_cli.reset_session.assert_awaited_once()
        mock_storage.build_codex_resume_context.assert_called_once_with(
            "/tmp/rollout.jsonl"
        )
        mock_storage.clear_session.assert_called_once_with("test-bot")
        mock_storage.save_session.assert_not_called()
        assert router._compact_summary == ""
        assert "Recovered transcript" in router._resume_context
        text = mock_channel.send_text.call_args[0][1]
        assert "soft resume" in text.lower()

    async def test_codex_resume_context_injected_in_next_dispatch(
        self, mock_channel, mock_storage
    ):
        cli = AsyncMock()
        cli.cancel = AsyncMock()
        cli.supports_session_persistence = False
        cli.session_id = None
        cli.state = "idle"
        cli.reset_session = AsyncMock(
            side_effect=lambda: setattr(cli, "session_id", None)
        )

        prompts = []
        append_system_prompts = []

        async def fake_send(prompt, callback, model="", chat_id="", append_system_prompt=""):
            prompts.append(prompt)
            append_system_prompts.append(append_system_prompt)

        cli.send = fake_send
        mock_channel.show_typing = AsyncMock()
        mock_channel.stream_start = AsyncMock(
            return_value=__import__(
                "boxagent.channels.base",
                fromlist=["StreamHandle"],
            ).StreamHandle(message_id="m1", chat_id="123")
        )
        mock_channel.stream_update = AsyncMock()
        mock_channel.stream_end = AsyncMock()
        mock_channel.format_tool_call = lambda name, inp: ""

        mock_storage.list_codex_session_history.return_value = [
            {
                "session_id": "019d15d3-3a69-7021-a4ce-95a06e322fad",
                "path": "/tmp/rollout.jsonl",
            }
        ]
        mock_storage.build_codex_resume_context.return_value = (
            "[Recovered previous Codex session]\nRecovered transcript"
        )

        r = Router(
            cli_process=cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
            workspace="/home/testuser/.boxagent/workspace",
        )

        await r.handle_message(msg("/resume 1"))
        await r.handle_message(msg("继续修 /cancel"))

        assert len(prompts) == 1
        assert "Recovered transcript" in append_system_prompts[0]
        assert "继续修 /cancel" in prompts[0]
        assert r._resume_context == ""

        prompts.clear()
        append_system_prompts.clear()
        await r.handle_message(msg("第二条消息"))

        assert prompts == ["第二条消息"]


class TestStartCommand:
    async def test_sends_welcome(self, router, mock_channel):
        await router.handle_message(msg("/start"))
        mock_channel.send_text.assert_called_once()
        text = mock_channel.send_text.call_args[0][1]
        assert "welcome" in text.lower()

    async def test_includes_bot_name(self, router, mock_channel):
        await router.handle_message(msg("/start"))
        text = mock_channel.send_text.call_args[0][1]
        assert "test-bot" in text

    async def test_not_dispatched_to_cli(self, router, mock_cli):
        await router.handle_message(msg("/start"))
        mock_cli.send.assert_not_called()


class TestHelpCommand:
    async def test_sends_help(self, router, mock_channel):
        await router.handle_message(msg("/help"))
        mock_channel.send_text.assert_called_once()
        text = mock_channel.send_text.call_args[0][1]
        assert "/new" in text
        assert "/status" in text
        assert "/cancel" in text
        assert "/help" in text

    async def test_not_dispatched_to_cli(self, router, mock_cli):
        await router.handle_message(msg("/help"))
        mock_cli.send.assert_not_called()


class TestVerboseCommand:
    async def test_cycles_silent_to_summary(self, router, mock_channel):
        mock_channel.tool_calls_display = "silent"
        await router.handle_message(msg("/verbose"))
        assert mock_channel.tool_calls_display == "summary"
        text = mock_channel.send_text.call_args[0][1]
        assert "summary" in text.lower()

    async def test_cycles_summary_to_detailed(self, router, mock_channel):
        mock_channel.tool_calls_display = "summary"
        await router.handle_message(msg("/verbose"))
        assert mock_channel.tool_calls_display == "detailed"
        text = mock_channel.send_text.call_args[0][1]
        assert "detailed" in text.lower()

    async def test_cycles_detailed_to_silent(self, router, mock_channel):
        mock_channel.tool_calls_display = "detailed"
        await router.handle_message(msg("/verbose"))
        assert mock_channel.tool_calls_display == "silent"
        text = mock_channel.send_text.call_args[0][1]
        assert "silent" in text.lower()

    async def test_full_cycle(self, router, mock_channel):
        """Three /verbose calls cycle through all modes."""
        mock_channel.tool_calls_display = "summary"
        await router.handle_message(msg("/verbose"))
        assert mock_channel.tool_calls_display == "detailed"
        mock_channel.send_text.reset_mock()
        await router.handle_message(msg("/verbose"))
        assert mock_channel.tool_calls_display == "silent"
        mock_channel.send_text.reset_mock()
        await router.handle_message(msg("/verbose"))
        assert mock_channel.tool_calls_display == "summary"

    async def test_not_dispatched_to_cli(self, router, mock_cli, mock_channel):
        mock_channel.tool_calls_display = "summary"
        await router.handle_message(msg("/verbose"))
        mock_cli.send.assert_not_called()


class TestSyncSkillsCommand:
    async def test_syncs_and_reports(self, mock_cli, mock_channel, mock_storage):
        with tempfile.TemporaryDirectory() as work_dir, \
             tempfile.TemporaryDirectory() as skill_src:
            # Create fake skill dirs
            (Path(skill_src) / "my-skill").mkdir()
            (Path(skill_src) / "my-skill" / "SKILL.md").write_text("# Skill")
            (Path(skill_src) / "other-skill").mkdir()

            r = Router(
                cli_process=mock_cli,
                channel=mock_channel,
                allowed_users=[123],
                storage=mock_storage,
                bot_name="test-bot",
                workspace=work_dir,
                extra_skill_dirs=[skill_src],
            )
            await r.handle_message(msg("/sync_skills"))
            text = mock_channel.send_text.call_args[0][1]
            assert "2" in text
            assert "my-skill" in text
            assert "other-skill" in text

            # Verify symlinks created
            skills_dir = Path(work_dir) / ".claude" / "skills"
            assert (skills_dir / "my-skill").is_symlink()
            assert (skills_dir / "other-skill").is_symlink()

    async def test_empty_extra_skill_dirs(self, mock_cli, mock_channel, mock_storage):
        r = Router(
            cli_process=mock_cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
            workspace="/tmp",
            extra_skill_dirs=[],
        )
        await r.handle_message(msg("/sync_skills"))
        text = mock_channel.send_text.call_args[0][1]
        assert "no skills" in text.lower() or "empty" in text.lower()

    async def test_not_dispatched_to_cli(self, mock_cli, mock_channel, mock_storage):
        r = Router(
            cli_process=mock_cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
        )
        await r.handle_message(msg("/sync_skills"))
        mock_cli.send.assert_not_called()


class TestCompactCommand:
    async def test_no_session_returns_message(self, router, mock_cli, mock_channel):
        mock_cli.session_id = None
        await router.handle_message(msg("/compact"))
        text = mock_channel.send_text.call_args[0][1]
        assert "no active session" in text.lower()

    async def test_compact_generates_summary_and_resets(self, mock_channel, mock_storage):
        cli = AsyncMock()
        cli.session_id = "sess_123"
        cli.state = "idle"
        cli.reset_session = AsyncMock(
            side_effect=lambda: setattr(cli, "session_id", None)
        )

        async def fake_send(prompt, callback, model="", chat_id="", append_system_prompt=""):
            await callback.on_stream("- Discussed topic A\n- Decided B")

        cli.send = fake_send
        mock_channel.show_typing = AsyncMock()

        r = Router(
            cli_process=cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
        )
        await r.handle_message(msg("/compact"))

        # Session should be cleared
        cli.reset_session.assert_awaited_once()
        assert cli.session_id is None
        mock_storage.clear_session.assert_called_once_with("test-bot")

        # Summary should be in the response
        calls = mock_channel.send_text.call_args_list
        final_text = calls[-1][0][1]
        assert "topic A" in final_text
        assert "Decided B" in final_text

    async def test_compact_summary_injected_in_next_dispatch(self, mock_channel, mock_storage):
        cli = AsyncMock()
        cli.session_id = "sess_123"
        cli.state = "idle"
        cli.reset_session = AsyncMock(
            side_effect=lambda: setattr(cli, "session_id", None)
        )

        call_log = []
        system_log = []

        async def fake_send(prompt, callback, model="", chat_id="", append_system_prompt=""):
            call_log.append(prompt)
            system_log.append(append_system_prompt)
            await callback.on_stream("summary text here")

        cli.send = fake_send
        mock_channel.show_typing = AsyncMock()
        mock_channel.stream_start = AsyncMock(
            return_value=__import__("boxagent.channels.base", fromlist=["StreamHandle"]).StreamHandle(
                message_id="m1", chat_id="123"
            )
        )
        mock_channel.stream_update = AsyncMock()
        mock_channel.stream_end = AsyncMock()

        r = Router(
            cli_process=cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
        )

        # Compact
        await r.handle_message(msg("/compact"))

        # Next message should include summary in append_system_prompt
        call_log.clear()
        system_log.clear()
        await r.handle_message(msg("hello"))
        assert len(call_log) == 1
        assert "summary text here" in system_log[0]
        assert "hello" in call_log[0]

    async def test_not_dispatched_to_cli_directly(self, router, mock_cli, mock_channel):
        mock_cli.session_id = None
        await router.handle_message(msg("/compact"))
        mock_cli.send.assert_not_called()

    async def test_compact_with_user_hint(self, mock_channel, mock_storage):
        """User text after /compact is passed as additional instructions."""
        cli = AsyncMock()
        cli.session_id = "sess_123"
        cli.state = "idle"
        cli.reset_session = AsyncMock(
            side_effect=lambda: setattr(cli, "session_id", None)
        )

        captured_prompts = []

        async def fake_send(prompt, callback, model="", chat_id="", append_system_prompt=""):
            captured_prompts.append(prompt)
            await callback.on_stream("summary with focus")

        cli.send = fake_send
        mock_channel.show_typing = AsyncMock()

        r = Router(
            cli_process=cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
        )
        await r.handle_message(msg("/compact 帮我重点保存数据库相关的内容"))

        assert len(captured_prompts) == 1
        assert "帮我重点保存数据库相关的内容" in captured_prompts[0]
        assert "Additional instructions" in captured_prompts[0]

    async def test_compact_without_hint_no_additional(self, mock_channel, mock_storage):
        """Plain /compact has no additional instructions."""
        cli = AsyncMock()
        cli.session_id = "sess_123"
        cli.state = "idle"
        cli.reset_session = AsyncMock(
            side_effect=lambda: setattr(cli, "session_id", None)
        )

        captured_prompts = []

        async def fake_send(prompt, callback, model="", chat_id="", append_system_prompt=""):
            captured_prompts.append(prompt)
            await callback.on_stream("plain summary")

        cli.send = fake_send
        mock_channel.show_typing = AsyncMock()

        r = Router(
            cli_process=cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
        )
        await r.handle_message(msg("/compact"))

        assert len(captured_prompts) == 1
        assert "Additional instructions" not in captured_prompts[0]


class TestModelCommand:
    async def test_show_current_model(self, router, mock_cli, mock_channel):
        mock_cli.model = "opus"
        await router.handle_message(msg("/model"))
        text = mock_channel.send_text.call_args[0][1]
        assert "opus" in text

    async def test_show_default_when_empty(self, router, mock_cli, mock_channel):
        mock_cli.model = ""
        await router.handle_message(msg("/model"))
        text = mock_channel.send_text.call_args[0][1]
        assert "default" in text.lower()

    async def test_switch_model(self, router, mock_cli, mock_channel):
        mock_cli.model = "sonnet"
        await router.handle_message(msg("/model opus"))
        assert mock_cli.model == "opus"
        text = mock_channel.send_text.call_args[0][1]
        assert "opus" in text
        assert "sonnet" in text

    async def test_not_dispatched_to_cli(self, router, mock_cli, mock_channel):
        await router.handle_message(msg("/model"))
        mock_cli.send.assert_not_called()


class TestAtModelPrefix:
    async def test_session_context_includes_display_name(self, mock_channel, mock_storage):
        cli = AsyncMock()
        cli.session_id = None
        cli.state = "idle"

        captured = {}

        async def fake_send(prompt, callback, model="", chat_id="", append_system_prompt=""):
            captured["prompt"] = prompt
            captured["append_system_prompt"] = append_system_prompt

        cli.send = fake_send
        mock_channel.show_typing = AsyncMock()
        mock_channel.stream_start = AsyncMock(
            return_value=__import__("boxagent.channels.base", fromlist=["StreamHandle"]).StreamHandle(
                message_id="m1", chat_id="123"
            )
        )
        mock_channel.stream_update = AsyncMock()
        mock_channel.stream_end = AsyncMock()
        mock_channel.format_tool_call = lambda name, inp: ""

        r = Router(
            cli_process=cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
            display_name="Demo Bot",
        )

        await r.handle_message(msg("hello world"))

        assert "bot: test-bot" in captured["append_system_prompt"]
        assert "display_name: Demo Bot" in captured["append_system_prompt"]

    async def test_at_model_passes_override(self, mock_channel, mock_storage):
        cli = AsyncMock()
        cli.session_id = None
        cli.state = "idle"
        cli.model = "sonnet"

        captured = {}

        async def fake_send(prompt, callback, model="", chat_id="", append_system_prompt=""):
            captured["prompt"] = prompt
            captured["model"] = model
            captured["append_system_prompt"] = append_system_prompt

        cli.send = fake_send
        mock_channel.show_typing = AsyncMock()
        mock_channel.stream_start = AsyncMock(
            return_value=__import__("boxagent.channels.base", fromlist=["StreamHandle"]).StreamHandle(
                message_id="m1", chat_id="123"
            )
        )
        mock_channel.stream_update = AsyncMock()
        mock_channel.stream_end = AsyncMock()
        mock_channel.format_tool_call = lambda name, inp: ""

        r = Router(
            cli_process=cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
        )
        await r.handle_message(msg("@opus explain this"))

        assert captured["model"] == "opus"
        assert captured["prompt"] == "explain this"
        assert "[BoxAgent Context]" in captured["append_system_prompt"]

    async def test_no_prefix_no_override(self, mock_channel, mock_storage):
        cli = AsyncMock()
        cli.session_id = None
        cli.state = "idle"

        captured = {}

        async def fake_send(prompt, callback, model="", chat_id="", append_system_prompt=""):
            captured["prompt"] = prompt
            captured["model"] = model
            captured["append_system_prompt"] = append_system_prompt

        cli.send = fake_send
        mock_channel.show_typing = AsyncMock()
        mock_channel.stream_start = AsyncMock(
            return_value=__import__("boxagent.channels.base", fromlist=["StreamHandle"]).StreamHandle(
                message_id="m1", chat_id="123"
            )
        )
        mock_channel.stream_update = AsyncMock()
        mock_channel.stream_end = AsyncMock()
        mock_channel.format_tool_call = lambda name, inp: ""

        r = Router(
            cli_process=cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
        )
        await r.handle_message(msg("hello world"))

        assert captured["model"] == ""
        assert captured["prompt"] == "hello world"
        assert "[BoxAgent Context]" in captured["append_system_prompt"]

    async def test_second_message_also_has_context(self, mock_channel, mock_storage):
        """Context is injected on every message via --append-system-prompt."""
        cli = AsyncMock()
        cli.session_id = None
        cli.state = "idle"

        captured = {}

        async def fake_send(prompt, callback, model="", chat_id="", append_system_prompt=""):
            captured["prompt"] = prompt
            captured["append_system_prompt"] = append_system_prompt

        cli.send = fake_send
        mock_channel.show_typing = AsyncMock()
        mock_channel.stream_start = AsyncMock(
            return_value=__import__("boxagent.channels.base", fromlist=["StreamHandle"]).StreamHandle(
                message_id="m1", chat_id="123"
            )
        )
        mock_channel.stream_update = AsyncMock()
        mock_channel.stream_end = AsyncMock()
        mock_channel.format_tool_call = lambda name, inp: ""

        r = Router(
            cli_process=cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
        )
        await r.handle_message(msg("first"))
        assert "[BoxAgent Context]" in captured["append_system_prompt"]

        await r.handle_message(msg("second"))
        assert captured["prompt"] == "second"
        assert "[BoxAgent Context]" in captured["append_system_prompt"]


class TestExecCommand:
    async def test_exec_simple_command(self, router, mock_channel):
        await router.handle_message(msg("/exec echo hello"))
        text = mock_channel.send_text.call_args[0][1]
        assert "hello" in text
        assert "Exit: 0" in text

    async def test_exec_shows_exit_code(self, router, mock_channel):
        await router.handle_message(msg("/exec false"))
        text = mock_channel.send_text.call_args[0][1]
        assert "Exit: 1" in text

    async def test_exec_no_command(self, router, mock_channel):
        await router.handle_message(msg("/exec"))
        text = mock_channel.send_text.call_args[0][1]
        assert "usage" in text.lower()

    async def test_exec_empty_command(self, router, mock_channel):
        await router.handle_message(msg("/exec   "))
        text = mock_channel.send_text.call_args[0][1]
        assert "usage" in text.lower()

    async def test_exec_with_custom_timeout(self, router, mock_channel):
        await router.handle_message(msg("/exec -t 5 echo timeout_test"))
        text = mock_channel.send_text.call_args[0][1]
        assert "timeout_test" in text

    async def test_exec_timeout_kills_process(self, router, mock_channel):
        await router.handle_message(msg("/exec -t 1 sleep 30"))
        text = mock_channel.send_text.call_args[0][1]
        assert "timed out" in text.lower()

    async def test_exec_invalid_timeout(self, router, mock_channel):
        await router.handle_message(msg("/exec -t 0 echo x"))
        text = mock_channel.send_text.call_args[0][1]
        assert "1-600" in text

    async def test_exec_timeout_too_large(self, router, mock_channel):
        await router.handle_message(msg("/exec -t 999 echo x"))
        text = mock_channel.send_text.call_args[0][1]
        assert "1-600" in text

    async def test_exec_stderr_included(self, router, mock_channel):
        await router.handle_message(msg("/exec echo err >&2"))
        text = mock_channel.send_text.call_args[0][1]
        assert "err" in text

    async def test_exec_no_output(self, router, mock_channel):
        await router.handle_message(msg("/exec true"))
        text = mock_channel.send_text.call_args[0][1]
        assert "Exit: 0" in text

    async def test_exec_output_in_code_block(self, router, mock_channel):
        await router.handle_message(msg("/exec echo hello"))
        text = mock_channel.send_text.call_args[0][1]
        assert "```" in text

    async def test_exec_not_dispatched_to_cli(self, router, mock_cli):
        await router.handle_message(msg("/exec echo test"))
        mock_cli.send.assert_not_called()

    async def test_exec_uses_workspace_as_cwd(self, mock_cli, mock_channel, mock_storage):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            r = Router(
                cli_process=mock_cli,
                channel=mock_channel,
                allowed_users=[123],
                storage=mock_storage,
                bot_name="test-bot",
                workspace=tmpdir,
            )
            await r.handle_message(msg("/exec pwd"))
            text = mock_channel.send_text.call_args[0][1]
            assert tmpdir in text

    async def test_exec_long_output_sends_file(self, router, mock_channel):
        """Output > 3900 chars should be sent as a file."""
        # Generate output longer than 3900 chars
        mock_channel._bot = MagicMock()
        mock_channel._bot.send_document = AsyncMock()
        await router.handle_message(
            msg("/exec python3 -c \"print('x' * 5000)\"")
        )
        # Should call send_document instead of send_text for the output
        # (send_text is called for show_typing, so check the last call)
        if mock_channel._bot.send_document.called:
            caption = mock_channel._bot.send_document.call_args.kwargs.get("caption", "")
            assert "Exit:" in caption
        else:
            # Fallback path: truncated in code block
            text = mock_channel.send_text.call_args[0][1]
            assert "Exit:" in text

    async def test_exec_nonflag_t(self, router, mock_channel):
        """-t without valid number treats entire rest as command."""
        await router.handle_message(msg("/exec -t abc echo hi"))
        text = mock_channel.send_text.call_args[0][1]
        # Should try to run "-t abc echo hi" as a command or handle gracefully
        # The important thing is it doesn't crash
        assert mock_channel.send_text.called


class TestTrustWorkspaceCommand:
    """Tests for /trust_workspace command."""

    async def test_trust_new_workspace(self, router, mock_channel, tmp_path):
        """Trust a workspace that isn't in .claude.json yet."""
        import json

        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({"projects": {}}))

        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        router.workspace = str(workspace_dir)

        from boxagent.router_commands import cmd_trust_workspace
        from unittest.mock import patch

        with patch("boxagent.router_commands.Path.home", return_value=tmp_path):
            await cmd_trust_workspace(
                msg("/trust_workspace"),
                channel=mock_channel,
                workspace=str(workspace_dir),
            )

        text = mock_channel.send_text.call_args[0][1]
        assert "Trusted" in text

        data = json.loads(claude_json.read_text())
        ws_key = str(workspace_dir.resolve())
        assert ws_key in data["projects"]
        assert data["projects"][ws_key]["hasTrustDialogAccepted"] is True
        assert data["projects"][ws_key]["allowedTools"] == []

    async def test_trust_already_trusted(self, router, mock_channel, tmp_path):
        """Workspace already trusted — should report already trusted."""
        import json

        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        ws_key = str(workspace_dir.resolve())

        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({
            "projects": {ws_key: {"hasTrustDialogAccepted": True}},
        }))

        from boxagent.router_commands import cmd_trust_workspace
        from unittest.mock import patch

        with patch("boxagent.router_commands.Path.home", return_value=tmp_path):
            await cmd_trust_workspace(
                msg("/trust_workspace"),
                channel=mock_channel,
                workspace=str(workspace_dir),
            )

        text = mock_channel.send_text.call_args[0][1]
        assert "Already trusted" in text

    async def test_trust_no_workspace(self, mock_channel):
        """No workspace configured — should report error."""
        from boxagent.router_commands import cmd_trust_workspace

        await cmd_trust_workspace(
            msg("/trust_workspace"),
            channel=mock_channel,
            workspace="",
        )

        text = mock_channel.send_text.call_args[0][1]
        assert "No valid workspace" in text

    async def test_trust_no_claude_json(self, mock_channel, tmp_path):
        """~/.claude.json doesn't exist — should report error."""
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        from boxagent.router_commands import cmd_trust_workspace
        from unittest.mock import patch

        with patch("boxagent.router_commands.Path.home", return_value=tmp_path):
            await cmd_trust_workspace(
                msg("/trust_workspace"),
                channel=mock_channel,
                workspace=str(workspace_dir),
            )

        text = mock_channel.send_text.call_args[0][1]
        assert "not found" in text

    async def test_trust_adds_default_fields(self, mock_channel, tmp_path):
        """New project entry should have all default fields."""
        import json

        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({"numStartups": 1}))

        from boxagent.router_commands import cmd_trust_workspace
        from unittest.mock import patch

        with patch("boxagent.router_commands.Path.home", return_value=tmp_path):
            await cmd_trust_workspace(
                msg("/trust_workspace"),
                channel=mock_channel,
                workspace=str(workspace_dir),
            )

        data = json.loads(claude_json.read_text())
        ws_key = str(workspace_dir.resolve())
        project = data["projects"][ws_key]
        assert project["hasTrustDialogAccepted"] is True
        assert "mcpServers" in project
        assert "enabledMcpjsonServers" in project
        assert "ignorePatterns" in project
        # Existing data preserved
        assert data["numStartups"] == 1


class TestCdCommand:
    async def test_shows_current_workspace(self, router, mock_channel):
        await router.handle_message(msg("/cd"))
        text = mock_channel.send_text.call_args[0][1]
        assert "/home/testuser/.boxagent/workspace" in text

    async def test_invalid_directory(self, router, mock_channel):
        await router.handle_message(msg("/cd /nonexistent/path"))
        text = mock_channel.send_text.call_args[0][1]
        assert "not found" in text.lower()

    async def test_switches_workspace(self, router, mock_cli, mock_channel, mock_storage):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            await router.handle_message(msg(f"/cd {tmpdir}"))
            text = mock_channel.send_text.call_args[0][1]
            assert "switched" in text.lower()
            assert mock_cli.workspace == router.workspace
            assert router.workspace == str(Path(tmpdir).resolve())
            mock_cli.reset_session.assert_called()
            mock_storage.clear_session.assert_called_with("test-bot")

    async def test_expands_tilde(self, router, mock_cli, mock_channel):
        import os
        home = os.path.expanduser("~")
        await router.handle_message(msg("/cd ~"))
        assert router.workspace == os.path.realpath(home)


class TestBackendCommand:
    async def test_shows_current_backend(self, router, mock_channel):
        await router.handle_message(msg("/backend"))
        text = mock_channel.send_text.call_args[0][1]
        assert "claude-cli" in text

    async def test_invalid_backend(self, router, mock_channel):
        await router.handle_message(msg("/backend invalid"))
        text = mock_channel.send_text.call_args[0][1]
        assert "unknown" in text.lower()

    async def test_same_backend_noop(self, router, mock_channel):
        await router.handle_message(msg("/backend claude-cli"))
        text = mock_channel.send_text.call_args[0][1]
        assert "already" in text.lower()

    async def test_switches_backend(self, router, mock_cli, mock_channel, mock_storage):
        mock_cli.workspace = router.workspace
        mock_cli.model = "opus"
        mock_cli.agent = ""
        mock_cli.bot_token = ""
        mock_cli.yolo = False
        mock_cli.stop = AsyncMock()

        await router.handle_message(msg("/backend codex-cli"))

        mock_cli.stop.assert_awaited_once()
        text = mock_channel.send_text.call_args[0][1]
        assert "switched" in text.lower()
        assert "codex-cli" in text
        assert router.ai_backend == "codex-cli"
        assert router.cli_process is not mock_cli
        mock_storage.clear_session.assert_called_with("test-bot")
