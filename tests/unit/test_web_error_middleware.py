"""Tests for the aiohttp error-logging middleware."""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from boxagent.events.bus import EventBus
from boxagent.events.storage import EventStore
from boxagent.log import log
from boxagent.web_error_middleware import error_logging_middleware


@pytest.fixture
def bus(tmp_path):
    store = EventStore(tmp_path / "e.db")
    b = EventBus(store, "host")
    log.bind(b)
    yield b, store
    log.unbind()
    b.close()


async def _make_client(app):
    server = TestServer(app)
    await server.start_server()
    return TestClient(server)


@pytest.mark.asyncio
async def test_handler_exception_logs_event(bus):
    _, store = bus

    async def boom(request):
        raise RuntimeError("kapow")

    app = web.Application(middlewares=[error_logging_middleware])
    app.router.add_get("/x", boom)
    client = await _make_client(app)
    try:
        resp = await client.get("/x")
        assert resp.status == 500
    finally:
        await client.close()

    events = [e for e in store.query() if e.category == "web.error"]
    assert len(events) == 1
    e = events[0]
    assert e.level == "error"
    assert e.meta.get("path") == "/x"
    assert e.meta.get("method") == "GET"
    assert "RuntimeError" in (e.meta.get("exception") or "")
    assert "kapow" in e.message


@pytest.mark.asyncio
async def test_500_response_logs_event(bus):
    _, store = bus

    async def crash(request):
        raise web.HTTPInternalServerError(text="Server got itself in trouble")

    app = web.Application(middlewares=[error_logging_middleware])
    app.router.add_get("/y", crash)
    client = await _make_client(app)
    try:
        resp = await client.get("/y")
        assert resp.status == 500
    finally:
        await client.close()

    events = [e for e in store.query() if e.category == "web.error"]
    assert len(events) == 1
    assert events[0].meta.get("status") == 500


@pytest.mark.asyncio
async def test_404_does_not_log(bus):
    _, store = bus

    async def notfound(request):
        raise web.HTTPNotFound()

    app = web.Application(middlewares=[error_logging_middleware])
    app.router.add_get("/z", notfound)
    client = await _make_client(app)
    try:
        resp = await client.get("/z")
        assert resp.status == 404
    finally:
        await client.close()

    assert [e for e in store.query() if e.category == "web.error"] == []


@pytest.mark.asyncio
async def test_200_does_not_log(bus):
    _, store = bus

    async def ok(request):
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[error_logging_middleware])
    app.router.add_get("/ok", ok)
    client = await _make_client(app)
    try:
        resp = await client.get("/ok")
        assert resp.status == 200
    finally:
        await client.close()

    assert [e for e in store.query() if e.category == "web.error"] == []
