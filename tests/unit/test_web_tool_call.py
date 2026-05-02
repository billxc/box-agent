"""Tests for polymorphic tool_call rendering on WebChannel + ChannelCallback."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from boxagent.channels.web import WebChannel
from boxagent.router.callback import ChannelCallback


# ─── WebChannel.on_tool_call publishes structured events ────────────────────

def test_on_tool_call_publishes_structured_event():
    wc = WebChannel(bot_name="b")
    q = wc.subscribe("c1")
    asyncio.run(wc.on_tool_call("c1", "t-1", "Bash", {"command": "ls"}, ""))
    ev = q.get_nowait()
    assert ev["type"] == "tool_call"
    assert ev["tool_id"] == "t-1"
    assert ev["name"] == "Bash"
    assert ev["args"] == {"command": "ls"}


def test_on_tool_call_allocates_id_when_missing():
    wc = WebChannel(bot_name="b")
    q = wc.subscribe("c1")
    asyncio.run(wc.on_tool_call("c1", "", "Read", {"path": "/x"}, ""))
    assert q.get_nowait()["tool_id"]  # auto-allocated, non-empty


def test_on_tool_call_codex_single_shot_publishes_call_then_result():
    """When `result` is non-empty (Codex), WebChannel publishes both events."""
    wc = WebChannel(bot_name="b")
    q = wc.subscribe("c1")
    asyncio.run(wc.on_tool_call("c1", "t-2", "shell", {"command": "ls"}, "exit=0\nfile"))
    call = q.get_nowait()
    assert call["type"] == "tool_call"
    result = q.get_nowait()
    assert result["type"] == "tool_result"
    assert result["tool_id"] == "t-2"
    assert result["ok"] is True
    assert result["summary"].startswith("exit=0")


def test_on_tool_call_returns_false_so_callback_skips_paragraph_break():
    wc = WebChannel(bot_name="b")
    used_stream = asyncio.run(wc.on_tool_call("c1", "t", "x", {}, ""))
    assert used_stream is False


# ─── WebChannel.on_tool_update maps status → result event ────────────────────

def test_on_tool_update_completed_publishes_ok_result():
    wc = WebChannel(bot_name="b")
    q = wc.subscribe("c1")
    asyncio.run(wc.on_tool_update(
        "c1", "t-3", "$ ls", status="completed", output="done\n",
    ))
    ev = q.get_nowait()
    assert ev["type"] == "tool_result"
    assert ev["tool_id"] == "t-3"
    assert ev["ok"] is True
    assert ev["summary"] == "done\n"


def test_on_tool_update_failed_publishes_failed_result():
    wc = WebChannel(bot_name="b")
    q = wc.subscribe("c1")
    asyncio.run(wc.on_tool_update(
        "c1", "t-4", "$ x", status="failed", output="boom",
    ))
    ev = q.get_nowait()
    assert ev["ok"] is False
    assert ev["error"] == "boom"


def test_on_tool_update_in_progress_publishes_nothing():
    wc = WebChannel(bot_name="b")
    q = wc.subscribe("c1")
    asyncio.run(wc.on_tool_update("c1", "t-5", "$ x", status="in_progress"))
    assert q.empty()


# ─── ChannelCallback delegates without branching ─────────────────────────────

def _cb_with(channel):
    cb = ChannelCallback(channel=channel, chat_id="c1")
    return cb


def test_channelcallback_on_tool_call_delegates_to_channel():
    ch = MagicMock()
    ch.on_tool_call = AsyncMock(return_value=False)
    ch.show_typing = AsyncMock()
    cb = _cb_with(ch)
    asyncio.run(cb.on_tool_call("Bash", {"command": "ls"}, "result", tool_id="t-1"))
    ch.on_tool_call.assert_awaited_once()
    args, kwargs = ch.on_tool_call.call_args
    assert args == ("c1", "t-1", "Bash", {"command": "ls"}, "result")
    assert kwargs["stream_handle"] is None
    assert kwargs["webhook_name"] == ""


def test_channelcallback_marks_paragraph_break_when_channel_streamed():
    ch = MagicMock()
    ch.on_tool_call = AsyncMock(return_value=True)  # channel streamed
    ch.show_typing = AsyncMock()
    cb = _cb_with(ch)
    asyncio.run(cb.on_tool_call("x", {}, "", tool_id=""))
    assert cb._needs_paragraph_break_after_tool is True


def test_channelcallback_no_paragraph_break_when_channel_did_not_stream():
    ch = MagicMock()
    ch.on_tool_call = AsyncMock(return_value=False)
    ch.show_typing = AsyncMock()
    cb = _cb_with(ch)
    asyncio.run(cb.on_tool_call("x", {}, "", tool_id=""))
    assert cb._needs_paragraph_break_after_tool is False


def test_channelcallback_on_tool_update_delegates():
    ch = MagicMock()
    ch.on_tool_update = AsyncMock(return_value=False)
    ch.show_typing = AsyncMock()
    cb = _cb_with(ch)
    asyncio.run(cb.on_tool_update(
        tool_call_id="t-3", title="$ ls", status="completed", output="done",
    ))
    ch.on_tool_update.assert_awaited_once()
    args, kwargs = ch.on_tool_update.call_args
    assert args == ("c1", "t-3", "$ ls")
    assert kwargs["status"] == "completed"
    assert kwargs["output"] == "done"
