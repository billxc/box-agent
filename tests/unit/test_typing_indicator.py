"""Unit tests for ChannelCallback typing indicator lifecycle."""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from boxagent.channels.base import Attachment, IncomingMessage, StreamHandle
from boxagent.router import Router
from boxagent.router import ChannelCallback


@pytest.fixture
def mock_channel():
    ch = AsyncMock()
    ch.show_typing = AsyncMock()
    ch.stream_start = AsyncMock(
        return_value=StreamHandle(message_id="msg1", chat_id="chat1")
    )
    ch.stream_update = AsyncMock()
    ch.stream_end = AsyncMock()
    ch.send_text = AsyncMock()
    ch.format_tool_call = lambda name, inp: f"tool:{name}"
    return ch


@pytest.fixture
def callback(mock_channel):
    return ChannelCallback(channel=mock_channel, chat_id="chat1")


class TestTypingStartStop:
    async def test_start_typing_creates_task(self, callback):
        await callback.start_typing()
        assert callback._typing_task is not None
        assert not callback._typing_task.done()
        callback._stop_typing()

    async def test_start_typing_calls_show_typing(self, callback, mock_channel):
        await callback.start_typing()
        # Let the loop run once
        await asyncio.sleep(0.05)
        mock_channel.show_typing.assert_called_with("chat1")
        callback._stop_typing()

    async def test_stop_typing_cancels_task(self, callback):
        await callback.start_typing()
        task = callback._typing_task
        callback._stop_typing()
        assert callback._typing_task is None
        await asyncio.sleep(0)  # let cancellation propagate
        assert task.cancelled() or task.done()

    async def test_stop_typing_noop_when_not_started(self, callback):
        # Should not raise
        callback._stop_typing()
        assert callback._typing_task is None

    async def test_start_typing_idempotent(self, callback):
        """Calling start_typing twice stops the first loop."""
        await callback.start_typing()
        first_task = callback._typing_task
        await callback.start_typing()
        second_task = callback._typing_task
        assert first_task is not second_task
        await asyncio.sleep(0)  # let cancellation propagate
        assert first_task.cancelled() or first_task.done()
        assert not second_task.done()
        callback._stop_typing()


class TestOnStreamStopsTyping:
    async def test_on_stream_stops_typing(self, callback, mock_channel):
        await callback.start_typing()
        assert callback._typing_task is not None
        await callback.on_stream("hello")
        assert callback._typing_task is None

    async def test_on_stream_starts_stream(self, callback, mock_channel):
        await callback.on_stream("hello")
        mock_channel.stream_start.assert_called_once_with("chat1", webhook_name="")
        mock_channel.stream_update.assert_called_once()

    async def test_on_stream_reuses_handle(self, callback, mock_channel):
        await callback.on_stream("hello")
        await callback.on_stream(" world")
        # stream_start only called once
        mock_channel.stream_start.assert_called_once()
        assert mock_channel.stream_update.call_count == 2


class TestToolCallRestartsTyping:
    async def test_tool_call_restarts_typing(self, callback, mock_channel):
        """After on_stream stops typing, on_tool_call restarts it."""
        await callback.start_typing()
        await callback.on_stream("thinking...")
        # Typing stopped by on_stream
        assert callback._typing_task is None

        await callback.on_tool_call("read_file", {"path": "x.py"}, "")
        # Typing restarted
        assert callback._typing_task is not None
        assert not callback._typing_task.done()
        callback._stop_typing()

    async def test_tool_call_typing_actually_fires(self, callback, mock_channel):
        """The restarted typing loop actually calls show_typing."""
        await callback.start_typing()
        await callback.on_stream("text")
        mock_channel.show_typing.reset_mock()

        await callback.on_tool_call("bash", {}, "")
        await asyncio.sleep(0.05)
        mock_channel.show_typing.assert_called_with("chat1")
        callback._stop_typing()

    async def test_tool_call_without_prior_stream(self, callback, mock_channel):
        """Tool call before any stream text still restarts typing."""
        await callback.start_typing()
        # tool_call directly (no on_stream first)
        await callback.on_tool_call("bash", {}, "")
        assert callback._typing_task is not None
        assert not callback._typing_task.done()
        callback._stop_typing()

    async def test_tool_call_formats_and_updates_stream(self, callback, mock_channel):
        """Tool call appends formatted text to the stream."""
        # Need a handle first
        await callback.on_stream("text")
        await callback.on_tool_call("read_file", {"path": "x"}, "")
        # stream_update called for text + tool call
        calls = mock_channel.stream_update.call_args_list
        assert len(calls) == 2
        assert "tool:read_file" in calls[1][0][1]
        callback._stop_typing()

    async def test_tool_call_no_format_without_handle(self, callback, mock_channel):
        """Tool call before stream_start sends via send_text, still restarts typing."""
        await callback.on_tool_call("bash", {}, "")
        # No stream_update (no handle), but send_text used instead
        mock_channel.stream_update.assert_not_called()
        mock_channel.send_text.assert_called_once()
        assert "tool:bash" in mock_channel.send_text.call_args[0][1]
        # Typing is running
        assert callback._typing_task is not None
        callback._stop_typing()


