# DECISION — Unify the four buses into one content-agnostic MessageBus

> Facilitator synthesis of Round 1 + Round 2 (architect / engineer / tester). The discussion **converged**. This document is the executable contract. It synthesizes; it adds no new scope.

---

## 0. What we are solving (one paragraph)

Today BoxAgent has **four** message-delivery implementations that are really a **2×2 grid** (transport axis × content axis): `EventBus` (local events) + `EventSyncer` (remote events) + `WebChannel._publish` (local chat) + `ChatSyncer` (remote chat). The two remote syncers **copy-pasted each other's skeleton** (`decisions.md`: "ChatSyncer 抄了已在生产验证的 EventSyncer 骨架"), and the two wiring modules **fight over the same three registry callbacks** in a fragile install-order chain (`chat_sync_wiring` must install after `sync_wiring` and fall through). We unify the **link + local-fan-out + wiring + wire-frame + dispatch** into one bus, and keep the **two replication policies** (durable-broadcast vs ephemeral-demand) as two sibling subscriber-coordinators. This deletes the copy-pasted skeleton and the dual chained wiring while keeping every load-bearing behavior byte-identical.

---

## 1. Consensus points (all three agents agree)

The target architecture, agreed by architect + engineer + tester:

1. **Content-agnostic `MessageBus` core.** The bus routes on `topic` only; `payload` is opaque; the bus never constructs `Event`, never reads `origin_seq`, never touches SQLite. Envelope is `{topic, payload, ts}` — nothing else promoted (`origin_seq`/`origin_machine`/`level`/`bot` all live inside `payload`).

2. **Composed `PeerTransport`.** The shared `_peers: dict[peer_key, SendFrame]` + `send_to` (byte-for-byte identical today) + `attach_peer`/`detach_peer` + topic-routed `handle_frame` dispatch (~40 lines of real logic + ~120 lines of duplicated wiring) is extracted **once** by composition. Both replicators hold one `PeerTransport`, not inherit from a base.

3. **`Local`/`Remote` Subscriber abstraction.** A subscriber is "something that receives envelopes for a topic, in order." `LocalSubscriber` wraps an in-process `asyncio.Queue` (`put_nowait`, drop-on-full). `RemoteSubscriber` wraps a bounded queue + **one pump task** that `await`s `link.send(frame)`. Only the *link* differs — this is the "location transparent" model chat already evolved toward.

4. **Sibling `EventReplicator` + `ChatReplicator` over a shared `PeerTransport`.** NOT one strategy class, NOT base+subclass. There is **no shared replication algorithm** — "resync backlog via store cursor + gossip to all peers" and "refcount demand + route to one peer" share zero statements. They are two *subscribers* to the one bus (like `TelegramNotifier` and `EventStreamSubscriber` already are), differing only in policy.

5. **`EventStore` = a privileged *synchronous* subscriber.** For durable topics the store-subscriber runs **first, synchronously**, mints `id`+`origin_seq`, and the enriched envelope is what the remaining N−1 subscribers see. This is mechanically what `EventBus.publish` does today (`bus.py:47` insert_local → line 55 ordered for-loop). Making the store an unordered async peer would break `/events` `before_id` pagination (needs `id` at fan-out time) and cross-machine dedup (needs `origin_seq`) and reintroduce 坑 #1 (ordering). `publish` stays **synchronous** for local fan-out; all async is confined to the `RemoteSubscriber` pump.

6. **Durability = a per-topic subscriber-list fact, NOT an envelope field, NOT inferred from topic name, NOT a runtime check.** A topic is registered once with its subscriber set. `events.*` → `[StoreSubscriber(first, sync), EventReplicator, TelegramNotifier, EventStreamSubscriber]`; `chat.*` → `[ChatReplicator, local-queues]`. "durable" ≡ "is `StoreSubscriber` in this topic's subscriber list." **stream_delta physically cannot reach SQLite because `StoreSubscriber` is not in `chat.*`'s list** — a wiring fact, enforced by construction.

7. **`boxagent.log` facade stays byte-identical.** `log.bind(sink)` where `sink.publish(level, category, message, **meta)` — signature unchanged. Business code sees zero change. The bound sink becomes a thin `LogToBusAdapter` that builds a `Message` and calls `bus.publish`.

