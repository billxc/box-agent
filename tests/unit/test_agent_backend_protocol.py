"""Tests for AgentBackend protocol + MockBackend test double."""

import pytest

from boxagent.agent.protocol import AgentBackend
from boxagent.testing.mocks import MockBackend, SendCall


class TestProtocol:
    def test_mock_satisfies_runtime_protocol(self):
        backend = MockBackend()
        assert isinstance(backend, AgentBackend)

    def test_real_backend_satisfies_protocol(self):
        from boxagent.agent.claude_process import ClaudeProcess
        proc = ClaudeProcess(workspace="/tmp", bot_name="t")
        assert isinstance(proc, AgentBackend)


class TestMockBackend:
    @pytest.mark.asyncio
    async def test_default_send_emits_single_chunk(self):
        backend = MockBackend()
        backend.start()
        chunks: list[str] = []

        class CB:
            async def on_stream(self, text): chunks.append(text)
            async def on_tool_call(self, *a, **kw): pass
            async def on_tool_update(self, *a, **kw): pass
            async def on_error(self, e): pass
            async def on_file(self, *a, **kw): pass
            async def on_image(self, *a, **kw): pass

        await backend.send("hi", CB())
        assert chunks == ["ok"]
        assert backend.sends == [SendCall(
            message="hi", model="", chat_id="",
            append_system_prompt="", env=None,
        )]

    @pytest.mark.asyncio
    async def test_script_emits_each_chunk(self):
        backend = MockBackend()
        backend.start()
        chunks: list[str] = []

        class CB:
            async def on_stream(self, text): chunks.append(text)
            async def on_tool_call(self, *a, **kw): pass
            async def on_tool_update(self, *a, **kw): pass
            async def on_error(self, e): pass
            async def on_file(self, *a, **kw): pass
            async def on_image(self, *a, **kw): pass

        backend.script(["alpha", "beta", "gamma"])
        await backend.send("x", CB())
        assert chunks == ["alpha", "beta", "gamma"]

    @pytest.mark.asyncio
    async def test_state_transitions_during_send(self):
        backend = MockBackend()
        observed: list[str] = []

        async def handler(msg, cb, **kw):
            observed.append(backend.state)

        backend.script_handler(handler)
        assert backend.state == "idle"

        class CB:
            async def on_stream(self, *a, **kw): pass
            async def on_tool_call(self, *a, **kw): pass
            async def on_tool_update(self, *a, **kw): pass
            async def on_error(self, *a, **kw): pass
            async def on_file(self, *a, **kw): pass
            async def on_image(self, *a, **kw): pass

        await backend.send("hi", CB())
        assert observed == ["busy"]
        assert backend.state == "idle"

    @pytest.mark.asyncio
    async def test_cancel_and_reset(self):
        backend = MockBackend(session_id="abc-123")
        await backend.cancel()
        assert backend.cancel_count == 1

        await backend.reset_session()
        assert backend.reset_session_count == 1
        assert backend.cancel_count == 2  # reset_session calls cancel
        assert backend.session_id is None

    @pytest.mark.asyncio
    async def test_stop_marks_dead(self):
        backend = MockBackend()
        backend.start()
        await backend.stop()
        assert backend.state == "dead"
        assert backend.stopped is True

    @pytest.mark.asyncio
    async def test_fail_next_turn_sets_diagnostics_after_send(self):
        backend = MockBackend()
        backend.fail_next_turn("boom")

        class CB:
            async def on_stream(self, *a, **kw): pass
            async def on_tool_call(self, *a, **kw): pass
            async def on_tool_update(self, *a, **kw): pass
            async def on_error(self, *a, **kw): pass
            async def on_file(self, *a, **kw): pass
            async def on_image(self, *a, **kw): pass

        # Diagnostics are clean before send
        assert backend.last_turn_failed is False
        assert backend.last_turn_error == ""
        await backend.send("x", CB())
        # After send, failure is reflected
        assert backend.last_turn_failed is True
        assert backend.last_turn_error == "boom"

        # Next send (no fail_next_turn) clears diagnostics
        await backend.send("y", CB())
        assert backend.last_turn_failed is False
        assert backend.last_turn_error == ""

    def test_protocol_includes_new_fields(self):
        """Catch accidental Protocol/Mock drift — new fields are required."""
        backend = MockBackend()
        # Should compile / be accessible (no AttributeError)
        assert backend.agent == ""
        assert backend.last_turn_failed is False
        assert backend.last_turn_error == ""
        assert backend.supports_session_persistence is True
