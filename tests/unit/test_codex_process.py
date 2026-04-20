"""Unit tests for CodexProcess — mock subprocess, test JSONL parsing."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest


def make_jsonl(*events: dict) -> bytes:
    """Create JSONL byte stream from event dicts."""
    return b"\n".join(json.dumps(e).encode() for e in events) + b"\n"


def thread_started_event(thread_id: str = "tid-001") -> dict:
    return {"type": "thread.started", "thread_id": thread_id}


def turn_started_event() -> dict:
    return {"type": "turn.started"}


def agent_message_event(text: str, item_id: str = "item_0") -> dict:
    return {
        "type": "item.completed",
        "item": {"id": item_id, "type": "agent_message", "text": text},
    }


def command_started_event(command: str, item_id: str = "item_1") -> dict:
    return {
        "type": "item.started",
        "item": {
            "id": item_id,
            "type": "command_execution",
            "command": command,
            "aggregated_output": "",
            "exit_code": None,
            "status": "in_progress",
        },
    }


def command_completed_event(
    command: str, output: str = "", exit_code: int = 0, item_id: str = "item_1"
) -> dict:
    return {
        "type": "item.completed",
        "item": {
            "id": item_id,
            "type": "command_execution",
            "command": command,
            "aggregated_output": output,
            "exit_code": exit_code,
            "status": "completed",
        },
    }


def turn_completed_event(input_tokens: int = 100, output_tokens: int = 20) -> dict:
    return {
        "type": "turn.completed",
        "usage": {
            "input_tokens": input_tokens,
            "cached_input_tokens": 0,
            "output_tokens": output_tokens,
        },
    }

from tests.unit.helpers import FakeProcess


@pytest.fixture
def callback():
    cb = AsyncMock()
    cb.on_stream = AsyncMock()
    cb.on_tool_call = AsyncMock()
    cb.on_tool_update = AsyncMock()
    cb.on_error = AsyncMock()
    return cb


@pytest.fixture
def process_factory():
    """Return a factory that creates CodexProcess with workspace set."""
    from boxagent.agent.codex_process import CodexProcess

    def _make(**kwargs):
        defaults = {"workspace": "/tmp/test"}
        defaults.update(kwargs)
        p = CodexProcess(**defaults)
        p.start()
        return p

    return _make


# --- Basic text response ---


@pytest.mark.asyncio
async def test_simple_text_response(callback, process_factory):
    """Agent returns a simple text message."""
    data = make_jsonl(
        thread_started_event("tid-abc"),
        turn_started_event(),
        agent_message_event("hello world"),
        turn_completed_event(),
    )

    with patch("asyncio.create_subprocess_exec", return_value=FakeProcess(data)):
        proc = process_factory()
        await proc.send("hi", callback)
        await proc.stop()

    callback.on_stream.assert_called_once_with("hello world")
    assert proc.session_id == "tid-abc"


# --- Multi-message turn ---


@pytest.mark.asyncio
async def test_multi_message_turn(callback, process_factory):
    """Multiple agent messages in one turn."""
    data = make_jsonl(
        thread_started_event("tid-multi"),
        turn_started_event(),
        agent_message_event("first"),
        agent_message_event("second", item_id="item_2"),
        turn_completed_event(),
    )

    with patch("asyncio.create_subprocess_exec", return_value=FakeProcess(data)):
        proc = process_factory()
        await proc.send("test", callback)
        await proc.stop()

    assert callback.on_stream.call_count == 2
    callback.on_stream.assert_any_call("first")
    callback.on_stream.assert_any_call("second")


# --- Tool call (command execution) ---


@pytest.mark.asyncio
async def test_command_execution(callback, process_factory):
    """Command execution emits tool_update (started) and tool_call (completed)."""
    data = make_jsonl(
        thread_started_event(),
        turn_started_event(),
        agent_message_event("Let me check."),
        command_started_event("cat test.txt"),
        command_completed_event("cat test.txt", output="hello\n", exit_code=0),
        agent_message_event("It says hello.", item_id="item_2"),
        turn_completed_event(),
    )

    with patch("asyncio.create_subprocess_exec", return_value=FakeProcess(data)):
        proc = process_factory()
        await proc.send("read the file", callback)
        await proc.stop()

    # on_tool_update called for started
    callback.on_tool_update.assert_called_once()
    call_args = callback.on_tool_update.call_args
    assert call_args.kwargs.get("status") == "in_progress"

    # on_tool_call called for completed
    callback.on_tool_call.assert_called_once_with(
        "shell",
        {"command": "cat test.txt"},
        "exit=0\nhello\n",
    )

    # Two text messages streamed
    assert callback.on_stream.call_count == 2


# --- Session continuity (resume) ---


@pytest.mark.asyncio
async def test_session_resume_uses_thread_id(callback, process_factory):
    """After first turn, second turn should use resume subcommand."""
    first_data = make_jsonl(
        thread_started_event("tid-resume"),
        turn_started_event(),
        agent_message_event("first reply"),
        turn_completed_event(),
    )
    second_data = make_jsonl(
        thread_started_event("tid-resume"),
        turn_started_event(),
        agent_message_event("second reply"),
        turn_completed_event(),
    )

    call_count = 0

    async def fake_exec(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return FakeProcess(first_data)
        return FakeProcess(second_data)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec) as mock_exec:
        proc = process_factory()
        await proc.send("hello", callback)
        assert proc.session_id == "tid-resume"

        await proc.send("again", callback)
        await proc.stop()

    # Second call should have "resume" in args
    second_call_args = mock_exec.call_args_list[1][0]
    assert "resume" in second_call_args
    assert "tid-resume" in second_call_args


# --- Error handling ---


@pytest.mark.asyncio
async def test_nonzero_exit_reports_error(callback, process_factory):
    """Non-zero exit code should trigger on_error."""
    data = make_jsonl(thread_started_event())

    fake = FakeProcess(data, returncode=1)
    fake.stderr.read = AsyncMock(return_value=b"something went wrong")

    with patch("asyncio.create_subprocess_exec", return_value=fake):
        proc = process_factory()
        await proc.send("fail", callback)
        await proc.stop()

    callback.on_error.assert_called_once()
    error_msg = callback.on_error.call_args[0][0]
    assert "exit code 1" in error_msg
    assert "something went wrong" in error_msg


# --- Cancel ---


@pytest.mark.asyncio
async def test_cancel_terminates_process(process_factory):
    """Cancel should terminate the running subprocess."""
    from boxagent.agent.codex_process import CodexProcess

    proc = CodexProcess(workspace="/tmp/test")

    fake = FakeProcess(b"", returncode=-15)
    fake.returncode = None  # still running

    proc._process = fake
    proc.state = "busy"
    proc._idle_event.clear()

    await proc.cancel()

    assert fake._terminated
    assert proc.state == "idle"


# --- Stdin pipe mode (codex exec -) ---


@pytest.mark.asyncio
async def test_prompt_piped_via_stdin(callback, process_factory):
    """Prompt should be piped via stdin, args should end with '-' sentinel."""
    data = make_jsonl(
        thread_started_event("tid-stdin"),
        turn_started_event(),
        agent_message_event("got it"),
        turn_completed_event(),
    )

    with patch("asyncio.create_subprocess_exec", return_value=FakeProcess(data)) as mock_exec:
        proc = process_factory()
        await proc.send("hello world", callback)
        await proc.stop()

    # Args should end with "-", not the message text
    args = mock_exec.call_args[0]
    assert args[-1] == "-"
    assert "hello world" not in args

    # stdin should be PIPE, not DEVNULL
    kwargs = mock_exec.call_args[1]
    assert kwargs["stdin"] == asyncio.subprocess.PIPE

    callback.on_stream.assert_called_once_with("got it")


@pytest.mark.asyncio
async def test_resume_also_uses_stdin_pipe(callback, process_factory):
    """Resume turns should also pipe the prompt via stdin."""
    data = make_jsonl(
        thread_started_event("tid-resume2"),
        turn_started_event(),
        agent_message_event("resumed"),
        turn_completed_event(),
    )

    with patch("asyncio.create_subprocess_exec", return_value=FakeProcess(data)) as mock_exec:
        proc = process_factory(session_id="tid-resume2")
        await proc.send("follow up", callback)
        await proc.stop()

    args = mock_exec.call_args[0]
    assert "resume" in args
    assert args[-1] == "-"
    assert "follow up" not in args


# --- Reset session ---


@pytest.mark.asyncio
async def test_reset_session_clears_id(process_factory):
    """reset_session should clear session_id."""
    from boxagent.agent.codex_process import CodexProcess

    proc = CodexProcess(workspace="/tmp/test", session_id="tid-old")
    await proc.reset_session()
    assert proc.session_id is None


# --- Model override ---


@pytest.mark.asyncio
async def test_model_override(callback, process_factory):
    """Per-turn model override should appear in CLI args."""
    data = make_jsonl(
        thread_started_event(),
        turn_started_event(),
        agent_message_event("ok"),
        turn_completed_event(),
    )

    with patch("asyncio.create_subprocess_exec", return_value=FakeProcess(data)) as mock_exec:
        proc = process_factory(model="gpt-5.4")
        await proc.send("test", callback, model="gpt-5.4-mini")
        await proc.stop()

    args = mock_exec.call_args[0]
    # Per-turn override should win
    model_idx = args.index("--model")
    assert args[model_idx + 1] == "gpt-5.4-mini"
