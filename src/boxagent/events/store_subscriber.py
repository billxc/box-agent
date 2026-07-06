"""StoreSubscriber: the durable subscriber that owns the local store write.

Internal — do not import from business code; use `boxagent.log` instead.

This object performs the SQLite write for a locally published event and returns
the enriched `Event` (with `id` + `origin_seq` minted by `EventStore`). It is the
privileged first-slot subscriber on the MessageBus: `bus.core._StoreBusSubscriber`
(owned by `EventBus`) wraps `write_local` in a `deliver(Message)` adapter and is
registered FIRST, so it runs first and synchronously; the enriched `Event` it
returns is stashed into the message payload and the remaining bus subscribers
receive that same object unchanged.

`EventStore` remains the sole SQLite writer — this class does not open a second
write path.
"""
from __future__ import annotations

from .models import Event
from .storage import EventStore


class StoreSubscriber:
    def __init__(self, store: EventStore, machine_id: str) -> None:
        self._store = store
        self._machine_id = machine_id

    @property
    def machine_id(self) -> str:
        return self._machine_id

    def write_local(
        self, level: str, category: str, message: str, meta: dict | None = None
    ) -> Event:
        """Persist a locally published event and return the enriched `Event`.

        `bot` is pulled out of `meta` because it is a top-level store column.
        The remaining `meta` (or None if empty) is stored as-is. The returned
        `Event` carries the store-assigned `id` and `origin_seq`.
        """
        meta = dict(meta) if meta else {}
        bot = meta.pop("bot", None)
        return self._store.insert_local(
            self._machine_id,
            level,
            category,
            message,
            bot=bot,
            meta=meta or None,
        )
