"""Tests: boxagent_tool wrapper logs failures into the event bus."""
from __future__ import annotations

import pytest

from boxagent.events.bus import EventBus
from boxagent.events.storage import EventStore
from boxagent.log import log
from boxagent.tools import ToolContext, boxagent_tool


@pytest.fixture
def bus(tmp_path):
    store = EventStore(tmp_path / "e.db")
    b = EventBus(store, "host")
    log.bind(b)
    yield store
    log.unbind()
    b.close()


def _ctx():
    return ToolContext(bot_name="bot_a", chat_id="c1")


@pytest.mark.asyncio
async def test_tool_returning_error_string_logs_event(bus):
    @boxagent_tool(name="t_err1", group="peer",
                   description="x", schema={})
    async def fails(args, ctx):
        return "Error: gateway not available"

    out = await fails({}, _ctx())
    assert "gateway not available" in out  # passthrough preserved

    events = [e for e in bus.query() if e.category == "agent.tool_error"]
    assert len(events) == 1
    assert events[0].level == "error"
    assert events[0].meta.get("tool") == "t_err1"
    assert "gateway not available" in events[0].message


@pytest.mark.asyncio
async def test_tool_raising_logs_event_and_reraises(bus):
    @boxagent_tool(name="t_err2", group="peer",
                   description="x", schema={})
    async def boom(args, ctx):
        raise RuntimeError("kapow")

    with pytest.raises(RuntimeError):
        await boom({}, _ctx())

    events = [e for e in bus.query() if e.category == "agent.tool_error"]
    assert len(events) == 1
    assert events[0].meta.get("tool") == "t_err2"
    assert "RuntimeError" in events[0].meta.get("exception", "")


@pytest.mark.asyncio
async def test_tool_success_does_not_log_error(bus):
    @boxagent_tool(name="t_ok", group="peer",
                   description="x", schema={})
    async def ok(args, ctx):
        return "all good"

    await ok({}, _ctx())
    assert [e for e in bus.query() if e.category == "agent.tool_error"] == []
