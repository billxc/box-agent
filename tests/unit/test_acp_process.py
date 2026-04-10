"""Unit tests for ACPProcess and ACP client mapping."""

from unittest.mock import AsyncMock, MagicMock, patch

from acp.schema import AgentMessageChunk, TextContentBlock, ToolCallProgress, ToolCallStart


class TestACPClientMapping:
    async def test_agent_message_chunk_streams_text(self):
        from boxagent.agent.acp_process import _BoxAgentACPClient

        client = _BoxAgentACPClient()
        cb = AsyncMock()
        client.set_callback(cb)

        update = AgentMessageChunk(
            content=TextContentBlock(text="hello", type="text"),
            session_update="agent_message_chunk",
        )
        await client.session_update("s1", update)

        cb.on_stream.assert_awaited_once_with("hello")

    async def test_tool_start_and_progress_map_to_tool_update_with_title_cache(self):
        from boxagent.agent.acp_process import _BoxAgentACPClient

        client = _BoxAgentACPClient()
        cb = AsyncMock()
        client.set_callback(cb)

        start = ToolCallStart(
            tool_call_id="call_1",
            title="Run pwd",
            status="in_progress",
            raw_input={"command": "pwd"},
            session_update="tool_call",
        )
        progress = ToolCallProgress(
            tool_call_id="call_1",
            title="tool:call_1",
            status="completed",
            raw_output={"stdout": "/tmp/acp-test\n"},
            session_update="tool_call_update",
        )

        await client.session_update("s1", start)
        await client.session_update("s1", progress)

        assert cb.on_tool_update.await_count == 2
        first = cb.on_tool_update.await_args_list[0].kwargs
        second = cb.on_tool_update.await_args_list[1].kwargs
        assert first["title"] == "Run pwd"
        assert first["status"] == "in_progress"
        assert second["title"] == "Run pwd"
        assert second["status"] == "completed"
        assert second["output"] == {"stdout": "/tmp/acp-test\n"}


class TestACPProcessSessionRestore:
    async def test_new_session_sets_public_session_id(self):
        from boxagent.agent.acp_process import ACPProcess

        proc = ACPProcess(workspace="/tmp/test")
        conn = AsyncMock()
        conn.initialize.return_value = MagicMock(protocol_version=1)
        conn.new_session.return_value = MagicMock(session_id="session-new")
        ctx = AsyncMock()
        ctx.__aenter__.return_value = (conn, MagicMock())
        ctx.__aexit__.return_value = None

        with patch("boxagent.agent.acp_process.spawn_agent_process", return_value=ctx):
            await proc._ensure_connected()

        assert proc._acp_session_id == "session-new"
        assert proc.session_id == "session-new"
        conn.load_session.assert_not_awaited()

    async def test_load_session_used_when_session_id_present(self):
        from boxagent.agent.acp_process import ACPProcess

        proc = ACPProcess(workspace="/tmp/test", session_id="session-old")
        conn = AsyncMock()
        conn.initialize.return_value = MagicMock(protocol_version=1)
        ctx = AsyncMock()
        ctx.__aenter__.return_value = (conn, MagicMock())
        ctx.__aexit__.return_value = None

        with patch("boxagent.agent.acp_process.spawn_agent_process", return_value=ctx):
            await proc._ensure_connected()

        conn.load_session.assert_awaited_once_with(
            session_id="session-old",
            cwd="/tmp/test",
        )
        conn.new_session.assert_not_awaited()
        assert proc._acp_session_id == "session-old"
        assert proc.session_id == "session-old"

    async def test_load_session_suppresses_replay_notifications(self):
        """Session-update notifications during load_session must not reach the callback."""
        from boxagent.agent.acp_process import ACPProcess, _BoxAgentACPClient

        proc = ACPProcess(workspace="/tmp/test", session_id="session-old")
        conn = AsyncMock()
        conn.initialize.return_value = MagicMock(protocol_version=1)
        ctx = AsyncMock()
        ctx.__aenter__.return_value = (conn, MagicMock())
        ctx.__aexit__.return_value = None

        # Simulate: ACP server fires replay events during load_session
        async def fake_load_session(**kwargs):
            client = proc._client
            update = AgentMessageChunk(
                content=TextContentBlock(text="old message", type="text"),
                session_update="agent_message_chunk",
            )
            await client.session_update("session-old", update)

        conn.load_session.side_effect = fake_load_session

        cb = AsyncMock()
        proc._client.set_callback(cb)

        with patch("boxagent.agent.acp_process.spawn_agent_process", return_value=ctx):
            await proc._ensure_connected()

        # The replay notification should have been suppressed
        cb.on_stream.assert_not_awaited()
        assert proc._acp_session_id == "session-old"
        # Suppress flag must be cleared after load_session
        assert proc._client._suppress_updates is False

    async def test_load_session_falls_back_to_new_session(self):
        from boxagent.agent.acp_process import ACPProcess

        proc = ACPProcess(workspace="/tmp/test", session_id="session-old")
        conn = AsyncMock()
        conn.initialize.return_value = MagicMock(protocol_version=1)
        conn.load_session.side_effect = RuntimeError("missing")
        conn.new_session.return_value = MagicMock(session_id="session-new")
        ctx = AsyncMock()
        ctx.__aenter__.return_value = (conn, MagicMock())
        ctx.__aexit__.return_value = None

        with patch("boxagent.agent.acp_process.spawn_agent_process", return_value=ctx):
            await proc._ensure_connected()

        conn.load_session.assert_awaited_once_with(
            session_id="session-old",
            cwd="/tmp/test",
        )
        conn.new_session.assert_awaited_once_with(cwd="/tmp/test")
        assert proc._acp_session_id == "session-new"
        assert proc.session_id == "session-new"


class TestACPProcessCancel:
    async def test_cancel_sends_acp_cancel_when_busy(self):
        from boxagent.agent.acp_process import ACPProcess

        proc = ACPProcess(workspace="/tmp/test")
        proc.state = "busy"
        proc._conn = AsyncMock()
        proc._acp_session_id = "session-1"

        await proc.cancel()

        assert proc._cancelled is True
        assert proc._cancel_requested.is_set()
        proc._conn.cancel.assert_awaited_once_with(session_id="session-1")
        assert proc.state == "busy"

    async def test_cancel_noop_when_not_busy(self):
        from boxagent.agent.acp_process import ACPProcess

        proc = ACPProcess(workspace="/tmp/test")
        proc.state = "idle"
        proc._conn = AsyncMock()
        proc._acp_session_id = "session-1"

        await proc.cancel()

        proc._conn.cancel.assert_not_called()
        assert proc._cancelled is False

    async def test_cancel_disconnects_if_cancel_call_fails(self):
        from boxagent.agent.acp_process import ACPProcess

        proc = ACPProcess(workspace="/tmp/test")
        proc.state = "busy"
        proc._conn = AsyncMock()
        proc._conn.cancel.side_effect = RuntimeError("boom")
        proc._acp_session_id = "session-1"
        proc._disconnect = AsyncMock()

        await proc.cancel()

        proc._disconnect.assert_awaited_once()
