"""Integration test: log_turn emits AGENT_TURN events when log facade is bound."""
from __future__ import annotations

import pytest

from boxagent.events.bus import EventBus
from boxagent.events.storage import EventStore
from boxagent.log import log
from boxagent.router.callback import log_turn


@pytest.fixture
def bound_bus(tmp_path):
    store = EventStore(tmp_path / "events.db")
    bus = EventBus(store=store, machine_id="m_test")
    log.bind(bus)
    yield bus
    log.unbind()
    bus.close()


def test_log_turn_writes_jsonl_and_emits_event(bound_bus, tmp_path):
    transcript_path = tmp_path / "t" / "sess.jsonl"

    log_turn(transcript_path, bot="bot_a", chat_id="chat_1",
             user_text="hello", assistant_text="hi back")

    # JSONL preserved
    assert transcript_path.exists()
    lines = transcript_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # user + assistant

    # Event log received the summary
    rows = bound_bus._store.query()
    assert len(rows) == 1
    assert rows[0].category == "agent.turn"
    assert rows[0].bot == "bot_a"
    assert rows[0].meta["chat_id"] == "chat_1"
    assert rows[0].meta["user_len"] == len("hello")
    assert rows[0].meta["assistant_len"] == len("hi back")
    # Full text NOT stored in event log (transcript handles that)
    assert "hello" not in rows[0].message
    assert "hi back" not in rows[0].message


def test_log_turn_works_without_bound_bus(tmp_path):
    """Backward-compat: log_turn must not crash when log facade is unbound."""
    log.unbind()
    transcript_path = tmp_path / "t" / "sess.jsonl"

    log_turn(transcript_path, "bot_a", "chat_1", "u", "a")

    assert transcript_path.exists()
