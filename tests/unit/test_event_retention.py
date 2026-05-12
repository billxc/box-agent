"""Tests for RetentionSweeper."""
from __future__ import annotations

import asyncio
import time

import pytest

from boxagent.events.retention import RetentionSweeper, DEFAULT_RETENTION_SECONDS
from boxagent.events.storage import EventStore


@pytest.fixture
def store(tmp_path):
    s = EventStore(tmp_path / "events.db")
    yield s
    s.close()


def test_sweep_once_deletes_older_than_retention(store):
    now = time.time()
    store.insert_local("m1", "info", "c", "old", ts=now - DEFAULT_RETENTION_SECONDS - 100)
    store.insert_local("m1", "info", "c", "fresh", ts=now)
    sweeper = RetentionSweeper(store)
    deleted = sweeper.sweep_once()
    assert deleted == 1
    msgs = {e.message for e in store.query()}
    assert msgs == {"fresh"}


def test_sweep_once_returns_zero_when_nothing_old(store):
    store.insert_local("m1", "info", "c", "fresh", ts=time.time())
    assert RetentionSweeper(store).sweep_once() == 0


def test_custom_retention(store):
    now = time.time()
    store.insert_local("m1", "info", "c", "10s old", ts=now - 10)
    store.insert_local("m1", "info", "c", "now", ts=now)
    sweeper = RetentionSweeper(store, retention_seconds=5)
    assert sweeper.sweep_once() == 1
    msgs = {e.message for e in store.query()}
    assert msgs == {"now"}


@pytest.mark.asyncio
async def test_start_loops_and_sweeps(store):
    now = time.time()
    store.insert_local("m1", "info", "c", "old", ts=now - 100)
    sweeper = RetentionSweeper(store, retention_seconds=10, interval_seconds=0.05)
    sweeper.start()
    await asyncio.sleep(0.08)
    await sweeper.stop()
    assert store.query() == []


@pytest.mark.asyncio
async def test_stop_idempotent(store):
    sweeper = RetentionSweeper(store)
    await sweeper.stop()
    sweeper.start()
    await sweeper.stop()
    await sweeper.stop()
