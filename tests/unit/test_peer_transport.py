"""Unit tests for the shared PeerTransport (registry + send-and-swallow)."""
from __future__ import annotations

from boxagent.cluster.peer_transport import PeerTransport


def test_attach_and_read_access():
    transport = PeerTransport(log_prefix="test")

    async def send(frame):
        return None

    assert "peer1" not in transport
    transport.attach_peer("peer1", send)
    assert "peer1" in transport
    assert transport.get("peer1") is send
    assert transport.peer_keys() == ["peer1"]
    assert list(transport) == ["peer1"]


def test_attach_replaces_existing_send():
    transport = PeerTransport(log_prefix="test")

    async def first(frame):
        return None

    async def second(frame):
        return None

    transport.attach_peer("peer1", first)
    transport.attach_peer("peer1", second)
    assert transport.get("peer1") is second
    assert transport.peer_keys() == ["peer1"]


def test_detach_returns_whether_existed():
    transport = PeerTransport(log_prefix="test")

    async def send(frame):
        return None

    transport.attach_peer("peer1", send)
    assert transport.detach_peer("peer1") is True
    assert transport.detach_peer("peer1") is False
    assert "peer1" not in transport


def test_clear_removes_all_peers():
    transport = PeerTransport(log_prefix="test")

    async def send(frame):
        return None

    transport.attach_peer("peer1", send)
    transport.attach_peer("peer2", send)
    transport.clear()
    assert transport.peer_keys() == []


async def test_send_to_delivers_frame():
    transport = PeerTransport(log_prefix="test")
    received: list[dict] = []

    async def send(frame):
        received.append(frame)

    transport.attach_peer("peer1", send)
    frame = {"type": "hello"}
    await transport.send_to("peer1", frame)
    assert received == [frame]


async def test_send_to_unknown_peer_is_noop():
    transport = PeerTransport(log_prefix="test")
    # No registered peer — send_to must not raise.
    await transport.send_to("ghost", {"type": "hello"})


async def test_send_to_swallows_send_failure():
    transport = PeerTransport(log_prefix="test")

    async def failing(frame):
        raise RuntimeError("boom")

    transport.attach_peer("peer1", failing)
    # Must not propagate the exception (swallow + log).
    await transport.send_to("peer1", {"type": "hello"})
