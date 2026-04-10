"""Integration tests for ClaudeProcess with real Claude CLI."""

import shutil

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not shutil.which("claude"), reason="claude CLI not on PATH"
    ),
]


class TestClaudeProcessReal:
    """Tests that spawn real claude CLI processes."""

    @pytest.mark.timeout(60)
    async def test_simple_prompt_returns_text(self):
        """Send a simple prompt, verify text response received."""
        from boxagent.agent.claude_process import ClaudeProcess
        from unittest.mock import AsyncMock

        callback = AsyncMock()
        collected_text = []

        async def collect_stream(text):
            collected_text.append(text)

        callback.on_stream = collect_stream
        callback.on_tool_call = AsyncMock()
        callback.on_error = AsyncMock()

        cli = ClaudeProcess(workspace="/tmp")
        await cli._execute_turn(
            "Reply with exactly: hello world. Nothing else.", callback
        )

        full_text = "".join(collected_text).lower()
        assert "hello" in full_text
        assert cli.session_id is not None
        callback.on_error.assert_not_called()

    @pytest.mark.timeout(60)
    async def test_session_resume(self):
        """First turn gets session_id, second turn resumes."""
        from boxagent.agent.claude_process import ClaudeProcess
        from unittest.mock import AsyncMock

        callback = AsyncMock()
        callback.on_stream = AsyncMock()
        callback.on_tool_call = AsyncMock()
        callback.on_error = AsyncMock()

        cli = ClaudeProcess(workspace="/tmp")

        # Turn 1
        await cli._execute_turn("Say hello.", callback)
        session_id = cli.session_id
        assert session_id is not None

        # Turn 2 with resume
        callback.reset_mock()
        await cli._execute_turn("Say goodbye.", callback)
        assert cli.session_id is not None
        callback.on_error.assert_not_called()
