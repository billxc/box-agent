"""The bus envelope.

`Packet` is the ONLY thing the bus routes on. It is a neutral leaf: this module
imports nothing project-internal. `payload` is opaque to the bus core — the core
NEVER inspects it. Business semantics (correlation_id, reply_to, kind, …) all
live inside `payload` and are understood only by whichever subscriber cares.

Addressing is transport's job, not business's: `sender`/`receiver` are machine
ids, `topic` is the channel. `receiver == ""` means broadcast (fan out by topic);
a set `receiver` means point-to-point to that one machine.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Packet:
    """An immutable delivery envelope. Local and cluster buses share it verbatim;
    the cluster bus serializes it to JSON over the WS, the local bus delivers it
    in-process.

    message_id: unique per packet, a UUID stamped by the sending bus at `send()`.
                Transport-level identity: dedup / loop-guard / trace. Distinct from
                a business-level correlation_id (which lives in `payload` and only
                exists on request/reply packets).
    sender:     machine id of the bus that created the packet.
    receiver:   machine id; "" = broadcast, set = point-to-point to that machine.
    topic:      routing key, e.g. "chat.<machine>.<bot>.<chat_id>" or "events.<category>".
    payload:    OPAQUE to the bus. The core never reads it; subscribers do.
    ts:         caller-supplied timestamp (seconds). The bus core is clock-free on
                purpose (deterministic + testable), so the caller passes ts.
    """

    message_id: str
    sender: str
    receiver: str
    topic: str
    payload: dict
    ts: float