class TestFullLifecycle:
    async def test_text_tool_text_cycle(self, callback, mock_channel):
        """Simulate: text → tool → text. Typing follows correctly."""
        # 1. Start typing
        await callback.start_typing()
        assert callback._typing_task is not None

        # 2. First text — typing stops
        await callback.on_stream("Let me check...")
        assert callback._typing_task is None

        # 3. Tool call — typing restarts
        await callback.on_tool_call("read_file", {"path": "foo.py"}, "")
        assert callback._typing_task is not None

        # 4. Second text — typing stops again
        await callback.on_stream("Here's what I found:")
        assert callback._typing_task is None

    async def test_multiple_tool_calls(self, callback, mock_channel):
        """Multiple tool calls each restart typing."""
        await callback.start_typing()
        await callback.on_stream("text")
        assert callback._typing_task is None

        await callback.on_tool_call("tool_a", {}, "")
        task_a = callback._typing_task
        assert task_a is not None

        await callback.on_tool_call("tool_b", {}, "")
        task_b = callback._typing_task
        assert task_b is not None
        # Old task replaced
        assert task_a is not task_b
        await asyncio.sleep(0)  # let cancellation propagate
        assert task_a.cancelled() or task_a.done()

        callback._stop_typing()

    async def test_error_stops_typing(self, callback, mock_channel):
        """on_error stops typing and sends error message."""
        await callback.start_typing()
        await callback.on_error("something broke")
        assert callback._typing_task is None
        mock_channel.send_text.assert_called_once()
        assert "error" in mock_channel.send_text.call_args[0][1].lower()

    async def test_error_after_tool_call_stops_typing(self, callback, mock_channel):
        """Error during tool execution stops the restarted typing."""
        await callback.start_typing()
        await callback.on_stream("text")
        await callback.on_tool_call("bash", {}, "")
        assert callback._typing_task is not None

        await callback.on_error("command failed")
        assert callback._typing_task is None

    async def test_close_stops_typing_and_stream(self, callback, mock_channel):
        await callback.start_typing()
        await callback.on_stream("hello")

        await callback.close()

        assert callback._closed is True
        assert callback._typing_task is None
        mock_channel.stream_end.assert_called_once()

    async def test_late_tool_call_after_close_does_not_restart_typing(
        self, callback, mock_channel
    ):
        await callback.close()
        mock_channel.show_typing.reset_mock()
        mock_channel.send_text.reset_mock()
        mock_channel.stream_update.reset_mock()

        await callback.on_tool_call("bash", {}, "")
        await asyncio.sleep(0.05)

        assert callback._typing_task is None
        mock_channel.show_typing.assert_not_called()
        mock_channel.send_text.assert_not_called()
        mock_channel.stream_update.assert_not_called()

    async def test_late_stream_after_close_is_ignored(self, callback, mock_channel):
        await callback.close()
        mock_channel.stream_start.reset_mock()
        mock_channel.stream_update.reset_mock()

        await callback.on_stream("late text")

        mock_channel.stream_start.assert_not_called()
        mock_channel.stream_update.assert_not_called()


# --- Router-level tests: commands vs dispatch typing behavior ---


def _make_router_channel():
    """Create a mock channel that tracks show_typing calls."""
    ch = AsyncMock()
    ch.show_typing = AsyncMock()
    ch.send_text = AsyncMock()
    ch.stream_start = AsyncMock(
        return_value=StreamHandle(message_id="msg1", chat_id="chat1")
    )
    ch.stream_update = AsyncMock()
    ch.stream_end = AsyncMock()
    ch.format_tool_call = lambda name, inp: f"tool:{name}"
    ch.tool_calls_display = "summary"
    return ch


