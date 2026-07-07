"""Tests for the events HTTP routes — direct handler invocation with
mocked Starlette Request objects (no real server)."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from boxagent.events.bus import EventBus
from boxagent.events.storage import EventStore
from boxagent.transports.web.server import WebHttpServer


def _make_server(tmp_path) -> WebHttpServer:
    config = SimpleNamespace(
        web_token="", web_trust_header="X-Trusted",
        web_host="127.0.0.1", web_port=0, bots={},
    )
    server = WebHttpServer(
        config=config,
        local_dir=tmp_path,
        config_dir=tmp_path,
        storage=None,
        web_channels={},
        pools={},
        topology=MagicMock(),
        cluster_rpc=MagicMock(),
        cluster_routes=None,
    )
    store = EventStore(tmp_path / "events.db")
    bus = EventBus(store=store, machine_id="m1")
    server.set_event_bus(bus)
    return server


def _make_request(query: dict | None = None, remote: str = "127.0.0.1",
                  match_info: dict | None = None, body: dict | None = None):
    req = MagicMock()
    req.query_params = query or {}
    req.client = SimpleNamespace(host=remote)
    req.headers = {}
    req.path_params = match_info or {}
    if body is not None:
        async def _json(): return body
        req.json = _json
    else:
        async def _json_empty(): raise ValueError("no body")
        req.json = _json_empty
    return req


@pytest.fixture
def server(tmp_path):
    s = _make_server(tmp_path)
    yield s
    if s.event_bus is not None:
        s.event_bus.close()


def _seed(server):
    bus = server.event_bus
    bus.publish("info", "scheduler.run", "task fired", task_id="t1", bot="bot_a")
    bus.publish("error", "backend.crash", "boom", bot="bot_b")
    bus.publish("notify", "agent.notify", "hello", bot="bot_a")
    bus.publish("debug", "cluster.peer.up", "tick #1")


# ---------- /api/events query ----------

@pytest.mark.asyncio
async def test_query_returns_all_events(server):
    _seed(server)
    resp = await server._handle_events_query(_make_request())
    body = json.loads(resp.body)
    assert body["ok"] is True
    assert len(body["events"]) == 4


@pytest.mark.asyncio
async def test_query_filter_by_levels(server):
    _seed(server)
    resp = await server._handle_events_query(_make_request({"levels": "error,notify"}))
    body = json.loads(resp.body)
    cats = {e["category"] for e in body["events"]}
    assert cats == {"backend.crash", "agent.notify"}


@pytest.mark.asyncio
async def test_query_filter_by_category_prefix(server):
    _seed(server)
    resp = await server._handle_events_query(
        _make_request({"category_prefix": "cluster"})
    )
    body = json.loads(resp.body)
    assert len(body["events"]) == 1
    assert body["events"][0]["category"] == "cluster.peer.up"


@pytest.mark.asyncio
async def test_query_filter_by_bot(server):
    _seed(server)
    resp = await server._handle_events_query(_make_request({"bot": "bot_a"}))
    body = json.loads(resp.body)
    assert len(body["events"]) == 2


@pytest.mark.asyncio
async def test_query_search(server):
    _seed(server)
    resp = await server._handle_events_query(_make_request({"search": "boom"}))
    body = json.loads(resp.body)
    assert len(body["events"]) == 1
    assert body["events"][0]["message"] == "boom"


@pytest.mark.asyncio
async def test_query_limit_and_pagination(server):
    for i in range(10):
        server.event_bus.publish("info", "c", f"m{i}")
    page1 = json.loads(
        (await server._handle_events_query(_make_request({"limit": "4"}))).body
    )
    assert len(page1["events"]) == 4
    assert page1["next_cursor"] is not None

    page2 = json.loads((await server._handle_events_query(
        _make_request({"limit": "4", "before_id": str(page1["next_cursor"])})
    )).body)
    assert len(page2["events"]) == 4
    # Different page
    p1_ids = {e["id"] for e in page1["events"]}
    p2_ids = {e["id"] for e in page2["events"]}
    assert p1_ids.isdisjoint(p2_ids)


# ---------- /api/events/categories ----------

@pytest.mark.asyncio
async def test_categories_lists_distinct_with_counts(server):
    _seed(server)
    server.event_bus.publish("info", "scheduler.run", "another")
    resp = await server._handle_events_categories(_make_request())
    body = json.loads(resp.body)
    cats = {row["category"]: row["count"] for row in body["categories"]}
    assert cats == {
        "scheduler.run": 2,
        "backend.crash": 1,
        "agent.notify": 1,
        "cluster.peer.up": 1,
    }


# ---------- mark_read ----------

@pytest.mark.asyncio
async def test_mark_read_sets_read_at(server):
    _seed(server)
    events = json.loads((await server._handle_events_query(_make_request())).body)["events"]
    target_id = events[0]["id"]

    resp = await server._handle_events_mark_read(
        _make_request(match_info={"event_id": str(target_id)})
    )
    assert json.loads(resp.body) == {"ok": True, "updated": 1}

    after = json.loads((await server._handle_events_query(_make_request())).body)["events"]
    target = next(e for e in after if e["id"] == target_id)
    assert target["read_at"] is not None


@pytest.mark.asyncio
async def test_read_all_only_affects_unread(server):
    _seed(server)
    resp = await server._handle_events_read_all(_make_request(body={}))
    body = json.loads(resp.body)
    assert body["updated"] == 4

    # Re-running marks zero (everything already read)
    resp2 = await server._handle_events_read_all(_make_request(body={}))
    assert json.loads(resp2.body)["updated"] == 0


@pytest.mark.asyncio
async def test_read_all_with_filter(server):
    _seed(server)
    resp = await server._handle_events_read_all(
        _make_request(body={"levels": ["error"]})
    )
    body = json.loads(resp.body)
    assert body["updated"] == 1


# ---------- bus not bound (graceful) ----------

@pytest.mark.asyncio
async def test_query_returns_empty_when_bus_not_bound(tmp_path):
    config = SimpleNamespace(
        web_token="", web_trust_header="X-Trusted",
        web_host="127.0.0.1", web_port=0, bots={},
    )
    server = WebHttpServer(
        config=config, local_dir=tmp_path, config_dir=tmp_path, storage=None,
        web_channels={}, pools={}, topology=MagicMock(),
        cluster_rpc=MagicMock(), cluster_routes=None,
    )
    resp = await server._handle_events_query(_make_request())
    assert json.loads(resp.body) == {"ok": True, "events": []}
