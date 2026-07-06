"""Unit tests for ClusterBus routing — the one `_forward` over fake links.

Covers: broadcast (local + ship to all other links), point-to-point (self /
remote / unroutable), relay (host A→B), loop guard (never back to source),
inbound version gate (hard-cut drop), outbound version-incompatible link →
unreachable signal.

asyncio_mode=auto — async tests need no decorator.
"""
from __future__ import annotations

import asyncio

from boxagent.bus.message import Packet
from boxagent.cluster.cluster_bus import ClusterBus, WIRE_VERSION


class FakeLink:
    """Records the frames the bus ships over it."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, frame: dict) -> None:
        self.sent.append(frame)


class Recorder:
    """Local subscriber recording delivered packets."""

    def __init__(self) -> None:
        self.received: list[Packet] = []

    def deliver(self, packet: Packet) -> None:
        self.received.append(packet)


async def _settle() -> None:
    # Let the async drain pump flush the (in-memory) send queue.
    for _ in range(30):
        await asyncio.sleep(0)


def _route_map(mapping: dict) -> "callable":
    return lambda machine: mapping.get(machine)


def _frame(*, receiver: str, topic: str, sender: str = "other", version: int = WIRE_VERSION) -> dict:
    return {
        "v": version,
        "packet": {
            "message_id": "m1", "sender": sender, "receiver": receiver,
            "topic": topic, "payload": {"n": 1}, "ts": 1.0,
        },
    }


# --------------------------------------------------------------------------- #
# broadcast                                                                    #
# --------------------------------------------------------------------------- #


async def test_broadcast_delivers_local_and_ships_to_all_links():
    bus = ClusterBus(machine_id="host", route=_route_map({}))
    recorder = Recorder()
    bus.subscribe("events.x", recorder)
    link_a, link_b = FakeLink(), FakeLink()
    bus.attach_link("A", link_a.send)
    bus.attach_link("B", link_b.send)

    bus.send(receiver="", topic="events.x", payload={"n": 1}, ts=1.0)
    await _settle()

    assert len(recorder.received) == 1            # local fan-out
    assert len(link_a.sent) == 1                  # shipped to A
    assert len(link_b.sent) == 1                  # shipped to B
    assert link_a.sent[0]["v"] == WIRE_VERSION
    assert link_a.sent[0]["packet"]["receiver"] == ""


async def test_inbound_broadcast_not_shipped_back_to_source():
    bus = ClusterBus(machine_id="host", route=_route_map({}))
    recorder = Recorder()
    bus.subscribe("events.x", recorder)
    link_a, link_b = FakeLink(), FakeLink()
    bus.attach_link("A", link_a.send)
    bus.attach_link("B", link_b.send)

    bus.on_inbound("A", _frame(receiver="", topic="events.x"))
    await _settle()

    assert len(recorder.received) == 1            # delivered locally
    assert link_a.sent == []                      # NOT back to source A
    assert len(link_b.sent) == 1                  # forwarded to B


# --------------------------------------------------------------------------- #
# point-to-point                                                              #
# --------------------------------------------------------------------------- #


async def test_point_to_point_to_self_delivers_local_only():
    bus = ClusterBus(machine_id="host", route=_route_map({}))
    recorder = Recorder()
    bus.subscribe("req.web", recorder)
    link_a = FakeLink()
    bus.attach_link("A", link_a.send)

    bus.send(receiver="host", topic="req.web", payload={"n": 1}, ts=1.0)
    await _settle()

    assert len(recorder.received) == 1
    assert link_a.sent == []                      # no ship for a self-addressed packet


async def test_point_to_point_to_remote_ships_only():
    bus = ClusterBus(machine_id="host", route=_route_map({"B": "linkB"}))
    recorder = Recorder()
    bus.subscribe("req.web", recorder)
    link_b = FakeLink()
    bus.attach_link("linkB", link_b.send)

    bus.send(receiver="B", topic="req.web", payload={"n": 1}, ts=1.0)
    await _settle()

    assert recorder.received == []                # not for us → no local delivery
    assert len(link_b.sent) == 1
    assert link_b.sent[0]["packet"]["receiver"] == "B"


async def test_point_to_point_unroutable_signals_unreachable():
    unreachable: list[str] = []
    bus = ClusterBus(
        machine_id="host", route=_route_map({}),
        on_unreachable=unreachable.append,
    )

    bus.send(receiver="Z", topic="req.web", payload={"n": 1}, ts=1.0)
    await _settle()

    assert unreachable == ["Z"]


# --------------------------------------------------------------------------- #
# relay (host A→B)                                                            #
# --------------------------------------------------------------------------- #


async def test_relay_forwards_to_target_not_local_not_source():
    bus = ClusterBus(machine_id="host", route=_route_map({"B": "linkB"}))
    recorder = Recorder()
    bus.subscribe("req.web", recorder)
    link_a, link_b = FakeLink(), FakeLink()
    bus.attach_link("linkA", link_a.send)
    bus.attach_link("linkB", link_b.send)

    # Guest A's request to guest B arrives on linkA; host relays to linkB.
    bus.on_inbound("linkA", _frame(receiver="B", topic="req.web", sender="A"))
    await _settle()

    assert recorder.received == []                # host is not the target
    assert link_a.sent == []                      # never back to source
    assert len(link_b.sent) == 1                  # relayed onward to B


# --------------------------------------------------------------------------- #
# version gate (hard-cut)                                                     #
# --------------------------------------------------------------------------- #


async def test_inbound_wrong_version_dropped():
    bus = ClusterBus(machine_id="host", route=_route_map({}))
    recorder = Recorder()
    bus.subscribe("events.x", recorder)
    link_b = FakeLink()
    bus.attach_link("B", link_b.send)

    bus.on_inbound("A", _frame(receiver="", topic="events.x", version=2))
    await _settle()

    assert recorder.received == []                # dropped, not delivered
    assert link_b.sent == []                      # dropped, not forwarded


async def test_inbound_missing_version_dropped():
    bus = ClusterBus(machine_id="host", route=_route_map({}))
    recorder = Recorder()
    bus.subscribe("events.x", recorder)

    frame = _frame(receiver="", topic="events.x")
    del frame["v"]
    bus.on_inbound("A", frame)
    await _settle()

    assert recorder.received == []                # missing v → dropped (hard-cut)


async def test_shipped_frame_carries_type_packet_for_read_loop_routing():
    # The WS read loops (guest_client/registry) route a frame to ClusterBus ONLY
    # when frame["type"] == "packet". A frame without it falls to the legacy
    # version gate and is dropped — the real bug this guards.
    bus = ClusterBus(machine_id="host", route=_route_map({}))
    link_a = FakeLink()
    bus.attach_link("A", link_a.send)

    bus.send(receiver="", topic="events.x", payload={"n": 1}, ts=1.0)
    await _settle()

    assert len(link_a.sent) == 1
    frame = link_a.sent[0]
    assert frame["type"] == "packet"
    assert frame["v"] == WIRE_VERSION
    assert "packet" in frame


async def test_wire_round_trip_frame_delivers_on_other_bus():
    # End-to-end: the exact frame node A emits, fed to node B's on_inbound,
    # reaches B's local subscriber (proves the emitted frame is what on_inbound
    # parses — the read-loop contract).
    node_a = ClusterBus(machine_id="A", route=_route_map({"B": "linkB"}))
    link_b = FakeLink()
    node_a.attach_link("linkB", link_b.send)

    node_b = ClusterBus(machine_id="B", route=_route_map({}))
    recorder = Recorder()
    node_b.subscribe("req.web", recorder)

    node_a.send(receiver="B", topic="req.web", payload={"hello": 1}, ts=1.0)
    await _settle()

    assert len(link_b.sent) == 1
    node_b.on_inbound("A", link_b.sent[0])   # feed A's exact wire frame to B
    await _settle()

    assert len(recorder.received) == 1
    assert recorder.received[0].payload == {"hello": 1}
    assert recorder.received[0].receiver == "B"

    unreachable: list[str] = []
    bus = ClusterBus(
        machine_id="host", route=_route_map({"B": "linkB"}),
        on_unreachable=unreachable.append,
    )
    link_b = FakeLink()
    bus.attach_link("linkB", link_b.send, version=2)   # incompatible

    bus.send(receiver="B", topic="req.web", payload={"n": 1}, ts=1.0)
    await _settle()

    assert link_b.sent == []                      # not shipped to an incompatible link
    assert unreachable == ["B"]
