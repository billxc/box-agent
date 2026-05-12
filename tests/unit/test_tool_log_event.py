"""Tests for the log_event MCP builtin tool."""
from __future__ import annotations

import pytest

from boxagent.events.bus import EventBus
from boxagent.events.storage import EventStore
from boxagent.log import log
from boxagent.tools import ToolContext
from boxagent.tools.builtin.log_event import log_event


@pytest.fixture
def bound_bus(tmp_path):
    store = EventStore(tmp_path / "events.db")
    bus = EventBus(store, machine_id="m1")
    log.bind(bus)
    yield store, bus
    log.unbind()
    bus.close()


def _ctx():
    return ToolContext(bot_name="bot_a", chat_id="chat_1")


@pytest.mark.asyncio
async def test_log_event_basic(bound_bus):
    store, _ = bound_bus
    out = await log_event(
        {"category": "task_done", "message": "finished work"}, _ctx(),
    )
    assert "logged" in out.lower()
    events = store.query()
    assert len(events) == 1
    e = events[0]
    assert e.category == "agent.task_done"  # auto-prefixed
    assert e.message == "finished work"
    assert e.level == "info"
    assert e.bot == "bot_a"


@pytest.mark.asyncio
async def test_log_event_explicit_level(bound_bus):
    store, _ = bound_bus
    await log_event(
        {"category": "alert", "message": "!", "level": "error"}, _ctx(),
    )
    events = store.query()
    assert events[0].level == "error"


@pytest.mark.asyncio
async def test_log_event_already_prefixed_not_double_prefixed(bound_bus):
    store, _ = bound_bus
    await log_event(
        {"category": "agent.custom", "message": "x"}, _ctx(),
    )
    events = store.query()
    assert events[0].category == "agent.custom"


@pytest.mark.asyncio
async def test_log_event_meta_passed_through(bound_bus):
    store, _ = bound_bus
    await log_event(
        {"category": "x", "message": "m", "meta": {"k": "v", "n": 1}}, _ctx(),
    )
    events = store.query()
    assert events[0].meta["k"] == "v"
    assert events[0].meta["n"] == 1


@pytest.mark.asyncio
async def test_log_event_invalid_level_falls_back(bound_bus):
    store, _ = bound_bus
    out = await log_event(
        {"category": "x", "message": "m", "level": "garbage"}, _ctx(),
    )
    events = store.query()
    assert events[0].level == "info"
    assert "level" in out.lower() or "logged" in out.lower()


@pytest.mark.asyncio
async def test_log_event_missing_category_is_error(bound_bus):
    store, _ = bound_bus
    out = await log_event({"message": "x"}, _ctx())
    assert "error" in out.lower()
    assert [e for e in store.query() if e.category != "agent.tool_error"] == []


@pytest.mark.asyncio
async def test_log_event_missing_message_is_error(bound_bus):
    store, _ = bound_bus
    out = await log_event({"category": "x"}, _ctx())
    assert "error" in out.lower()
    assert [e for e in store.query() if e.category != "agent.tool_error"] == []