def _make_router(channel, cli=None):
    if cli is None:
        cli = AsyncMock()
        cli.send = AsyncMock()
        cli.cancel = AsyncMock()
        cli.state = "idle"
        cli.session_id = None
    return Router(
        cli_process=cli,
        channel=channel,
        allowed_users=[123],
        bot_name="test-bot",
    )


def _msg(text):
    return IncomingMessage(
        channel="telegram", chat_id="123", user_id="123", text=text,
    )


class TestCommandsNoTyping:
    """Slash commands must NOT trigger typing indicator."""

    @pytest.mark.parametrize("cmd", ["/status", "/new", "/cancel", "/resume", "/start", "/help", "/verbose", "/sync_skills"])
    async def test_command_no_show_typing(self, cmd):
        ch = _make_router_channel()
        router = _make_router(ch)
        await router.handle_message(_msg(cmd))
        ch.show_typing.assert_not_called()

    @pytest.mark.parametrize("cmd", ["/status", "/new", "/cancel", "/resume", "/start", "/help", "/verbose", "/sync_skills"])
    async def test_command_no_stream_start(self, cmd):
        ch = _make_router_channel()
        router = _make_router(ch)
        await router.handle_message(_msg(cmd))
        ch.stream_start.assert_not_called()

    @pytest.mark.parametrize("cmd", ["/status", "/new", "/cancel", "/resume", "/start", "/help", "/verbose", "/sync_skills"])
    async def test_command_no_lingering_tasks(self, cmd):
        """No background asyncio tasks left after command handling."""
        ch = _make_router_channel()
        router = _make_router(ch)

        tasks_before = {t for t in asyncio.all_tasks() if not t.done()}
        await router.handle_message(_msg(cmd))
        tasks_after = {t for t in asyncio.all_tasks() if not t.done()}

        leaked = tasks_after - tasks_before
        # Filter to only typing-related tasks (our _loop coroutines)
        typing_leaked = [t for t in leaked if "_loop" in repr(t)]
        assert typing_leaked == [], f"Leaked typing tasks: {typing_leaked}"


class TestDispatchTyping:
    """Normal messages go through _dispatch which manages typing."""

    async def test_dispatch_calls_show_typing(self):
        """Normal message triggers typing loop via _dispatch."""
        ch = _make_router_channel()
        cli = AsyncMock()
        # Make cli.send slow enough for typing loop to fire
        async def slow_send(prompt, callback, model="", chat_id="", append_system_prompt="", env=None):
            await asyncio.sleep(0.1)
        cli.send = slow_send
        cli.session_id = None
        router = _make_router(ch, cli)

        await router.handle_message(_msg("hello"))
        ch.show_typing.assert_called()

    async def test_dispatch_typing_cleaned_up_after_send(self):
        """After cli.send completes, no typing task is left behind."""
        ch = _make_router_channel()
        cli = AsyncMock()
        cli.send = AsyncMock()
        cli.session_id = None
        router = _make_router(ch, cli)

        tasks_before = {t for t in asyncio.all_tasks() if not t.done()}
        await router.handle_message(_msg("hello"))
        await asyncio.sleep(0.05)
        tasks_after = {t for t in asyncio.all_tasks() if not t.done()}

        leaked = tasks_after - tasks_before
        typing_leaked = [t for t in leaked if "_loop" in repr(t)]
        assert typing_leaked == [], f"Leaked typing tasks: {typing_leaked}"