8. **`EventStore` remains the sole SQLite write point.** Verified in code: only `insert_local` (`storage.py:65`) and `insert_remote` (`storage.py:105`) INSERT; `mark_read` (line 254) is the only other mutation, already store-owned. No new write path is introduced. Tester's `CountingEventStore` spy subclassing those two methods catches 100% of writes.

9. **Phase 0 = build the harness + freeze the invariants BEFORE any production code moves.** Non-negotiable, comes first: `tests/unit/_bus_harness.py` (2/3-node in-process loopback) + `tests/unit/test_message_bus_invariants.py` (frozen, append-only) + `docs/bus-migration-map.md` (old-test → invariant mapping).

10. **Backpressure is per-subscriber, own queue, never a shared queue.** Durable subscribers never drop; ephemeral subscribers may drop-on-full. The coordinator *registering* the subscriber picks the policy, not the bus core. `settle()` in tests polls observable conditions, never bare `sleep(0.05)`.

---

## 2. Resolved disagreements

Three disagreements existed after Round 1. All three resolved in Round 2. Recorded here as binding:

### 2.1 Big-bang vs phased → **full-unification end-state, executed as revertable phases; wire-frame flip is Phase 7 / LAST and NOT optional**

- **Engineer withdrew** the "big-bang is wrong" objection (R2 Divergence 1): full unification executed as N revertable phases, each green, is the *same destination* with a rollback seam at each step — not a half-measure.
- **Architect took the position** (R2 Disagreement 1) that "big-bang" means *reach the full end-state, don't ossify a half-merged hybrid* — orthogonal to commit granularity. The real betrayal would be shipping Phase A (PeerTransport) and declaring victory.
- **BINDING RESOLUTION:** Commit to the full end-state as a contract — **all phases land, in one PR series, no indefinite pause at any phase.** Phasing is the *safety envelope inside a big-bang mandate*, not an alternative to it. **The wire-frame unification (Phase 7) MUST land — it is sequenced last and version-gated, but it is NOT optional.** Deferring it permanently leaves TWO frame vocabularies on the wire, which is exactly the copy-paste we are deleting. BA is a single-owner personal network (`git pull && restart` across 3–4 boxes); the mixed-version window is minutes, self-inflicted, owner-controlled. No soak needed at personal scale, but a mixed-version test gate is required (see Phase 7).

### 2.2 Class structure → **sibling coordinators over shared transport (architect CONCEDED)**

- Engineer (R1 §2b) challenged the architect to "show the pseudo-code where broadcast+cursor+debounce and subscribe+refcount+relay share a branch — otherwise concede."
- **Architect conceded** (R2 Disagreement 2): tried to write the shared branch; every line's body is a policy fork with **no shared statements** (`peers_for` differs: broadcast-set vs `route()`-one; `buffer_or_send` differs: debounce-batch vs immediate-pump; `persist_or_deliver` differs: store+gossip vs queue+relay). A strategy class here is "two method bodies with a shared signature and zero shared code — two classes wearing a trenchcoat."
- **BINDING RESOLUTION:** `EventReplicator` + `ChatReplicator` are **siblings composing a shared `PeerTransport`**, NOT one strategy class, NOT base+subclass. The unification is real and lives at the **bus API + transport + local-link + wiring + wire-frame + dispatch** layers (all singular). The replication *policy* stays two named coordinators because there is no single replication algorithm to have.

### 2.3 Envelope durability field → **no `origin_seq` overload, store stays synchronous (architect CONCEDED)**

- Architect's R1 proposal `origin_seq==0 ⇒ ephemeral, >0 ⇒ durable` overloads a payload field to carry routing policy.
- **Architect conceded** (R2 Disagreement 3): durability is NOT an envelope field and NOT inferred from `origin_seq`. Tester **withdrew** their R1 "explicit `durable` field" preference (R2 D1) — promoting durability into the envelope re-couples the bus to event semantics. Both landed on the **subscriber-list-is-the-policy** model (architect R2 "New insight"): the durable/ephemeral decision is *which subscribers are registered for the topic*, not a bool on the wire.
- **BINDING RESOLUTION:** `origin_seq` lives in `payload`, minted by the store-subscriber, **never read by the bus core**. The store stays privileged-synchronous (enrich-then-fan-out). Durability is the subscriber-list fact from consensus point 6.

---

## 3. Target architecture (concrete)

### 3.1 The envelope

