"""Unit tests for RequestReply — request/reply over the cluster bus.

Covers: the request packet shape + correlation, reply resolution, fast-fail on
unreachable, local dispatch returns None, and the responder emits a reply packet.
Uses a fake bus (records send, exposes subscribers) — no real WS.

asyncio_mode=auto — async tests need no decorator.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from boxagent.cluster.request_reply import RequestReply


class FakeSubscription:
    def close(self) -> None:
        pass


class FakeBus:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.subs: dict = {}

    def subscribe(self, topic_pattern, subscriber):
        self.subs[topic_pattern] = subscriber
        return FakeSubscription()

    def send(self, *, receiver, topic, payload, ts):
        self.sent.append({"receiver": receiver, "topic": topic, "payload": payload})
        return "message-id"


def _topology(machine: str):
    return SimpleNamespace(local_machine_id=lambda: machine, guest_registry=None)


def _packet(payload: dict):
    return SimpleNamespace(payload=payload)


async def test_request_sends_packet_and_resolves_on_reply():
    bus = FakeBus()
    rr = RequestReply(bus=bus, topology=_topology("mbp"), local_web_port=0, id_factory=lambda: "cid1")

    task = asyncio.create_task(rr.request("devbox", "POST", "/api/send", body={"x": 1}))
    await asyncio.sleep(0)  # let it send + park the future

    sent = bus.sent[0]
    assert sent["receiver"] == "devbox"
    assert sent["topic"] == "request.devbox"
    assert sent["payload"]["method"] == "POST"
    assert sent["payload"]["path"] == "/api/send"
    assert sent["payload"]["correlation_id"] == "cid1"
    assert sent["payload"]["reply_machine"] == "mbp"

    # inject the correlated reply on the reply-inbox subscriber
    bus.subs["reply.mbp."].deliver(_packet({"correlation_id": "cid1", "status": 200, "body": {"ok": True}}))
    result = await task
    assert result == {"status": 200, "body": {"ok": True}}


async def test_request_fast_fails_on_unreachable():
    bus = FakeBus()
    rr = RequestReply(bus=bus, topology=_topology("mbp"), local_web_port=0, id_factory=lambda: "cid1")

    task = asyncio.create_task(rr.request("devbox", "GET", "/x", timeout=30.0))
    await asyncio.sleep(0)
    rr.fail_unreachable("devbox")   # bus signalled the peer is gone

    result = await task
    assert result["status"] == 502
    assert "unreachable" in result["body"]["error"]


async def test_fail_unreachable_only_affects_that_machine():
    bus = FakeBus()
    ids = iter(["cid1", "cid2"])
    rr = RequestReply(bus=bus, topology=_topology("mbp"), local_web_port=0, id_factory=lambda: next(ids))

    task_a = asyncio.create_task(rr.request("devbox", "GET", "/a"))
    task_b = asyncio.create_task(rr.request("nas", "GET", "/b"))
    await asyncio.sleep(0)

    rr.fail_unreachable("devbox")
    result_a = await task_a
    assert result_a["status"] == 502
    assert not task_b.done()   # request to `nas` still pending

    bus.subs["reply.mbp."].deliver(_packet({"correlation_id": "cid2", "status": 200, "body": {}}))
    result_b = await task_b
    assert result_b["status"] == 200


async def test_dispatch_machine_request_local_returns_none():
    bus = FakeBus()
    rr = RequestReply(bus=bus, topology=_topology("mbp"), local_web_port=0)
    request = SimpleNamespace(query={})
    assert await rr.dispatch_machine_request("mbp", "GET", "/x", request) is None


async def test_responder_runs_loopback_and_replies():
    bus = FakeBus()
    rr = RequestReply(bus=bus, topology=_topology("devbox"), local_web_port=0)

    async def fake_loopback(method, path, query, body):
        assert (method, path) == ("POST", "/api/send")
        return {"status": 201, "body": {"done": True}}

    rr._loopback = fake_loopback
    await rr._serve_request({
        "method": "POST", "path": "/api/send", "query": {}, "body": {"x": 1},
        "reply_machine": "mbp", "correlation_id": "cid9",
    })

    reply = bus.sent[-1]
    assert reply["receiver"] == "mbp"
    assert reply["topic"] == "reply.mbp.cid9"
    assert reply["payload"]["status"] == 201
    assert reply["payload"]["correlation_id"] == "cid9"


async def test_responder_request_subscriber_dispatches():
    # An inbound request packet on "request.<local>" schedules a serve task.
    bus = FakeBus()
    rr = RequestReply(bus=bus, topology=_topology("devbox"), local_web_port=0)
    served: list = []

    async def fake_loopback(method, path, query, body):
        served.append((method, path))
        return {"status": 200, "body": {}}

    rr._loopback = fake_loopback
    bus.subs["request.devbox"].deliver(_packet({
        "method": "GET", "path": "/api/bots", "query": {}, "body": None,
        "reply_machine": "mbp", "correlation_id": "cid7",
    }))
    await asyncio.sleep(0)   # let the spawned serve task run
    await asyncio.sleep(0)
    assert served == [("GET", "/api/bots")]
