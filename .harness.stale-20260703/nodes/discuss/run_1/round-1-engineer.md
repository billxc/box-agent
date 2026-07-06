# Round 1 — Engineer Analysis: Big-bang unification into ONE MessageBus

Verdict up front: **The unification is feasible and the target model is sound, but "one class with a strategy" over EventSyncer+ChatSyncer is the WRONG merge.** The genuinely shared surface is ~40 lines (peer registry + WS send + frame dispatch). The divergent surface is ~350 lines and the two halves share almost no logic. I recommend a small shared **transport/peer layer** + keeping the two "sync policies" as separate subscriber-side objects, unified only at the *bus API* level (single `publish(topic, envelope)` / `subscribe(topic, subscriber)`), not at the *replication-strategy* level. And I'd do it **incrementally, behind the existing façades**, not as one big-bang cutover — reasons in §3.

---

## 1. Key observations

### 1a. The four "buses" are not four instances of one thing. They're two different *transport* patterns plus two different *replication policies*.

Reading the code line by line, the real axes are:

| | LOCAL fan-out | REMOTE replication |
|---|---|---|
| **events** | `EventBus._publish` — synchronous, ordered, store-first (`bus.py:41-61`) | `EventSyncer` — **broadcast/full**: debounce 200ms, cursor+origin_seq resync, gossip to all peers (`sync.py`) |
| **chat** | `WebChannel._publish` — synchronous per-chat queue fan-out (`channel.py:64-70`) | `ChatSyncer` — **subscribe/demand**: refcount per `(machine,bot,chat)`, two-hop host relay, no store (`chat_sync.py`) |

The owner's target model ("LOCAL subscriber → in-process queue; REMOTE subscriber → cluster WS; same protocol, only the link differs") is exactly right as a **framing** of the local half. But the remote half is where the two diverge hard, and that divergence is **essential, not incidental**:

- **events want full replication** (every node holds every event; the /events page and retention sweeper query a *local complete* SQLite). Resync-on-connect via cursors is load-bearing — a node that was offline must catch up.
- **chat wants demand-driven subscription** (only the viewing node pulls; stream_delta at hundreds/sec must never be stored *or* broadcast to nodes nobody is watching from). Refcount + `_refresh_source` edge detection is load-bearing.

You cannot collapse "broadcast everything + debounce + cursor resync" and "subscribe on demand + refcount + relay" into one code path without a big `if policy == ...` fork. That fork *is* the two classes. A strategy object whose two implementations share no branches is just two classes wearing a trenchcoat.

### 1b. What is *genuinely* shared (measured against the actual files)

Common to EventSyncer and ChatSyncer:

- `self._peers: dict[str, SendFrame]` + `attach_peer` / `detach_peer` — **identical shape** (`sync.py:88,95-105` vs `chat_sync.py:36,52-72`). Chat's detach does extra source-refcount cleanup; event's is a plain pop.
- `_send_to(peer_key, frame)` — **byte-for-byte identical** except the log prefix (`sync.py:227-234` vs `chat_sync.py:178-185`).
- `handle_frame(peer_key, payload) -> bool` dispatch-by-`payload["type"]`, returning True-if-consumed — **same contract** (`sync.py:119-128` vs `chat_sync.py:99-112`). This is what lets both chain onto `on_unknown_frame`.
- The wiring modules (`sync_wiring.py` 46 lines, `chat_sync_wiring.py` 82 lines) are near-duplicates: both build a `send_frame` closure over `session.ws.send_json` / `client._ws.send_json`, attach on connect, detach on disconnect, route `on_unknown_frame`. Chat's is longer only because it must *chain* (event installs first, unchained). This chaining is fragile (see §3) and is a symptom of two things fighting over the same three callback slots.

**Shared, rough line count: ~40 lines of logic** (`_peers` dict, `_send_to`, `attach/detach` skeleton, `handle_frame` dispatch skeleton) — plus ~120 lines of wiring boilerplate that is 80% duplicated.

### 1c. What is genuinely divergent (measured)

- **EventSyncer only** (~200 lines): `event_to_dict`/`event_from_dict`, window filter, `_on_local_event` buffer+`_schedule_flush`, `_flush_after_debounce`, `_flush` (batch slicing to MAX_BATCH), `_handle_batch` (insert_remote + gossip-to-others), `_handle_resync` (cursor diff against store). Every one of these touches the **store**. None has a chat analogue.
- **ChatSyncer only** (~180 lines): `_queues` + `_downstream` dual-index, `remote_subscribe`/`remote_unsubscribe`, `_deliver` (queue fan-out + downstream relay), `_refresh_source`/`_toggle_source` refcount edge detection, `on_local_demand` hook, `_route`/`_send_toward` two-hop routing. None has an event analogue.