```python
# bus/message.py
@dataclass(frozen=True)
class Message:
    topic: str          # "events.<category>" | "chat.<machine_id>.<bot>.<chat_id>"
    payload: dict       # opaque to the bus — NEVER inspected by the core
    ts: float           # set by bus if caller omits (WebChannel already does this)
```

Nothing else. `origin_machine`, `origin_seq`, `id`, `level`, `bot`, `category`, `read_at` are all **payload fields the durable-topic store-subscriber understands** — the bus never reads them.

Topic scheme:
```
events.<category>                              e.g. events.scheduler.run, events.cluster.host.rpc_fail
chat.<machine_id>.<bot>.<chat_id>              e.g. chat.win-mini.assistant.web-42
```
Prefix subscription supported (`events.` matches all event topics; `events.scheduler.` matches one subtree) — replaces both `EventStreamSubscriber._matches` category-prefix filtering and `(bot, chat_id)` queue keying. `level` is filtered by the subscriber *after* topic match (payload field, not topic segment), matching today's `EventStreamSubscriber.levels`.

### 3.2 The `MessageBus` API (content-agnostic, ~50 lines)

```python
# bus/core.py
class MessageBus:
    def subscribe(self, topic_pattern: str, subscriber: "Subscriber") -> "Subscription": ...
        # topic_pattern: exact "chat.m.b.c" OR prefix "events." / "events.scheduler."
        # returns Subscription with .close()  — replaces ad-hoc unsubscribe(callback)/(chat_id,queue)

    def publish(self, topic: str, payload: dict, ts: float | None = None) -> None: ...
        # SYNCHRONOUS local fan-out, ordered for-loop over matching subscribers.
        # For durable topics the first-slot StoreSubscriber runs first and enriches
        # payload (id/origin_seq) before the remaining subscribers see it.
        # NO create_task per message — 坑 #1. All async lives in RemoteSubscriber's pump.

    def attach_link(self, peer_key: str, transport: "PeerTransport") -> None: ...
    def detach_link(self, peer_key: str) -> None: ...
```

`publish` is **sync** because `log.info(...)` is called from sync business code everywhere and must not require `await`. The single enforced rule (engineer R2 "New insight"): *local fan-out is always a synchronous ordered for-loop; all async lives in `RemoteSubscriber`'s single pump task.* This one rule simultaneously guards 坑 #1 (ordering), `/events` pagination (id at fan-out time), and cross-machine dedup (origin_seq at fan-out time).

### 3.3 The `PeerTransport` interface (extracted shared link, ~50 lines)

```python
# cluster/peer_transport.py
class PeerTransport:
    def attach_peer(self, peer_key: str, send_frame: SendFrame) -> None: ...
    def detach_peer(self, peer_key: str) -> None: ...
    async def send_to(self, peer_key: str, frame: dict) -> None: ...    # was _send_to, dedup'd from 2 copies
    async def handle_frame(self, peer_key: str, frame: dict) -> bool: ...  # TOPIC-routed dispatch, returns consumed?
```

`send_to` is byte-for-byte identical between `EventSyncer._send_to` (`sync.py:227-234`) and `ChatSyncer._send_to` (`chat_sync.py:178-185`) today except the log prefix. `handle_frame` becomes **one topic-routed dispatch** — this eliminates the fragile event-then-chat wiring chain: one `on_unknown_frame` owner, no chaining, no install-order constraint. `machine_id` naming unified (kill the `local_machine`/`machine`/`machine_id` drift; `mid` is the cited footgun).

### 3.4 The Subscriber abstraction (~40 lines, shared)

```python
# bus/subscriber.py
class Subscriber(Protocol):
    def deliver(self, message: Message) -> None: ...   # SYNC entry (put_nowait or direct callback)

class LocalSubscriber:     # wraps asyncio.Queue; put_nowait; drop-on-full with logger.warning
    # browser SSE, telegram notifier, /events page, web chat clients

class RemoteSubscriber:    # bounded queue(maxsize=1024) + ONE pump task -> await transport.send_to(peer, frame)
    # forwards over a cluster WS peer; per-topic pump preserves order (坑 #1);
    # per-peer backpressure by bounding the queue
```

A queue subscriber is just a callback that does `queue.put_nowait` — this is why the events side (which has non-queue callback subscribers: store-writer, notifier) and the chat side (queue-only) both fit one abstraction.

### 3.5 Where the coordinators sit and what they subscribe to

