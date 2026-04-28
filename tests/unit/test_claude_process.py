"""Unit tests for ClaudeProcess — mock subprocess, test stream-json parsing."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from boxagent.agent_env import AgentEnv


def make_stream_lines(*events: dict) -> bytes:
    """Create NDJSON byte stream from event dicts."""
    return b"\n".join(json.dumps(e).encode() for e in events) + b"\n"


def text_delta_event(text: str, index: int = 0) -> dict:
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "text_delta", "text": text},
    }


def tool_use_start_event(name: str, tool_id: str = "tool_1", index: int = 1) -> dict:
    return {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}},
    }


def input_json_delta_event(partial_json: str, index: int = 1) -> dict:
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "input_json_delta", "partial_json": partial_json},
    }


def content_block_stop_event(index: int = 0) -> dict:
    return {"type": "content_block_stop", "index": index}


def result_event(session_id: str = "sess_123", cost: float = 0.01) -> dict:
    return {
        "type": "result",
        "session_id": session_id,
        "cost_usd": cost,
        "duration_ms": 1000,
    }


def error_result_event(
    session_id: str = "sess_error",
    *errors: str,
    subtype: str = "error_during_execution",
) -> dict:
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": True,
        "session_id": session_id,
        "errors": list(errors),
    }

from tests.unit.helpers import FakeProcess


@pytest.fixture
def callback():
    """Mock AgentCallback."""
    cb = AsyncMock()
    cb.on_stream = AsyncMock()
    cb.on_tool_call = AsyncMock()
    cb.on_error = AsyncMock()
    cb.on_file = AsyncMock()
    cb.on_image = AsyncMock()
    return cb


@pytest.fixture
def make_cli_process():
    """Factory for ClaudeProcess instances."""
    from boxagent.agent.claude_process import ClaudeProcess

    def _make(workspace: str = "/tmp/test"):
        proc = ClaudeProcess(workspace=workspace)
        return proc

    return _make


class TestStreamJsonParsing:
    """Test parsing of stream-json events from stdout."""

    async def test_text_delta_calls_on_stream(self, make_cli_process, callback):
        """content_block_delta with text_delta → callback.on_stream()."""
        events = [text_delta_event("Hello"), text_delta_event(" world"), result_event()]
        fake_proc = FakeProcess(make_stream_lines(*events))

        cli = make_cli_process()
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await cli._execute_turn("test", callback)

        assert callback.on_stream.call_count == 2
        callback.on_stream.assert_any_call("Hello")
        callback.on_stream.assert_any_call(" world")

    async def test_tool_use_calls_on_tool_call(self, make_cli_process, callback):
        """content_block_start with tool_use → callback.on_tool_call()."""
        events = [
            tool_use_start_event("Bash"),
            input_json_delta_event('{"command":'),
            input_json_delta_event(' "ls"}'),
            content_block_stop_event(index=1),
            result_event(),
        ]
        fake_proc = FakeProcess(make_stream_lines(*events))

        cli = make_cli_process()
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await cli._execute_turn("test", callback)

        callback.on_tool_call.assert_called_once()
        call_args = callback.on_tool_call.call_args
        assert call_args[0][0] == "Bash"
        assert call_args[0][1] == {"command": "ls"}

    async def test_result_event_saves_session_id(self, make_cli_process, callback):
        """result event → session_id saved on ClaudeProcess."""
        events = [result_event(session_id="sess_abc")]
        fake_proc = FakeProcess(make_stream_lines(*events))

        cli = make_cli_process()
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await cli._execute_turn("test", callback)

        assert cli.session_id == "sess_abc"

    async def test_malformed_json_skipped(self, make_cli_process, callback):
        """Malformed JSON lines are silently skipped."""
        raw = b'not valid json\n' + json.dumps(result_event()).encode() + b'\n'
        fake_proc = FakeProcess(raw)

        cli = make_cli_process()
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await cli._execute_turn("test", callback)

        # Should not raise, and result event still processed
        assert cli.session_id == "sess_123"
        callback.on_error.assert_not_called()

    async def test_tool_input_accumulation_invalid_json(self, make_cli_process, callback):
        """If accumulated tool input is not valid JSON, pass {} instead."""
        events = [
            tool_use_start_event("Bash"),
            input_json_delta_event("{broken"),
            content_block_stop_event(index=1),
            result_event(),
        ]
        fake_proc = FakeProcess(make_stream_lines(*events))

        cli = make_cli_process()
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await cli._execute_turn("test", callback)

        callback.on_tool_call.assert_called_once()
        assert callback.on_tool_call.call_args[0][1] == {}

    async def test_nonzero_exit_calls_on_error(self, make_cli_process, callback):
        """Subprocess non-zero exit (not cancel) → callback.on_error()."""
        events = [result_event()]
        fake_proc = FakeProcess(make_stream_lines(*events), returncode=1)

        cli = make_cli_process()
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await cli._execute_turn("test", callback)

        callback.on_error.assert_called_once()
        assert "exit code 1" in callback.on_error.call_args[0][0].lower()

    async def test_structured_result_error_is_included_in_error_message(
        self, make_cli_process, callback
    ):
        events = [
            error_result_event(
                "sess_new",
                "No conversation found with session ID: stale_session",
            )
        ]
        fake_proc = FakeProcess(make_stream_lines(*events), returncode=1)

        cli = make_cli_process()
        cli.session_id = "stale_session"
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await cli._execute_turn("test", callback)

        callback.on_error.assert_called_once()
        error_text = callback.on_error.call_args[0][0]
        assert "exit code 1" in error_text.lower()
        assert "No conversation found with session ID: stale_session" in error_text
        assert cli.session_id == "stale_session"
        assert cli.last_turn_failed is True

    async def test_returncode_checked_after_wait(self, make_cli_process, callback):
        """After process.wait(), returncode is checked for errors."""
        events = [text_delta_event("ok"), result_event()]
        fake_proc = FakeProcess(make_stream_lines(*events), returncode=0)

        cli = make_cli_process()
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await cli._execute_turn("test", callback)

        # No error on exit code 0
        callback.on_error.assert_not_called()


    async def test_assistant_event_text_calls_on_stream(self, make_cli_process, callback):
        """CLI 'assistant' message with text content → callback.on_stream()."""
        events = [
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Hello world"}],
                },
                "session_id": "sess_456",
            },
            result_event(session_id="sess_456"),
        ]
        fake_proc = FakeProcess(make_stream_lines(*events))

        cli = make_cli_process()
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await cli._execute_turn("test", callback)

        callback.on_stream.assert_called_once_with("Hello world")
        assert cli.session_id == "sess_456"

    async def test_assistant_event_tool_use_calls_on_tool_call(self, make_cli_process, callback):
        """CLI 'assistant' message with tool_use content → callback.on_tool_call()."""
        events = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                    ],
                },
                "session_id": "sess_789",
            },
            result_event(session_id="sess_789"),
        ]
        fake_proc = FakeProcess(make_stream_lines(*events))

        cli = make_cli_process()
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await cli._execute_turn("test", callback)

        callback.on_tool_call.assert_called_once()
        call_args = callback.on_tool_call.call_args
        assert call_args[0][0] == "Bash"
        assert call_args[0][1] == {"command": "ls"}


class TestCancel:
    """Test cancel() behavior."""

    async def test_cancel_terminates_then_kills(self, make_cli_process):
        """cancel() calls terminate; on timeout, calls kill. State → idle."""
        cli = make_cli_process()
        fake_proc = FakeProcess(b"", returncode=0)
        fake_proc.returncode = None  # process still running

        async def never_exit():
            await asyncio.sleep(100)

        fake_proc.wait = never_exit
        cli._process = fake_proc
        cli._cancelled = False
        cli.state = "busy"

        await cli.cancel()

        assert fake_proc._terminated
        assert fake_proc._killed  # kill called after 3s timeout
        assert cli._cancelled
        assert cli.state == "idle"
        assert cli._idle_event.is_set()

    async def test_cancel_does_not_trigger_on_error(self, make_cli_process, callback):
        """Cancelled process (non-zero exit) should NOT call on_error."""
        # Simulate a process that exits with code -15 (SIGTERM) after cancel
        events = [text_delta_event("partial"), result_event()]
        fake_proc = FakeProcess(make_stream_lines(*events), returncode=-15)

        cli = make_cli_process()
        cli._cancelled = True  # cancel() was called before _execute_turn checks returncode

        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await cli._execute_turn("test", callback)

        # on_error should NOT be called because _cancelled is True
        callback.on_error.assert_not_called()


class TestMessageQueue:
    """Test serial message processing."""

    async def test_messages_processed_serially(self, make_cli_process, callback):
        """Messages 1 and 2: all callbacks for msg1 complete before msg2 starts."""
        call_order = []

        async def track_stream(text):
            call_order.append(("stream", text))

        callback.on_stream = track_stream

        events1 = [text_delta_event("msg1_a"), text_delta_event("msg1_b"), result_event("s1")]
        events2 = [text_delta_event("msg2_a"), result_event("s2")]

        call_count = 0

        async def fake_exec(*args, **kwargs):
            nonlocal call_count
            if call_count == 0:
                call_count += 1
                return FakeProcess(make_stream_lines(*events1))
            else:
                return FakeProcess(make_stream_lines(*events2))

        cli = make_cli_process()
        cli.start()

        try:
            with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
                await cli.send("first", callback)
                await cli.send("second", callback)
                # Wait for both to complete
                await cli.wait_idle()

            # Verify ordering: all msg1 callbacks before any msg2
            msg1_indices = [i for i, (t, v) in enumerate(call_order) if "msg1" in v]
            msg2_indices = [i for i, (t, v) in enumerate(call_order) if "msg2" in v]
            if msg1_indices and msg2_indices:
                assert max(msg1_indices) < min(msg2_indices)
        finally:
            await cli.stop()


class TestMCPConfig:
    """Test MCP server config generation."""

    async def test_mcp_config_added_when_bot_token_and_chat_id(self, callback):
        """--mcp-config is added to args when telegram_token and chat_id are set."""
        from boxagent.agent.claude_process import ClaudeProcess
        from boxagent.agent_env import AgentEnv

        events = [result_event()]
        fake_proc = FakeProcess(make_stream_lines(*events))

        cli = ClaudeProcess(workspace="/tmp/test")
        env = AgentEnv(telegram_token="test-token")

        captured_args = []

        async def capture_exec(*args, **kwargs):
            captured_args.extend(args)
            return fake_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            await cli._execute_turn("test", callback, chat_id="12345", env=env)

        args_str = " ".join(str(a) for a in captured_args)
        assert "--mcp-config" in args_str
        assert "test-token" in args_str
        assert "12345" in args_str
        assert "boxagent-telegram" in args_str

    async def test_no_telegram_mcp_without_bot_token(self, callback):
        """boxagent-telegram MCP is NOT added when telegram_token is empty,
        but boxagent MCP is still present."""
        from boxagent.agent.claude_process import ClaudeProcess

        events = [result_event()]
        fake_proc = FakeProcess(make_stream_lines(*events))

        cli = ClaudeProcess(workspace="/tmp/test")
        env = AgentEnv()  # no telegram_token

        captured_args = []

        async def capture_exec(*args, **kwargs):
            captured_args.extend(args)
            return fake_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            await cli._execute_turn("test", callback, chat_id="12345", env=env)

        args_str = " ".join(str(a) for a in captured_args)
        assert "--mcp-config" in args_str
        assert "boxagent-telegram" not in args_str
        assert '"boxagent"' in args_str

    async def test_no_mcp_config_without_chat_id(self, callback):
        """--mcp-config is NOT added when chat_id is empty."""
        from boxagent.agent.claude_process import ClaudeProcess

        events = [result_event()]
        fake_proc = FakeProcess(make_stream_lines(*events))

        cli = ClaudeProcess(workspace="/tmp/test")
        env = AgentEnv(telegram_token="test-token")

        captured_args = []

        async def capture_exec(*args, **kwargs):
            captured_args.extend(args)
            return fake_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            await cli._execute_turn("test", callback, env=env)

        args_str = " ".join(str(a) for a in captured_args)
        assert "--mcp-config" not in args_str

    async def test_mcp_config_contains_both_servers(self, callback):
        """MCP config has both boxagent and boxagent-telegram servers."""
        from boxagent.agent.claude_process import ClaudeProcess
        from boxagent.agent_env import AgentEnv

        events = [result_event()]
        fake_proc = FakeProcess(make_stream_lines(*events))

        cli = ClaudeProcess(workspace="/tmp/test")
        env = AgentEnv(telegram_token="tok")

        captured_args = []

        async def capture_exec(*args, **kwargs):
            captured_args.extend(args)
            return fake_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            await cli._execute_turn("test", callback, chat_id="999", env=env)

        # Find the --mcp-config value
        args_list = list(captured_args)
        idx = args_list.index("--mcp-config")
        config_json = json.loads(args_list[idx + 1])

        # boxagent server (schedule/session/workgroup)
        agent_server = config_json["mcpServers"]["boxagent"]
        assert "mcp_server.py" in agent_server["args"][0]

        # boxagent-telegram server (media tools)
        tg_server = config_json["mcpServers"]["boxagent-telegram"]
        assert "mcp_telegram.py" in tg_server["args"][0]
        assert tg_server["env"]["BOXAGENT_BOT_TOKEN"] == "tok"
        assert tg_server["env"]["BOXAGENT_CHAT_ID"] == "999"


class TestStop:
    """Test stop() / lifecycle."""

    async def test_stop_sets_state_dead(self, make_cli_process):
        """stop() cancels queue task and sets state to dead."""
        cli = make_cli_process()
        cli.start()
        await cli.stop()
        assert cli.state == "dead"