So the honest split is roughly **40 shared : 380 divergent**. The shared part is real but small, and it's the *transport plumbing*, not the *bus semantics*.

### 1d. The synchronous store-then-fanout is compatible with "EventStore as a subscriber" — but only if you don't reorder it

This is the subtle one, and it's the crux of the owner's question. Today `EventBus.publish` (`bus.py:47-60`) does:

1. `event = store.insert_local(...)`  ← assigns `id` and `origin_seq`, **synchronously**
2. `for callback in subscribers: callback(event)`  ← fan-out, **synchronously, in order**

Two subscribers depend on step 1 having *already happened* when they run:
- `EventStreamSubscriber` (/events SSE) forwards the `Event` with its `id` — the frontend uses `id` for `before_id` pagination cursors (`storage.py:194`). No id → broken pagination.
- `EventSyncer._on_local_event` reads `event.origin_seq` (`sync.py:190,192`) to build the cross-cluster key. No seq → no dedup.

If you naively make the store "just another subscriber to the events topic", you get **fan-out to N subscribers where one of them (the store) is the one that mints the id/seq the other N-1 need**. That breaks unless the store subscriber is *privileged*: runs first, synchronously, and its output (the enriched envelope) is what the remaining subscribers receive.

**This is fine and even clean** — but it means the bus core must support a notion of an **ordered, synchronous, envelope-transforming first subscriber** (or, equivalently, keep insert as a pre-fanout step for durable topics). It does NOT mean "the store is a peer of the SSE subscriber." The moment you make the store an async/unordered subscriber, /events pagination and cross-machine dedup both break, *and* you've reintroduced 坑 #1 (ordering) by another door. Keep `publish` synchronous. Persistence-first is a **per-topic policy** ("durable topics enrich-then-fanout; ephemeral topics fanout-only"), exactly as the constraints demand.

### 1e. Ordering & 坑 #1 — the current design is correct; don't lose it

- `EventBus._publish` fan-out is a plain `for` loop — synchronous, ordered. Good.
- `EventSyncer` buffers in `_on_local_event` (sync) and flushes in ONE debounce task; per-peer send is an ordered `for`. Good — no create_task-per-event.
- `ChatBus._pump` is the canonical fix for 坑 #1: **one task per `(bot,chat)` reading the WebChannel queue sequentially** and calling `on_local_publish` — never create_task-per-event (`chat_bus.py:68-82`). This is the pattern any unified remote-forward MUST preserve.