| Component | Package | Subscribes to | Owns |
|---|---|---|---|
| **StoreSubscriber** | `events/` | `events.` prefix, registered **FIRST + synchronous** | `EventStore`; mints `id`+`origin_seq` on local publish (`insert_local`); `insert_remote` (INSERT OR IGNORE dedup) on remote durable arrival; keeps `Event` as a read-path payload-view (`/api/events` query stays `store.query`) |
| **EventReplicator** (was EventSyncer) | `cluster/` | `events.` prefix | `PeerTransport`; **broadcast pattern** — debounce 200ms, batch (MAX_BATCH), send to **all** peers, gossip onward with origin dedup, cursor resync-on-attack. Verbatim `EventSyncer` policy. |
| **ChatReplicator** (was ChatSyncer) | `cluster/` | `chat.` prefix | `PeerTransport`; **demand pattern** — refcount `(machine, bot, chat)` subscriptions, `route()` to one peer, two-hop host relay, owner-side demand edge (`on_local_demand`). Verbatim `ChatSyncer` policy. |
| **TelegramNotifier** | `events/` | `events.` prefix (level/prefix filtered) | unchanged; `create_task(deliver)` per matching event, no rate limit (explicit design) |
| **EventStreamSubscriber** | `events/` | `events.` prefix | `/events` SSE; `LocalSubscriber` over a queue; unchanged filter (level/machine/bot/category_prefix) |
| **Local web chat queues** | `transports/web/` | `chat.<self>.<bot>.<chat_id>` exact | per-chat SSE queue fan-out |

**Durability by construction:** `StoreSubscriber` subscribes `events.` only. It is not in `chat.*`'s subscriber list. stream_delta at hundreds/sec physically cannot reach SQLite — no runtime check, a wiring fact.

### 3.6 Module / package layout — the key dependency decision

**New neutral `bus/` package that `events/` and `cluster/` both depend on; neither depends on the other.**

```
bus/                         NEW — neutral leaf core, depends on NOTHING in the project
  message.py                 Message dataclass
  core.py                    MessageBus (local fan-out + link registry)
  subscriber.py              Subscriber Protocol + LocalSubscriber + RemoteSubscriber

events/                      becomes "the durable-broadcast policy over the bus"
  storage.py                 EventStore (internals UNCHANGED — sole SQLite writer)
  store_subscriber.py        StoreSubscriber (was EventBus.publish store-write + insert_remote)  NEW
  models.py                  Event (now a payload-view, read path only)
  retention.py               RetentionSweeper (subscriber, ~unchanged)
  telegram_notifier.py       TelegramNotifier (subscriber, ~unchanged)
  web_stream.py              EventStreamSubscriber (subscriber, ~unchanged)

cluster/
  peer_transport.py          PeerTransport (extracted shared link)  NEW
  event_replicator.py        EventReplicator (was EventSyncer) — broadcast+cursor+gossip policy
  chat_replicator.py         ChatReplicator (was ChatSyncer)   — demand+refcount+relay policy
  bus_wiring.py              ONE wiring: attach PeerTransport per peer, topic-routed frames  NEW
                             (replaces events/sync_wiring.py + cluster/chat_sync_wiring.py chain)

log/                         facade UNCHANGED — log.bind() binds a thin LogToBusAdapter that turns
                             publish(level, category, message, **meta) into
                             bus.publish(f"events.{category}", {"level":..,"message":..,"bot":..,"meta":..})
```

**Dependency direction:** `bus/` is a leaf importing nothing project-internal. `events/` depends on `bus/`. `cluster/{event,chat}_replicator.py` depend on `bus/`. **Neither `events/` nor `cluster/` depends on the other.** This is a clean fan-in. Rationale (architect R1 §2.4): if the bus lived in `events/`, then `cluster/` (chat, a cluster concern) would import `events/` (a logging concern) — semantically backwards, and it recreates today's ugliest coupling.

### 3.7 How `boxagent.log` stays byte-identical

```python
class LogToBusAdapter:                 # ~10 lines, the bound sink
    def publish(self, level, category, message, **meta):
        bot = meta.pop("bot", None)
        self._bus.publish(
            f"events.{category}",
            {"level": level, "message": message, "bot": bot, "meta": meta},
        )
```

`log.bind(sink)` signature preserved (`sink.publish(level, category, message, **meta)`). Business code — which only imports `boxagent.log` and never `boxagent.events` — sees zero change. `/events` page, notifier, sync, retention all keep working because they become bus subscribers reading the same payloads. Verified against INV-G1/G2/G3/G4 (facade + `/api/events` + notifier + retention frozen).

