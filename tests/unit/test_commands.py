"""Unit tests for system commands (/status, /new, /cancel, /resume)."""

import tempfile
import time
from pathlib import Path

import pytest
from unittest.mock import MagicMock

from boxagent.transports.base import IncomingMessage
from boxagent.router import Router
from boxagent.testing.mocks import MockBackend, MockChannel


@pytest.fixture
def mock_cli():
    return MockBackend(session_id="sess_123", supports_session_persistence=True)


@pytest.fixture
def mock_channel():
    return MockChannel()


@pytest.fixture
def mock_storage():
    chat_state = MagicMock()
    chat_state.clear_session = MagicMock()
    chat_state.save_session = MagicMock()
    chat_state.list_session_history = MagicMock(return_value=[])
    return chat_state


@pytest.fixture
def router(mock_cli, mock_channel, mock_storage):
    r = Router(
        backend=mock_cli,
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


def _last_text(channel):
    """Last text body sent on the channel (chat_id, text) → text."""
    return channel.sent_texts[-1][1]


class TestStatusCommand:
    async def test_returns_state_and_session(self, router, mock_channel):
        await router.handle_message(msg("/status"))
        text = _last_text(mock_channel)
        assert "idle" in text.lower()
        assert "sess_123" in text

    async def test_shows_uptime(self, router, mock_channel):
        await router.handle_message(msg("/status"))
        text = _last_text(mock_channel)
        assert "uptime" in text.lower() or "1h" in text.lower() or "3600" in text


class TestNewCommand:
    async def test_clears_session(self, router, mock_cli, mock_storage):
        await router.handle_message(msg("/new"))
        if router.pool:
            pass  # pool.clear_session tested via pool tests
        else:
            assert mock_cli.reset_session_count == 1
            assert mock_cli.session_id is None
        mock_storage.clear_session.assert_called_once_with("test-bot", chat_id="123")

    async def test_clears_pending_compact_summary(self, router):
        router._compact_summaries["123"] = "stale summary"

        await router.handle_message(msg("/new"))

        assert router._compact_summaries.get("123", "") == ""

    async def test_sends_confirmation(self, router, mock_channel):
        await router.handle_message(msg("/new"))
        text = _last_text(mock_channel)
        assert "new" in text.lower() or "fresh" in text.lower()


class TestCancelCommand:
    async def test_calls_cancel(self, router, mock_cli):
        await router.handle_message(msg("/cancel"))
        assert mock_cli.cancel_count == 1

    async def test_sends_confirmation(self, router, mock_channel):
        await router.handle_message(msg("/cancel"))
        assert "cancel" in _last_text(mock_channel).lower()


class TestResumeCommand:
    async def test_lists_native_session_history(
        self, router, mock_channel, mock_storage
    ):
        mock_storage.list_session_history.return_value = [
            {"session_id": "sess_old", "saved_at": 1_710_000_000, "backend": "claude-cli"}
        ]

        await router.handle_message(msg("/resume"))

        # MockChannel doesn't expose send_text_with_inline_keyboard, so the
        # resume command falls through to send_text.
        text = _last_text(mock_channel)
        assert "sess_old" in text


class TestStartCommand:
    async def test_sends_welcome(self, router, mock_channel):
        await router.handle_message(msg("/start"))
        assert len(mock_channel.sent_texts) == 1
        assert "welcome" in _last_text(mock_channel).lower()

    async def test_includes_bot_name(self, router, mock_channel):
        await router.handle_message(msg("/start"))
        assert "test-bot" in _last_text(mock_channel)

    async def test_not_dispatched_to_cli(self, router, mock_cli):
        await router.handle_message(msg("/start"))
        assert mock_cli.sends == []


class TestHelpCommand:
    async def test_sends_help(self, router, mock_channel):
        await router.handle_message(msg("/help"))
        assert len(mock_channel.sent_texts) == 1
        text = _last_text(mock_channel)
        assert "/new" in text
        assert "/status" in text
        assert "/cancel" in text
        assert "/help" in text

    async def test_not_dispatched_to_cli(self, router, mock_cli):
        await router.handle_message(msg("/help"))
        assert mock_cli.sends == []


class TestVerboseCommand:
    async def test_cycles_silent_to_summary(self, router, mock_channel):
        mock_channel.tool_calls_display = "silent"
        await router.handle_message(msg("/verbose"))
        assert mock_channel.tool_calls_display == "summary"
        assert "summary" in _last_text(mock_channel).lower()

    async def test_cycles_summary_to_detailed(self, router, mock_channel):
        mock_channel.tool_calls_display = "summary"
        await router.handle_message(msg("/verbose"))
        assert mock_channel.tool_calls_display == "detailed"
        assert "detailed" in _last_text(mock_channel).lower()

    async def test_cycles_detailed_to_silent(self, router, mock_channel):
        mock_channel.tool_calls_display = "detailed"
        await router.handle_message(msg("/verbose"))
        assert mock_channel.tool_calls_display == "silent"
        assert "silent" in _last_text(mock_channel).lower()

    async def test_full_cycle(self, router, mock_channel):
        """Three /verbose calls cycle through all modes."""
        mock_channel.tool_calls_display = "summary"
        await router.handle_message(msg("/verbose"))
        assert mock_channel.tool_calls_display == "detailed"
        await router.handle_message(msg("/verbose"))
        assert mock_channel.tool_calls_display == "silent"
        await router.handle_message(msg("/verbose"))
        assert mock_channel.tool_calls_display == "summary"

    async def test_not_dispatched_to_cli(self, router, mock_cli, mock_channel):
        mock_channel.tool_calls_display = "summary"
        await router.handle_message(msg("/verbose"))
        assert mock_cli.sends == []


class TestSyncSkillsCommand:
    async def test_syncs_and_reports(self, mock_cli, mock_channel, mock_storage):
        with tempfile.TemporaryDirectory() as work_dir, \
             tempfile.TemporaryDirectory() as skill_src:
            (Path(skill_src) / "my-skill").mkdir()
            (Path(skill_src) / "my-skill" / "SKILL.md").write_text("# Skill")
            (Path(skill_src) / "other-skill").mkdir()

            r = Router(
                backend=mock_cli,
                channel=mock_channel,
                allowed_users=[123],
                storage=mock_storage,
                bot_name="test-bot",
                workspace=work_dir,
                extra_skill_dirs=[skill_src],
            )
            await r.handle_message(msg("/sync_skills"))
            text = _last_text(mock_channel)
            assert "2" in text
            assert "my-skill" in text
            assert "other-skill" in text

            skills_dir = Path(work_dir) / ".claude" / "skills"
            assert (skills_dir / "my-skill").is_symlink()
            assert (skills_dir / "other-skill").is_symlink()

    async def test_empty_extra_skill_dirs(self, mock_cli, mock_channel, mock_storage):
        r = Router(
            backend=mock_cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
            workspace="/tmp",
            extra_skill_dirs=[],
        )
        await r.handle_message(msg("/sync_skills"))
        text = _last_text(mock_channel)
        assert "no skills" in text.lower() or "empty" in text.lower()

    async def test_not_dispatched_to_cli(self, mock_cli, mock_channel, mock_storage):
        r = Router(
            backend=mock_cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
        )
        await r.handle_message(msg("/sync_skills"))
        assert mock_cli.sends == []


class TestCompactCommand:
    async def test_no_session_returns_message(self, router, mock_cli, mock_channel):
        mock_cli.session_id = None
        await router.handle_message(msg("/compact"))
        assert "no active session" in _last_text(mock_channel).lower()

    async def test_compact_generates_summary_and_resets(self, mock_channel, mock_storage):
        cli = MockBackend(session_id="sess_123")
        cli.script(["- Discussed topic A\n- Decided B"])

        r = Router(
            backend=cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
        )
        await r.handle_message(msg("/compact"))

        assert cli.reset_session_count == 1
        assert cli.session_id is None
        mock_storage.clear_session.assert_called_once_with("test-bot", chat_id="123", preserve_chain=True)
        # Final summary lands either as a sent_text or as the closing
        # stream chunk depending on the channel path.
        final_text = (mock_channel.sent_texts[-1][1] if mock_channel.sent_texts else "") + \
            (mock_channel.streams[-1].chunks[-1] if mock_channel.streams else "")
        assert "topic A" in final_text
        assert "Decided B" in final_text

    async def test_compact_summary_injected_in_next_dispatch(self, mock_channel, mock_storage):
        cli = MockBackend(session_id="sess_123")
        cli.script(["summary text here"])

        r = Router(
            backend=cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
        )

        # Compact
        await r.handle_message(msg("/compact"))

        cli.sends.clear()
        cli.script(["next reply"])
        await r.handle_message(msg("hello"))

        assert len(cli.sends) == 1
        assert "summary text here" in cli.sends[-1].append_system_prompt
        assert "hello" in cli.sends[-1].message

    async def test_not_dispatched_to_cli_directly(self, router, mock_cli, mock_channel):
        mock_cli.session_id = None
        await router.handle_message(msg("/compact"))
        assert mock_cli.sends == []

    async def test_compact_with_user_hint(self, mock_channel, mock_storage):
        """User text after /compact is passed as additional instructions."""
        cli = MockBackend(session_id="sess_123")
        cli.script(["summary with focus"])

        r = Router(
            backend=cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
        )
        await r.handle_message(msg("/compact 帮我重点保存数据库相关的内容"))

        assert len(cli.sends) == 1
        assert "帮我重点保存数据库相关的内容" in cli.sends[-1].message
        assert "Additional instructions" in cli.sends[-1].message

    async def test_compact_without_hint_no_additional(self, mock_channel, mock_storage):
        """Plain /compact has no additional instructions."""
        cli = MockBackend(session_id="sess_123")
        cli.script(["plain summary"])

        r = Router(
            backend=cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
        )
        await r.handle_message(msg("/compact"))

        assert len(cli.sends) == 1
        assert "Additional instructions" not in cli.sends[-1].message


class TestModelCommand:
    async def test_show_current_model(self, router, mock_cli, mock_channel):
        mock_cli.model = "opus"
        await router.handle_message(msg("/model"))
        assert "opus" in _last_text(mock_channel)

    async def test_show_default_when_empty(self, router, mock_cli, mock_channel):
        mock_cli.model = ""
        await router.handle_message(msg("/model"))
        assert "default" in _last_text(mock_channel).lower()

    async def test_switch_model(self, router, mock_cli, mock_channel):
        mock_cli.model = "sonnet"
        await router.handle_message(msg("/model opus"))
        assert mock_cli.model == "opus"
        text = _last_text(mock_channel)
        assert "opus" in text
        assert "sonnet" in text

    async def test_not_dispatched_to_cli(self, router, mock_cli, mock_channel):
        await router.handle_message(msg("/model"))
        assert mock_cli.sends == []


class TestAtModelPrefix:
    async def test_session_context_includes_display_name(self, mock_channel, mock_storage):
        cli = MockBackend()
        r = Router(
            backend=cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
            display_name="Demo Bot",
        )

        await r.handle_message(msg("hello world"))

        send = cli.sends[-1]
        assert "bot: test-bot" in send.append_system_prompt
        assert "display_name: Demo Bot" in send.append_system_prompt

    async def test_at_model_passes_override(self, mock_channel, mock_storage):
        cli = MockBackend(model="sonnet")
        r = Router(
            backend=cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
        )
        await r.handle_message(msg("@opus explain this"))

        send = cli.sends[-1]
        assert send.model == "opus"
        assert send.message == "explain this"
        assert "[BoxAgent Context]" in send.append_system_prompt

    async def test_no_prefix_no_override(self, mock_channel, mock_storage):
        cli = MockBackend()
        r = Router(
            backend=cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
        )
        await r.handle_message(msg("hello world"))

        send = cli.sends[-1]
        assert send.model == ""
        assert send.message == "hello world"
        assert "[BoxAgent Context]" in send.append_system_prompt

    async def test_second_message_also_has_context(self, mock_channel, mock_storage):
        """Context is injected on every message via --append-system-prompt."""
        cli = MockBackend()
        r = Router(
            backend=cli,
            channel=mock_channel,
            allowed_users=[123],
            storage=mock_storage,
            bot_name="test-bot",
        )
        await r.handle_message(msg("first"))
        assert "[BoxAgent Context]" in cli.sends[-1].append_system_prompt

        await r.handle_message(msg("second"))
        assert cli.sends[-1].message == "second"
        assert "[BoxAgent Context]" in cli.sends[-1].append_system_prompt


class TestExecCommand:
    async def test_exec_simple_command(self, router, mock_channel):
        await router.handle_message(msg("/exec echo hello"))
        text = _last_text(mock_channel)
        assert "hello" in text
        assert "Exit: 0" in text

    async def test_exec_shows_exit_code(self, router, mock_channel):
        await router.handle_message(msg("/exec false"))
        assert "Exit: 1" in _last_text(mock_channel)

    async def test_exec_no_command(self, router, mock_channel):
        await router.handle_message(msg("/exec"))
        assert "usage" in _last_text(mock_channel).lower()

    async def test_exec_empty_command(self, router, mock_channel):
        await router.handle_message(msg("/exec   "))
        assert "usage" in _last_text(mock_channel).lower()

    async def test_exec_with_custom_timeout(self, router, mock_channel):
        await router.handle_message(msg("/exec -t 5 echo timeout_test"))
        assert "timeout_test" in _last_text(mock_channel)

    async def test_exec_timeout_kills_process(self, router, mock_channel):
        await router.handle_message(msg("/exec -t 1 sleep 30"))
        assert "timed out" in _last_text(mock_channel).lower()

    async def test_exec_invalid_timeout(self, router, mock_channel):
        await router.handle_message(msg("/exec -t 0 echo x"))
        assert "1-600" in _last_text(mock_channel)

    async def test_exec_timeout_too_large(self, router, mock_channel):
        await router.handle_message(msg("/exec -t 999 echo x"))
        assert "1-600" in _last_text(mock_channel)

    async def test_exec_stderr_included(self, router, mock_channel):
        await router.handle_message(msg("/exec echo err >&2"))
        assert "err" in _last_text(mock_channel)

    async def test_exec_no_output(self, router, mock_channel):
        await router.handle_message(msg("/exec true"))
        assert "Exit: 0" in _last_text(mock_channel)

    async def test_exec_output_in_code_block(self, router, mock_channel):
        await router.handle_message(msg("/exec echo hello"))
        assert "```" in _last_text(mock_channel)

    async def test_exec_not_dispatched_to_cli(self, router, mock_cli):
        await router.handle_message(msg("/exec echo test"))
        assert mock_cli.sends == []

    async def test_exec_uses_workspace_as_cwd(self, mock_cli, mock_channel, mock_storage):
        with tempfile.TemporaryDirectory() as tmpdir:
            r = Router(
                backend=mock_cli,
                channel=mock_channel,
                allowed_users=[123],
                storage=mock_storage,
                bot_name="test-bot",
                workspace=tmpdir,
            )
            await r.handle_message(msg("/exec pwd"))
            assert tmpdir in _last_text(mock_channel)

    async def test_exec_long_output_sends_file(self, router, mock_channel):
        """Output > 3900 chars should be sent as a file.

        TelegramChannel-only; MockChannel has no _bot, so the exec
        command falls back to sending a truncated code block via
        send_text. Verify either path produces the right exit marker.
        """
        await router.handle_message(
            msg("/exec python3 -c \"print('x' * 5000)\"")
        )
        assert "Exit:" in _last_text(mock_channel)

    async def test_exec_nonflag_t(self, router, mock_channel):
        """-t without valid number treats entire rest as command."""
        await router.handle_message(msg("/exec -t abc echo hi"))
        # Should try to run "-t abc echo hi" as a command or handle gracefully
        # The important thing is it doesn't crash
        assert mock_channel.sent_texts


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

        from boxagent.router.commands.workspace import cmd_trust_workspace
        from unittest.mock import patch

        with patch("boxagent.router.commands.workspace.Path.home", return_value=tmp_path):
            await cmd_trust_workspace(router, msg("/trust_workspace"), mock_channel)

        assert "Trusted" in _last_text(mock_channel)

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
        router.workspace = str(workspace_dir)

        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({
            "projects": {ws_key: {"hasTrustDialogAccepted": True}},
        }))

        from boxagent.router.commands.workspace import cmd_trust_workspace
        from unittest.mock import patch

        with patch("boxagent.router.commands.workspace.Path.home", return_value=tmp_path):
            await cmd_trust_workspace(router, msg("/trust_workspace"), mock_channel)

        assert "Already trusted" in _last_text(mock_channel)

    async def test_trust_no_workspace(self, router, mock_channel):
        """No workspace configured — should report error."""
        from boxagent.router.commands.workspace import cmd_trust_workspace

        router.workspace = ""
        await cmd_trust_workspace(router, msg("/trust_workspace"), mock_channel)

        assert "No valid workspace" in _last_text(mock_channel)

    async def test_trust_no_claude_json(self, router, mock_channel, tmp_path):
        """~/.claude.json doesn't exist — should report error."""
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        router.workspace = str(workspace_dir)

        from boxagent.router.commands.workspace import cmd_trust_workspace
        from unittest.mock import patch

        with patch("boxagent.router.commands.workspace.Path.home", return_value=tmp_path):
            await cmd_trust_workspace(router, msg("/trust_workspace"), mock_channel)

        assert "not found" in _last_text(mock_channel)

    async def test_trust_adds_default_fields(self, router, mock_channel, tmp_path):
        """New project entry should have all default fields."""
        import json

        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        router.workspace = str(workspace_dir)

        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({"numStartups": 1}))

        from boxagent.router.commands.workspace import cmd_trust_workspace
        from unittest.mock import patch

        with patch("boxagent.router.commands.workspace.Path.home", return_value=tmp_path):
            await cmd_trust_workspace(router, msg("/trust_workspace"), mock_channel)

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
        assert "/home/testuser/.boxagent/workspace" in _last_text(mock_channel)

    async def test_invalid_directory(self, router, mock_channel):
        await router.handle_message(msg("/cd /nonexistent/path"))
        assert "not found" in _last_text(mock_channel).lower()

    async def test_switches_workspace(self, router, mock_cli, mock_channel, mock_storage):
        with tempfile.TemporaryDirectory() as tmpdir:
            await router.handle_message(msg(f"/cd {tmpdir}"))
            assert "switched" in _last_text(mock_channel).lower()
            assert mock_cli.workspace == router.workspace
            assert router.workspace == str(Path(tmpdir).resolve())
            assert mock_cli.reset_session_count >= 1
            mock_storage.clear_session.assert_called_with("test-bot", chat_id="123")
        import os
        home = os.path.expanduser("~")
        await router.handle_message(msg("/cd ~"))
        assert router.workspace == os.path.realpath(home)


class TestBackendCommand:
    async def test_shows_current_backend(self, router, mock_channel):
        await router.handle_message(msg("/backend"))
        assert "claude-cli" in _last_text(mock_channel)

    async def test_invalid_backend(self, router, mock_channel):
        await router.handle_message(msg("/backend invalid"))
        assert "unknown" in _last_text(mock_channel).lower()

    async def test_same_backend_noop(self, router, mock_channel):
        await router.handle_message(msg("/backend claude-cli"))
        assert "already" in _last_text(mock_channel).lower()

    async def test_switches_backend(self, router, mock_cli, mock_channel, mock_storage):
        mock_cli.workspace = router.workspace
        mock_cli.model = "opus"
        mock_cli.agent = ""
        mock_cli.yolo = False

        await router.handle_message(msg("/backend codex-cli"))

        assert mock_cli.stopped is True
        text = _last_text(mock_channel)
        assert "switched" in text.lower()
        assert "codex-cli" in text
        assert router.ai_backend == "codex-cli"
        assert router.backend is not mock_cli
        mock_storage.clear_session.assert_called_with("test-bot", chat_id="123")
