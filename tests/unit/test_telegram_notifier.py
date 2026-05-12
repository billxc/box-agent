"""Tests for TelegramNotifier — mock aiohttp.ClientSession to avoid real HTTP."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from boxagent.events.bus import EventBus
from boxagent.events.storage import EventStore
from boxagent.events.telegram_notifier import (
    TelegramNotifier,
    _format_message,
    _matches_category,
)
from boxagent.events.models import Event


def _make_event(level="error", category="backend.crash", message="boom", bot=None):
    return Event(
        id=1, origin_machine="m1", origin_seq=1, ts=0.0,
        level=level, category=category, message=message, bot=bot, meta={},
    )


# ---------- pure helpers ----------

def test_format_message_includes_level_category_message():
    e = _make_event()
    text = _format_message(e)
    assert "[ERROR]" in text
    assert "backend.crash" in text
    assert "boom" in text


def test_format_message_includes_bot_and_machine_when_present():
    e = _make_event(bot="bot_a")
    text = _format_message(e)
    assert "bot: bot_a" in text
    assert "@m1" in text


def test_matches_category_empty_prefix_matches_all():
    assert _matches_category("a.b.c", []) is True


def test_matches_category_exact():
    assert _matches_category("a.b", ["a.b"]) is True


def test_matches_category_prefix():
    assert _matches_category("a.b.c", ["a"]) is True
    assert _matches_category("a.b.c", ["a.b"]) is True


def test_matches_category_no_substring_match():
    # "ab" should not match "abx.y" via prefix logic
    assert _matches_category("abx.y", ["ab"]) is False


# ---------- enabled flag ----------

def test_disabled_when_token_empty():
    n = TelegramNotifier(token="", chat_id="1", levels=["error"])
    assert n.enabled is False


def test_disabled_when_chat_id_empty():
    n = TelegramNotifier(token="t", chat_id="", levels=["error"])
    assert n.enabled is False


def test_enabled_when_both_present():
    n = TelegramNotifier(token="t", chat_id="1", levels=["error"])
    assert n.enabled is True


# ---------- attach is no-op when disabled ----------

def test_attach_no_op_when_disabled(tmp_path):
    store = EventStore(tmp_path / "e.db")
    bus = EventBus(store, machine_id="m1")
    n = TelegramNotifier(token="", chat_id="", levels=["error"])
    n.attach(bus)
    bus.publish("error", "c", "m")
    # No subscriber added → no exception, nothing scheduled


# ---------- delivery via mocked session ----------

class _FakeResp:
    def __init__(self, status=200, text=""):
        self.status = status
        self._text = text

    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass
    async def text(self): return self._text


class _FakeSession:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._next_status = 200
        self._next_text = ""

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        return _FakeResp(self._next_status, self._next_text)

    async def close(self): pass


@pytest.mark.asyncio
async def test_delivery_posts_to_bot_api(tmp_path):
    fake = _FakeSession()
    n = TelegramNotifier(
        token="TOK", chat_id="42", levels=["error", "notify"], session=fake,
    )
    store = EventStore(tmp_path / "e.db")
    bus = EventBus(store, machine_id="m1")
    n.attach(bus)
    bus.publish("error", "backend.crash", "boom", bot="bot_a")
    await asyncio.sleep(0.01)
    assert len(fake.calls) == 1
    url, payload = fake.calls[0]
    assert "/botTOK/sendMessage" in url
    assert payload["chat_id"] == "42"
    assert "boom" in payload["text"]


@pytest.mark.asyncio
async def test_level_filter_drops_non_matching(tmp_path):
    fake = _FakeSession()
    n = TelegramNotifier(
        token="TOK", chat_id="42", levels=["error"], session=fake,
    )
    store = EventStore(tmp_path / "e.db")
    bus = EventBus(store, machine_id="m1")
    n.attach(bus)
    bus.publish("info", "c", "ignored")
    bus.publish("debug", "c", "ignored")
    bus.publish("error", "c", "kept")
    await asyncio.sleep(0.01)
    assert len(fake.calls) == 1
    assert "kept" in fake.calls[0][1]["text"]


@pytest.mark.asyncio
async def test_category_prefix_filter(tmp_path):
    fake = _FakeSession()
    n = TelegramNotifier(
        token="TOK", chat_id="42",
        levels=["info", "error"], categories=["agent"], session=fake,
    )
    store = EventStore(tmp_path / "e.db")
    bus = EventBus(store, machine_id="m1")
    n.attach(bus)
    bus.publish("info", "scheduler.run", "no")
    bus.publish("info", "agent.notify", "yes")
    await asyncio.sleep(0.01)
    assert len(fake.calls) == 1
    assert "yes" in fake.calls[0][1]["text"]


@pytest.mark.asyncio
async def test_http_error_does_not_raise(tmp_path):
    fake = _FakeSession()
    fake._next_status = 400
    fake._next_text = "bad"
    n = TelegramNotifier(token="TOK", chat_id="42", levels=["error"], session=fake)
    store = EventStore(tmp_path / "e.db")
    bus = EventBus(store, machine_id="m1")
    n.attach(bus)
    bus.publish("error", "c", "m")
    await asyncio.sleep(0.01)
    # Did not crash; the call was attempted
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_no_rate_limit_every_event_delivers(tmp_path):
    """Per design Q4: completely no throttling."""
    fake = _FakeSession()
    n = TelegramNotifier(token="TOK", chat_id="42", levels=["error"], session=fake)
    store = EventStore(tmp_path / "e.db")
    bus = EventBus(store, machine_id="m1")
    n.attach(bus)
    for i in range(20):
        bus.publish("error", "c", f"e{i}")
    await asyncio.sleep(0.05)
    assert len(fake.calls) == 20


@pytest.mark.asyncio
async def test_detach_stops_delivery(tmp_path):
    fake = _FakeSession()
    n = TelegramNotifier(token="TOK", chat_id="42", levels=["error"], session=fake)
    store = EventStore(tmp_path / "e.db")
    bus = EventBus(store, machine_id="m1")
    n.attach(bus)
    n.detach(bus)
    bus.publish("error", "c", "m")
    await asyncio.sleep(0.01)
    assert fake.calls == []