Note the asymmetry that a merge must respect: **events buffer+debounce+batch** (throughput, don't care about 200ms latency); **chat pumps 1:1 immediately** (latency, stream_delta must feel live). A unified `publish` cannot impose one flushing policy on both.

### 1f. Smallest envelope that works

Chat events are already plain dicts with a `type` discriminator (`message`/`stream_delta`/`tool_call`/…). Events are a frozen dataclass with 9 fields, most of which (`origin_seq`, `read_at`, `id`) are **durability bookkeeping the chat side never has**. The smallest *common* envelope is basically:

```
Envelope = {
  "topic": str,          # "events" | "chat.<machine>.<bot>.<chat_id>"
  "payload": dict,       # opaque to the bus
  "ts": float,           # set by bus if absent (WebChannel already does this)
}
```

Everything else (`origin_machine`, `origin_seq`, `id`, `level`, `category`, `bot`) is **payload the durable-topic store-subscriber understands**, not bus-level fields. The bus must stay content-agnostic (hard constraint: "bus doesn't know event vs chat"). So: **envelope = topic + opaque payload + ts. Full stop.** Do NOT promote `origin_seq`/`level`/`bot` into the envelope — that re-couples the bus to event semantics and the `Event` dataclass, which is exactly what we're trying to undo.

---

## 2. Proposed approach with rationale

### 2a. The local-link vs remote-link abstraction (the interface)

The one abstraction that genuinely pays off is unifying "where does a subscriber live." A subscriber is just something that receives envelopes for a topic, in order:

```python
# A Subscriber is anything that consumes envelopes for a topic, in order.
class Subscriber(Protocol):
    async def deliver(self, envelope: Envelope) -> None: ...

# A local subscriber wraps an in-process queue (browser SSE, telegram notifier).
class LocalSubscriber:
    def __init__(self, queue: asyncio.Queue) -> None:
        self._queue = queue
    async def deliver(self, envelope: Envelope) -> None:
        try:
            self._queue.put_nowait(envelope)
        except asyncio.QueueFull:
            logger.warning("subscriber queue full (%s); dropping", envelope.topic)

# A remote subscriber forwards over a cluster WS peer. ONE pump per topic
# reads a bounded queue and sends sequentially — preserves per-topic order
# (坑 #1), applies per-peer backpressure by bounding the queue.
class RemoteSubscriber:
    def __init__(self, peer_key: str, send_frame: SendFrame) -> None:
        self._queue = asyncio.Queue(maxsize=1024)
        self._task = asyncio.create_task(self._pump(peer_key, send_frame))
    async def deliver(self, envelope: Envelope) -> None:
        self._queue.put_nowait(envelope)          # or drop-on-full
    async def _pump(self, peer_key, send_frame):
        while True:
            envelope = await self._queue.get()
            await send_frame({"topic": envelope.topic, "payload": envelope.payload,
                              "ts": envelope.ts})
```

The bus core is then genuinely tiny and content-agnostic:

```python
class MessageBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[Subscriber]] = {}
    def subscribe(self, topic: str, subscriber: Subscriber) -> None: ...
    def unsubscribe(self, topic: str, subscriber: Subscriber) -> None: ...
    async def publish(self, topic: str, payload: dict, ts: float | None = None) -> None:
        envelope = Envelope(topic, payload, ts or time.time())
        for subscriber in list(self._subscribers.get(topic, ())):  # ordered
            await subscriber.deliver(envelope)
```

**This is the part of the owner's model that is 100% right and worth building.** LocalSubscriber vs RemoteSubscriber is the "only the link differs" abstraction, in ~30 lines, and the RemoteSubscriber's single per-topic pump is the correct home for the 坑 #1 fix.

### 2b. Where I DIVERGE from the owner's framing: replication policy is NOT a subscriber, it's a *coordinator*

Here's the honest engineering point. The owner says "'every machine sees all events' = every node subscribes to peers' events topic; chat = only the viewing node subscribes." That's a beautiful mental model, but it hides three things the current EventSyncer does that a plain "subscribe to peer's topic" cannot:

1. **Resync/catch-up on connect** (`_handle_resync`, cursor diff) — a fresh subscription only gets *future* events. Events need the *backlog*. That's a store query, not a subscription.
2. **Gossip fan-out with origin dedup** (`_handle_batch` forwards to *other* peers, INSERT OR IGNORE) — multi-hop replication, not point-to-point.
3. **Debounced batching** (200ms, MAX_BATCH=500) — a per-event `publish→deliver→send` would be hundreds of tiny WS frames.

And chat needs its own three things a plain subscription can't express: **demand refcount** (`_refresh_source`), **two-hop host relay routing** (`_route`/`_send_toward`), and **owner-side pump lifecycle** (`on_local_demand`).

So my recommended structure is:

- **MessageBus** (~50 lines) — topic → subscribers, ordered sync/async publish. Content-agnostic. NEW.
- **Local/RemoteSubscriber** (~40 lines) — the link abstraction. NEW, shared.
- **PeerTransport** (~50 lines) — extract the shared `_peers` dict + `_send_to` + `attach_peer`/`detach_peer` + `handle_frame` dispatch skeleton that EventSyncer and ChatSyncer both have. NEW, shared. **This is the real dedup win.**
- **EventReplicator** (was EventSyncer, ~180 lines) — subscribes to the `events` topic as a durable subscriber; owns store, cursor, debounce, gossip. Uses PeerTransport. Keeps its distinct policy.
- **ChatReplicator** (was ChatSyncer, ~160 lines) — owns demand refcount, relay routing. Uses PeerTransport. Keeps its distinct policy.

i.e. **base transport + two policy coordinators**, NOT one strategy class. The two coordinators are siblings that *share a transport*, not subclasses of a common replication algorithm (there is no common algorithm).

Why not `EventReplicator(BaseReplicator)` + `ChatReplicator(BaseReplicator)` with template methods? Because the only shared methods are transport (already extracted into PeerTransport by composition) — a base class would have an empty body of shared *replication* logic and a swarm of `@abstractmethod`s. Composition (`self._transport = PeerTransport(...)`) beats inheritance here: no LSP contortions, each coordinator reads top-to-bottom.

### 2c. Store as a privileged durable subscriber (satisfies constraint 1 & 2)

Model the durable path as: the `events` topic has a **DurableStoreSubscriber registered first**, whose `deliver` does `insert_local`, enriches the envelope payload with `id`/`origin_seq`, and is the version downstream subscribers see. Concretely, keep `publish` for durable topics as *enrich-then-fanout* (what `EventBus.publish` already does), and mark chat topics *ephemeral* (fanout-only, never touches store). The "durable vs ephemeral" bit is a per-topic registration flag on the bus — the ONLY policy knob the bus core needs. This keeps stream_delta off SQLite (constraint 1) and keeps cursor/dedup in the store subscriber (constraint 2), while `boxagent.log` → `bus.publish("events", ...)` signature is unchanged (constraint 3).

---

## 3. Risks & concerns

### 3a. I would NOT do this big-bang. Recommend incremental behind façades.

The owner chose big-bang; my honest judgment is that's the higher-risk path here, for concrete reasons:

- **The blast radius is the two most operationally load-bearing subsystems**: cross-machine event sync (split-brain history, 坑 #8/#9 territory) and live chat (every browser + iOS client). A big-bang cutover means *both* replication policies, *both* wirings, *and* the WS frame protocol change in one commit. If resync regresses, offline nodes silently miss history; if the chat pump regresses, 坑 #1 (out-of-order) comes back. These failures are **hard to catch in unit tests and only show under multi-node load**.
- **The frame protocol is a wire contract between machines.** If envelope framing changes (`event_batch`/`chat_event` → `{topic,payload}`), a mixed-version cluster (one node upgraded, one not) breaks. Big-bang gives no rollback seam. Incremental lets you ship the new bus *internally* while keeping the old wire frames until every node is upgraded.
- **886-test baseline, black-box style.** The existing tests (`test_event_syncer.py`, `test_chat_sync.py`) assert on the *current* frame types and method names. A big-bang rewrite invalidates both suites at once — you lose your regression net exactly when you most need it.

**Safer incremental sequence** (each phase independently shippable + testable, keeps tests green):

- **Phase A — extract PeerTransport** (shared `_peers`/`_send_to`/`attach`/`detach`/`handle_frame`). ~1 day. Low risk. EventSyncer and ChatSyncer both delegate to it; behavior identical; existing tests still pass. **This banks the only large real dedup (~120 lines of wiring + ~40 logic) with near-zero risk.**
- **Phase B — introduce MessageBus + Local/RemoteSubscriber, adapt EventBus to it.** Make `EventBus.publish` delegate to `MessageBus.publish("events", ...)` with the store as the privileged durable subscriber. Keep the `Event`-shaped payload. `boxagent.log` unchanged. ~2 days. Medium risk (touches the /events + notifier + sync fan-out order — must verify id/seq enrichment ordering). Reference test: `test_event_syncer.py` must stay green.
- **Phase C — route chat through the same MessageBus** (`chat.<machine>.<bot>.<chat>` topics, ephemeral). WebChannel `_publish` becomes `bus.publish(topic, event)`; ChatBus subscribe becomes `bus.subscribe(topic, LocalSubscriber(queue))`. ~2 days. Medium risk (the demand-refcount / owner-pump lifecycle must be preserved — this is where 坑 #1 lives).
- **Phase D — unify the wire frame** to `{topic,payload,ts}` and collapse the two `handle_frame`s into one topic-routed dispatch. **Do this last, only once A–C are stable, and gate it behind a version check** so mixed-version clusters don't break. ~2 days + soak. Highest risk (wire contract).

Effort total: ~8–9 focused days, and critically, **each phase is revertable**. A big-bang is maybe ~6 days of coding but concentrates all risk into one unrevertable commit against the two subsystems where multi-node bugs are hardest to reproduce. Net: incremental is *cheaper* once you price in the debugging tail.

If the owner insists on big-bang, at minimum: land Phase A first (it's pure win regardless), and keep the old wire frames (Phase D) out of the big commit.

### 3b. Backpressure semantics differ and must be chosen per-topic

- Events currently **buffer unboundedly** in `EventSyncer._buffer` (slices MAX_BATCH per flush) — a slow/dead peer grows the buffer; acceptable because event volume is low.
- Chat **drops on QueueFull** (`_deliver`, `chat_sync.py:134`; WebChannel:69) — correct for stream_delta (a dropped delta is recoverable; the `stream_end` carries full text).

A unified RemoteSubscriber with one `asyncio.Queue(maxsize=…)` must let the **topic policy choose drop-vs-block-vs-unbounded**. Don't hard-code one. This is a real design decision, not a detail — get it wrong and either events silently drop or chat backpressures the whole bot.

### 3c. Sync vs async publish mismatch

`EventBus.publish` is **synchronous** (`log.info(...)` is called from sync business code all over — it must not require `await`). `ChatSyncer`/`WebChannel._publish` fan-out into async queues but the *entry* (`_publish`) is also sync (`put_nowait`). But cross-machine `_send_to` is `async`. The unified bus therefore needs a **sync `publish` that does sync local fan-out and hands remote delivery to a per-topic pump** — i.e. RemoteSubscriber.deliver must be callable from a sync context (`put_nowait`), with the actual `await send_frame` happening in the pump task. Keep `publish` sync; the async is confined to the pump. Getting this boundary wrong reintroduces 坑 #1.

### 3d. The wiring-chain fragility is a real existing bug risk, and the merge is the chance to kill it

`chat_sync_wiring` MUST install after `sync_wiring` because event wiring does *unchained assignment* of `on_unknown_frame`/`on_guest_attached` and chat wiring *chains* onto it (`chat_sync_wiring.py:20-22,37-42`; gateway enforces order at `gateway.py:288-296`). This is a landmine: any future third consumer, or a reordering, silently drops one syncer's frames. **A unified PeerTransport with a single `handle_frame` that dispatches by topic eliminates the chain entirely** — one `on_unknown_frame` owner, topic-routed. This is a concrete correctness win of the merge, worth calling out to justify the effort.

### 3e. `machine_id` naming already drifts

`EventBus` uses `machine_id`; `ChatSyncer` uses `local_machine`/`machine`; registry uses `machine_id`. Unify on `machine_id` in the merged code (CLAUDE.md: full words, and `mid` is explicitly the cited footgun). Minor, but the merge touches every one of these — fix it in passing, don't spread the drift.

---

## 4. Questions for architect and tester

**For the architect:**

1. Do you actually want event replication and chat replication to become *one* code path, or is "one bus API + shared transport, two replication coordinators" (my §2b) acceptable as "unified"? I claim there is no shared *replication algorithm* to merge — only shared *transport*. If you believe there is, show me the pseudo-code where broadcast+cursor+debounce and subscribe+refcount+relay share a branch.
2. Is the envelope truly `{topic, payload, ts}` with everything else opaque, or do you want `origin_machine`/`origin_seq` promoted to envelope fields? Promoting them re-couples the bus to event durability semantics (I argue against; §1f). Your call decides whether the bus can ever carry a third content type cleanly.
3. Big-bang vs incremental (§3a): given the wire-contract + mixed-version-cluster risk, are you willing to at least keep Phase D (frame unification) out of the initial cut? If it must be big-bang, how do we handle a cluster mid-upgrade?
4. Per-topic backpressure policy (§3b): who owns the choice of drop/block/unbounded — the bus config, or the coordinator registering the subscriber?

**For the tester:**

1. `test_event_syncer.py` and `test_chat_sync.py` assert on current frame types (`event_batch`, `chat_subscribe`) and method names. Under the merge these change. Do we (a) rewrite them against the new `{topic,payload}` frames, or (b) add a black-box *behavioral* layer (two fake nodes, assert "node B's store ends up with node A's event" / "only the subscribed node receives chat_event") that survives the refactor? I strongly prefer (b) as the safety net *before* touching code — can you write that node-pair harness first?
2. What's the multi-node ordering test? 坑 #1 is invisible to single-process tests. Can we assert per-topic order under a burst of stream_deltas across the pump (e.g. publish 100 deltas, assert the RemoteSubscriber queue delivers them in order, no create_task interleave)?
3. Resync/catch-up coverage: is there a test that a peer connecting *after* N events have been published receives all N (cursor path)? That's the event-specific behavior most likely to silently regress in a merge, and the one least covered by chat's subscribe-only model.
4. Backpressure: can we test drop-on-full for chat and no-drop for events distinctly, so a wrong global policy fails a test?