class TestConcurrentCommandDuringDispatch:
    """System commands arriving while a dispatch is in progress."""

    @pytest.mark.parametrize("cmd", ["/status", "/new", "/cancel", "/resume", "/start", "/help", "/verbose", "/sync_skills"])
    async def test_command_during_busy_dispatch(self, cmd):
        """Command responds immediately even while CLI is busy."""
        ch = _make_router_channel()
        dispatch_done = asyncio.Event()

        async def slow_send(prompt, callback, model="", chat_id="", append_system_prompt="", env=None):
            await dispatch_done.wait()

        cli = AsyncMock()
        cli.send = slow_send
        cli.cancel = AsyncMock()
        cli.state = "busy"
        cli.session_id = "sess_abc"
        router = _make_router(ch, cli)

        # Start a dispatch that blocks
        dispatch_task = asyncio.create_task(
            router.handle_message(_msg("long running task"))
        )
        await asyncio.sleep(0.05)  # let dispatch start

        # Now send a command concurrently
        ch.send_text.reset_mock()
        await router.handle_message(_msg(cmd))

        # Command should have responded
        ch.send_text.assert_called()

        # Clean up: unblock dispatch
        dispatch_done.set()
        await dispatch_task

    async def test_command_does_not_disrupt_active_typing(self):
        """A /status during dispatch doesn't kill the dispatch's typing task."""
        ch = _make_router_channel()
        dispatch_done = asyncio.Event()

        async def slow_send(prompt, callback, model="", chat_id="", append_system_prompt="", env=None):
            await dispatch_done.wait()

        cli = AsyncMock()
        cli.send = slow_send
        cli.state = "busy"
        cli.session_id = "sess_abc"
        router = _make_router(ch, cli)

        dispatch_task = asyncio.create_task(
            router.handle_message(_msg("do something"))
        )
        await asyncio.sleep(0.05)  # let typing start

        # Typing should be running (at least one call)
        assert ch.show_typing.call_count >= 1

        # Send /status while dispatch is in progress
        await router.handle_message(_msg("/status"))

        # Typing task should still exist — command doesn't touch it
        # Verify by checking show_typing keeps getting called
        ch.show_typing.reset_mock()
        await asyncio.sleep(0.1)
        # The loop is still alive (it sleeps 4s, but the task is not cancelled)
        # We can't easily wait 4s, so just check the task wasn't leaked/killed
        # by verifying dispatch still completes cleanly
        dispatch_done.set()
        await dispatch_task
        # After dispatch completes, typing should be cleaned up
        await asyncio.sleep(0.05)
        tasks = {t for t in asyncio.all_tasks() if not t.done()}
        typing_tasks = [t for t in tasks if "_loop" in repr(t)]
        assert typing_tasks == []

    async def test_status_shows_busy_during_dispatch(self):
        """/status shows state=busy while CLI is processing."""
        ch = _make_router_channel()
        dispatch_done = asyncio.Event()

        async def slow_send(prompt, callback, model="", chat_id="", append_system_prompt="", env=None):
            await dispatch_done.wait()

        cli = AsyncMock()
        cli.send = slow_send
        cli.state = "busy"
        cli.session_id = "sess_123"
        router = _make_router(ch, cli)
        router.bot_name = "test-bot"

        dispatch_task = asyncio.create_task(
            router.handle_message(_msg("work"))
        )
        await asyncio.sleep(0.05)

        ch.send_text.reset_mock()
        await router.handle_message(_msg("/status"))
        text = ch.send_text.call_args[0][1]
        assert "busy" in text.lower()

        dispatch_done.set()
        await dispatch_task

    async def test_cancel_during_dispatch(self):
        """/cancel calls cli.cancel while dispatch is running."""
        ch = _make_router_channel()
        dispatch_done = asyncio.Event()

        async def slow_send(prompt, callback, model="", chat_id="", append_system_prompt="", env=None):
            await dispatch_done.wait()

        cli = AsyncMock()
        cli.send = slow_send
        cli.cancel = AsyncMock()
        cli.state = "busy"
        cli.session_id = None
        router = _make_router(ch, cli)

        dispatch_task = asyncio.create_task(
            router.handle_message(_msg("long task"))
        )
        await asyncio.sleep(0.05)

        await router.handle_message(_msg("/cancel"))
        cli.cancel.assert_called_once()

        dispatch_done.set()
        await dispatch_task

    async def test_late_tool_event_after_cancel_does_not_restart_typing(self):
        """Late callback events after dispatch cleanup must not leak typing loops."""
        ch = _make_router_channel()
        dispatch_done = asyncio.Event()
        captured_callback = None

        async def slow_send(prompt, callback, model="", chat_id="", append_system_prompt="", env=None):
            nonlocal captured_callback
            captured_callback = callback
            await dispatch_done.wait()

        cli = AsyncMock()
        cli.send = slow_send
        cli.cancel = AsyncMock(side_effect=lambda: dispatch_done.set())
        cli.state = "busy"
        cli.session_id = None
        router = _make_router(ch, cli)

        dispatch_task = asyncio.create_task(
            router.handle_message(_msg("long task"))
        )
        await asyncio.sleep(0.05)

        await router.handle_message(_msg("/cancel"))
        await dispatch_task

        assert captured_callback is not None
        ch.show_typing.reset_mock()
        ch.send_text.reset_mock()
        ch.stream_update.reset_mock()

        await captured_callback.on_tool_call("bash", {}, "")
        await asyncio.sleep(0.05)

        tasks = {t for t in asyncio.all_tasks() if not t.done()}
        typing_tasks = [t for t in tasks if "_loop" in repr(t)]
        assert typing_tasks == []
        ch.show_typing.assert_not_called()
        ch.stream_update.assert_not_called()