### 3.8 The wire-frame unification (Phase 7 / LAST)

Today two frame vocabularies coexist on one WS: `event_batch`/`event_resync` (events) and `chat_subscribe`/`chat_unsubscribe`/`chat_event` (chat). The two `handle_frame`s chain via `on_unknown_frame` fall-through, which is why `chat_sync_wiring` **must** install after `sync_wiring`.

**Final frame (Phase 7):** a single topic-addressed frame replaces both:
```python
{"v": 2, "topic": "<topic>", "payload": {...}, "ts": <float>}   # + policy-specific control frames
```
routed by **one** topic-prefixed `handle_frame` dispatch inside `PeerTransport` (`events.` → EventReplicator, `chat.` → ChatReplicator). Control frames that are not topic-addressed message deliveries (event batch envelopes, chat subscribe/unsubscribe/resync requests) keep a typed discriminator but ride the unified `v:2` framing. The `v` byte is the mixed-version gate: a `v:1` node receiving a `v:2` frame it can't parse must **drop gracefully, not crash** (Phase 7 gate). Owner restarts all 3–4 nodes together; the mixed window is minutes.

---

## 4. The phased migration plan

Ordered phases synthesizing the engineer's P0–P8 (R2) with the tester's per-phase gates (R2 §gate table). **Every phase gate = (full frozen invariant set green) + (that phase's named no-go green) + (`uv run pytest -x -q` passed ≥ 886).** The frozen invariant set never shrinks; each phase merely *adds* the invariant its new code path first makes reachable. Nothing is omitted; Phase 7 is *sequenced* last, not skipped.

Baseline note: current bus-related tests = 126 passed; full suite floor = **886** (HARD CONSTRAINT #4 — never drops). Net test count must *rise* (bus core gets its own tests).

---

### Phase 0 — Harness + frozen invariants + migration map. **No product code.**

- **Changes:** Build `tests/unit/_bus_harness.py` — a `TwoNodeCluster` / `ThreeNodeCluster` with real store + real bus + real replicator per node, linked by a bidirectional in-memory `_wire_pair`-style shuttle (generalizes `test_event_syncer.py::_wire_pair` and `test_chat_sync.py::_make`). Exposes `publish_event` (via real `boxagent.log` facade), `publish_chat` (via real `WebChannel._publish`), `subscribe_chat`, `store_rows`, `link()`/`drop_link()`/`relink()`, and `settle()` (polls observable conditions: buffer empty + no pending flush task + target queue reached expected count — **never bare `sleep`**). Ship the `reorder_tasks` injection hook (randomly permutes pending-task completion order during a publish burst). Write `tests/unit/test_message_bus_invariants.py` (frozen, append-only — INV-A1..G5 from tester R1 §2.2, phrased through the harness, reading ONLY `store_rows` / queue contents / captured frames — zero references to `EventSyncer`/`ChatSyncer`/`_subscribers`/`_pumps`/`_buffer`). Write `docs/bus-migration-map.md` (one row per existing bus test → behavior → covering invariant → delete-old-when).
- **Why reversible:** pure test addition; delete the files.
- **GATE (named no-go):** harness self-test green (`test_bus_harness.py::test_link_delivers_frame_both_directions`, `test_settle_waits_for_debounce`); **ALL invariants green against the OLD code** (proves they describe real existing behavior, not fiction — if any INV reds under old code, fix the *test* not the code); **`reorder_tasks` proven RED against a deliberately-broken per-event-`create_task` stub replicator** (a footgun guard you never saw fail is not a guard); `CountingEventStore` spy confirmed to catch both write points. `pytest ≥ 886`, no product code touched.

### Phase 1 — Extract `PeerTransport`.

- **Changes:** Extract shared `_peers` / `send_to` / `attach_peer` / `detach_peer` / `handle_frame` dispatch into `cluster/peer_transport.py`. Both `EventSyncer` and `ChatSyncer` delegate to it. Behavior identical. Wire frames and `log.bind` untouched. Banks the biggest real dedup (~40 logic + ~120 wiring lines) at near-zero risk.
- **Why reversible:** inline it back into the two syncers.
- **GATE (named no-go):** INV-B* (cross-machine event) and INV-C* (cross-machine chat) unchanged-green; all 126 old bus tests still green (pure delegation, no behavior change).

### Phase 2 — Land `bus/` core, unused.

- **Changes:** Land `bus/message.py` + `bus/core.py` + `bus/subscriber.py` (`Message`, `MessageBus`, `Subscriber`, `LocalSubscriber`, `RemoteSubscriber`) with their own unit tests. Nothing wired to it yet.
- **Why reversible:** delete the `bus/` package.
- **GATE (named no-go):** bus-core unit tests green (envelope roundtrip, local queue delivery, topic prefix routing, drop-on-full); full frozen invariant set green (old paths untouched); `pytest` only increases.

### Phase 3 — `StoreSubscriber` behind existing `EventBus`. **[touches store path]**

- **Changes:** Refactor `EventBus.publish` to delegate store-write to a `StoreSubscriber` object, still called **synchronously, first**, from `EventBus`. Extract to `events/store_subscriber.py`. No external behavior change; byte-identical `store.query` rows.
- **Why reversible:** inline `StoreSubscriber` back into `EventBus.publish` (one file).
- **GATE (named no-go = INV-A1):** **INV-A1 — the row is written before `publish` returns.** If the store went async, INV-A1 reds → no-go. Plus INV-A3 (durable-topic param table), INV-B* (event replication), INV-E1/E3 (order), INV-G1/G2/G3/G4/G5 (facade + `/api/events` + notifier + retention + subscriber-exception-isolation).

### Phase 4 — `EventBus` → thin adapter over `MessageBus`. **[MIDDLE — live `log.bind` pipeline, SHIMMED, never a raw swap]**

- **Changes:** `EventBus.publish` now builds a `Message` and calls `bus.publish`; `StoreSubscriber` + `EventStreamSubscriber` + `TelegramNotifier` + `EventSyncer` re-subscribe via the bus. **`log.bind` still binds `EventBus`** (which internally delegates) — the raw `log.bind(LogToBusAdapter)` swap is deferred to Phase 8. `/api/events` read path unchanged (still `store.query`).
- **Why reversible:** revert the one adapter file; `EventBus` internals restored.
- **GATE (named no-go = INV-A1 + ordering):** same SQLite rows (INV-A1 byte-identical via `store.query`), same SSE payloads, same notifier calls (MockChannel-style), INV-E1/E3 (order preserved through the new fan-out). This is the load-bearing internal phase; the `log.bind` pipeline is never red because `EventBus` stays bound and delegates.

### Phase 5 — Chat rides the same bus (`chat.*` ephemeral topics). **[INV-A2 is the head no-go]**

- **Changes:** `WebChannel._publish` → `bus.publish(topic, event)`; `ChatBus` subscribe → `bus.subscribe(topic, LocalSubscriber(queue))`. Demand refcount, owner-side pump lifecycle (`on_local_demand`), and two-hop relay routing **preserved** (this is where 坑 #1 lives). `chat.*` topics registered ephemeral (no `StoreSubscriber`).
- **Why reversible:** revert the one channel/chat_bus adapter file.
- **GATE (named no-go = INV-A2 + INV-D3):** **INV-A2 — 200 chat `stream_delta` publishes → `CountingEventStore` insert-delta == 0 AND before/after store-snapshot equality** (chat never hits SQLite). INV-D3 (cross-machine chat also never hits store). Plus INV-C* (subscribe/relay/refcount), INV-E2 (chat delta order), INV-F1 (backpressure isolation: slow subscriber drops its own, doesn't stall others).

### Phase 6 — Merge syncers into `EventReplicator` + `ChatReplicator` siblings; ONE `bus_wiring.py`.

- **Changes:** `EventSyncer` → `cluster/event_replicator.py`, `ChatSyncer` → `cluster/chat_replicator.py`, both composing `PeerTransport`. Delete `events/sync_wiring.py` + `cluster/chat_sync_wiring.py` chaining; install one `PeerTransport` per peer via one `bus_wiring.py`; drop the event-then-chat install-order constraint.
- **Why reversible:** the two old wiring modules stay in git; revert is one commit.
- **GATE (named no-go):** INV-B* and INV-C* all green through the merged coordinators; INV-D2 (event and chat frames don't swallow each other on one WS — behavioral, not chain-implementation); both reconnect behaviors preserved (event cursor-resync AND chat re-subscribe — INV-B4 + INV-C6).

### Phase 7 — Unify the wire frame → topic-addressed `{v, topic, payload, ts}`. **[LAST — cross-machine wire contract]**

- **Changes:** Flip both `event_batch`/`event_resync` and `chat_subscribe`/`chat_event` frames to the single `v:2` topic-addressed frame + typed control frames, dispatched by one topic-routed `handle_frame`. Version-gated (`v` byte): a `v:1` node drops an unparseable `v:2` frame gracefully. Owner restarts all nodes together.
- **Why reversible:** the version gate is the seam — revert the frame flip, `v:1` frames stay valid; one commit.
- **GATE (named no-go = INV-D1 + mixed-version):** **INV-D1 — ONE reconnect test (ThreeNodeCluster): drop link → A emits 2 events + 3 chat deltas → relink → settle → assert (a) B.store backfilled the 2 events via cursor AND (b) B's chat queue resumes receiving *new* deltas via re-sent subscribe; the 3 dropped deltas allowed lost (chat is live, no backlog).** Event-resync and chat-re-subscribe verified in the *same* reconnect so the merge cannot silently drop one. INV-D2 (frames don't swallow each other). **NEW mixed-version test:** node A speaks `v:2`, node B still speaks `v:1` on one link → assert no crash, graceful ignore. This is the one gate that can't be a pure in-process invariant — the harness runs two frame vocabularies on one link.

### Phase 8 — Drop the `EventBus` shim; `log.bind(LogToBusAdapter)`. Rewrite white-box tests.

- **Changes:** Remove the `EventBus` compatibility layer; `log.bind` now binds the 10-line `LogToBusAdapter` directly (byte-identical SQLite rows, asserted via `store.query`). Rewrite white-box tests (`test_event_bus.py` asserting `bus._subscribers`) to the new black-box boundary. Migrate — **never delete** — the behavior of `test_event_syncer.py` (12) + `test_chat_sync.py` (16) + `test_chat_bus.py` (6) into the frozen invariant layer per `docs/bus-migration-map.md`.
- **Why reversible:** revert the one `log.bind` line; `EventBus` shim restored from git.
- **GATE (named no-go):** every deletable old-test row in `bus-migration-map.md` points at a *green* invariant (no row → behavior silently dropped → block); full frozen invariant set green; `pytest ≥ 886` with net count **up**.

---

## 5. Acceptance criteria (graded against the whole effort)

1. **One bus, five singular things.** Exactly one publish/subscribe API, one `PeerTransport` link, one `bus_wiring.py`, one topic-routed `handle_frame` dispatch, and one wire frame (`v:2` topic-addressed) — verifiable by `grep`: `sync_wiring.py`, `chat_sync_wiring.py`, `event_batch`/`chat_subscribe` frame vocabularies, and the copy-pasted `_send_to` are all gone.

2. **stream_delta never touches SQLite.** INV-A2 green: 200 chat `stream_delta` publishes produce `CountingEventStore` insert-delta == 0 and before/after store-snapshot equality, across both local and cross-machine paths (INV-A2 + INV-D3).

3. **Event durability + ordering preserved.** INV-A1 (row written before `publish` returns), INV-B1..B8 (propagation, bidirectional, dedup, cursor resync, gossip, 3-day window, detach, send-failure-swallow), INV-E1/E2/E3 (100+ events and 100+ deltas in strict order across debounce/flush boundaries) all green.

4. **Reconnect recovers BOTH halves in one event.** INV-D1 green: a single reconnect backfills events via cursor AND re-sends chat subscribes; the merge cannot silently recover only one.

5. **Facade + all existing exits byte-identical.** `boxagent.log` signature unchanged; `/api/events` query/pagination/filter/mark_read (INV-G2), TelegramNotifier (INV-G3), retention sweeper (INV-G4), and subscriber-exception isolation (INV-G5) all frozen-green. Business code diff touching `boxagent.events` imports == 0.

6. **Clean dependency fan-in.** `bus/` imports nothing project-internal; `events/` and `cluster/` both depend on `bus/`; `events/`↔`cluster/` mutual import == 0 (verifiable by import-graph check).

7. **Test baseline only rises.** `uv run pytest -x -q` passed ≥ 886 at the end of *every* phase; final count strictly greater than the start (bus core adds its own tests); no bus behavior deleted without a green invariant row in `bus-migration-map.md`.

---

## 6. Open risks (residual even with phasing) + mitigations

**R1 — Mixed-version cluster window during Phase 7.** Flipping the wire frame means a `v:1` node and a `v:2` node briefly coexist during `git pull && restart` across 3–4 boxes.
*Mitigation:* the `v` version byte + the Phase-7 mixed-version gate test (node A `v:2`, node B `v:1` on one link → graceful drop, no crash). Owner restarts all nodes together; window is minutes, self-inflicted, owner-controlled. Phase 7 is version-gated and revertable (revert the flip, `v:1` frames stay valid). At personal scale no soak is required, but the mixed-version test **is** required to land.

**R2 — The live `log.bind` swap (Phase 4 + Phase 8).** `log.bind` fires at gateway line ~161 before almost everything; a window where `log.info` no-ops or double-writes is catastrophic and invisible.
*Mitigation:* never a raw swap. Phase 4 keeps `EventBus` bound and delegating internally (pipeline never red). The raw `log.bind(LogToBusAdapter)` swap is deferred to Phase 8, gated on byte-identical `store.query` rows asserted before and after. Both phases revert via one file/one line.

**R3 — Ordering footgun (`create_task`-per-message reintroduces 坑 #1).** Merging the two forwarders risks someone "simplifying" the single-pump to per-message tasks, silently reordering stream_delta (garbled UI text) or event batches.
*Mitigation:* the single enforced rule — *local fan-out is always a synchronous ordered for-loop; all async lives in `RemoteSubscriber`'s one pump task.* Guarded by INV-E1/E2/E3 (100+ items across flush boundaries) — and critically, the `reorder_tasks` injection hook is **proven RED in Phase 0 against a deliberately-broken per-event-`create_task` stub** before it is trusted against real code. A footgun guard never seen to fail is not a guard.

**R4 — Backpressure isolation (one slow subscriber stalls the bus).** If unification accidentally shares one queue across topics/subscribers, or flips `put_nowait` to blocking `await queue.put()`, a stuck browser SSE client backpressures the whole bot.
*Mitigation:* subscription == its own bounded queue, never shared; durable subscribers never drop, ephemeral drop-on-full; the *registering coordinator* picks the policy, not the bus core. Guarded by INV-F1 (slow subscriber drops its own traffic, fast subscriber unaffected) and INV-F2 (`/events` SSE queue full → warn + drop, store and other subscribers unaffected).

**R5 (secondary) — `origin_seq` ownership drift / third write path.** A reviewer "tidying" `origin_seq` minting into the envelope constructor breaks cross-machine dedup; a new SQLite write path defeats the `CountingEventStore` spy and falsely-greens INV-A2.
*Mitigation:* `origin_seq` minting stays in `StoreSubscriber`/`EventStore`, documented as load-bearing; `EventStore` confirmed sole SQLite writer (`insert_local`/`insert_remote` only). Any phase adding a second write path is a red flag to stop on (Phase-5 precondition).

**R6 (secondary) — Cross-thread publish / threading boundary.** `web_stream.py` uses `call_soon_threadsafe` and `EventStore` holds a `threading.Lock`, implying event publish may come from a non-event-loop thread while chat publish comes from the loop. Sharing one bus complicates the thread-safety boundary.
*Mitigation:* confirm the input thread model at Phase 4; if any cross-thread publish entry survives, add a concurrency invariant (multi-thread `publish_event` + loop-thread `publish_chat` → store uncorrupted, no seq conflict, no deadlock). If all publish is single-loop-thread post-unification, this risk closes.

---

## 7. Through-line (one paragraph)

Unify the **API + transport + local-link + wiring + wire-frame + dispatch** (real unification — deletes the copy-pasted skeleton and the fragile dual chained wiring). Keep **two replication coordinators** (`EventReplicator` broadcast+cursor+gossip, `ChatReplicator` demand+refcount+relay) as siblings over a shared `PeerTransport` — no shared replication algorithm exists. Envelope is `{topic, payload, ts}`; durability is a per-topic **subscriber-list fact**, not an envelope field; the store is a **privileged synchronous first-slot subscriber** and the sole SQLite writer. Execute as **9 revertable phases (P0–P8) toward the full end-state — all phases land, Phase 7 (wire-frame flip) included and version-gated; phasing is the safety envelope, not an exit ramp.** Every phase gate = frozen invariants green + phase-specific named no-go green + `pytest ≥ 886`.
