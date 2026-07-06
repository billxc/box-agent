# Round 1 — Architect analysis: unifying the four buses into one MessageBus

## 1. Key observations

### 1.1 The four things are NOT four; they are 2×2 (transport axis × content axis)

Read the actual code and the shape collapses:

| | LOCAL delivery | REMOTE delivery (cluster WS) |
|---|---|---|
| **events** | `EventBus` fan-out to callbacks + `EventStreamSubscriber` queue | `EventSyncer` — full replication, debounced batch, cursor resync |
| **chat** | `WebChannel._publish` per-`chat_id` queue fan-out | `ChatSyncer` — subscription, refcount, 2-hop host relay |

The "four implementations" are one 2×2 grid. The horizontal axis (local vs remote) is the *link*; the vertical axis (events vs chat) is *policy* (durable-broadcast vs ephemeral-subscribe). This is exactly the target model, and the current code already half-admits it: `decisions.md` line 25 says outright **"ChatSyncer 抄了已在生产验证的 EventSyncer 骨架"** — same `attach_peer`/`detach_peer`/`handle_frame`/`_peers: dict[str, SendFrame]`/`_send_to` skeleton, differing only in the frame vocabulary and whether delivery is broadcast-to-all-peers vs routed-to-one.

So the unification is not inventing a new abstraction — it is **extracting the skeleton both syncers already copy from each other** and letting the two policy differences live as subscriber/topic config. That is the single most important fact for scoping: the risk is low *because the merge target already exists twice in production*.

### 1.2 The remote "link" is genuinely uniform already

Both `EventSyncer` and `ChatSyncer` hold `self._peers: dict[peer_key, SendFrame]` where `SendFrame = Callable[[dict], Awaitable[None]]`. Both are wired identically in gateway: host attaches one peer per guest (`peer_key == machine_id`), guest attaches one peer (`peer_key == "host"`). Both hook `on_guest_attached`/`on_guest_detached`/`on_unknown_frame`. The `send_frame` closure is literally the same three lines in `events/sync_wiring.py` and `cluster/chat_sync_wiring.py`. **There is already exactly one remote-link abstraction, copy-pasted.** A unified bus has one `RemoteLink` and both wirings install it once.

### 1.3 The local "link" is two different queue idioms doing the same job

- `WebChannel`: `_subscribers: dict[chat_id, list[Queue]]`, `subscribe(chat_id) -> Queue`, `_publish(chat_id, dict)`.
- `EventBus`: `_subscribers: list[Callback]`, callback gets `Event`; `EventStreamSubscriber` wraps a callback around a `Queue` with `_matches()` filtering at enqueue.

Both are "topic → set of in-process queues, put_nowait, drop on full." The event side has an extra callback indirection (because `EventStore` write and `TelegramNotifier` are also callback subscribers, not queue subscribers). This matters: **the events local-bus has non-queue subscribers (store-writer, notifier) that must run synchronously inside `publish()`**, whereas chat local-bus only has queue subscribers. The unified bus must support *both* a callback subscriber and a queue subscriber — which is fine, a queue subscriber is just a callback that does `queue.put_nowait`.

### 1.4 The two hard content-differences are real and must survive as policy

1. **Durability / message shape.** Events are `Event` (frozen dataclass, `origin_machine`+`origin_seq` natural key, persisted, resync-able). Chat events are anonymous `dict`s (`{"type": "stream_delta", ...}`), never persisted, no seq, no identity. **stream_delta is hundreds/sec and must never touch SQLite** (constraint 1). The store is a *subscriber to one topic*, not a bus feature (constraint 2).
2. **Replication topology.** Events = full replication (every node subscribes to every node's events; `EventSyncer._flush` broadcasts to *all* peers; `_handle_batch` gossips onward). Chat = demand-driven subscription (only the viewing node subscribes; `ChatSyncer._refresh_source` refcounts, `route()` picks one peer). These are two different *subscription patterns over the same bus*, not two buses.

The envelope must therefore carry enough to express both without the bus core understanding either.

### 1.5 Dependency directions today (the actual chains)

```
gateway.py
  ├─ imports events.bus.EventBus, events.storage.EventStore, events.sync.EventSyncer,
  │         events.retention, events.telegram_notifier          (events/ is leaf-ish)
  ├─ imports cluster.chat_sync.ChatSyncer, cluster.chat_bus.ChatBus
  │         cluster.chat_sync_wiring, events.sync_wiring
  └─ wires: log.bind(EventBus);  EventSyncer.subscribe→EventBus;
            ChatBus.channel_for = AgentManager.web_channels.get

events/ depends on: log (Category), nothing in cluster/
cluster/chat_sync.py depends on: nothing (pure; route injected)
cluster/chat_bus.py depends on: chat_sync + duck-typed WebChannel (no transport import)
cluster/registry.py depends on: log
transports/web/server.py depends on: chat_bus (duck), event_bus (reaches into ._store!)
```

Two smells:
- `events/sync_wiring.py` and `cluster/chat_sync_wiring.py` **both** monkey-patch the same three registry callbacks, and chat *must* install after events and chain through the captured old value (fragile ordering documented in the wiring docstring + gateway comment). **This is the single ugliest coupling and the strongest argument for unification** — one bus means one wiring, no chaining, no ordering constraint.
- `web/server.py` reaches into `self.event_bus._store.query(...)` and even `._store._conn.execute(...)` directly (lines 991, 1005, 1017, 1047). The store is already a de-facto separate concern; the "store is a subscriber" refactor is aligned with how the read path *already* bypasses the bus.

### 1.6 What "location transparent" already bought chat, events lacks

`ChatSyncer` keys everything on `(owner_machine, bot, chat_id)` and treats "local" as `machine == self` — one table, `_deliver` shared between owner-publish and relay. `EventSyncer` has no such unification: local delivery is `bus.subscribe` callbacks, remote is a separate `_peers` broadcast. **Chat is the more evolved design.** The unified bus should adopt chat's "local is a topic-key special case" model, not events' "two separate mechanisms" model.

---

## 2. Proposed approach + rationale

### 2.1 The envelope

```python
@dataclass(frozen=True)
class Message:
    topic: str                 # routing key, dotted hierarchy
    payload: dict              # opaque to the bus
    origin_machine: str        # who first published
    origin_seq: int = 0        # 0 = ephemeral (no durable identity); >0 = durable
```

Rationale for each field:
- `topic` — the only thing the bus routes on. Replaces both `(level, category)`→callback matching and `(bot, chat_id)`→queue keying. Dotted hierarchy so prefix-subscribe works (events page already does category-prefix matching; `EventStreamSubscriber._matches` becomes a prefix subscription).
- `payload: dict` — content-agnostic. `Event` becomes `payload` fields on the `events.*` topic; chat dicts are `payload` on `chat.*` topics. **The bus never constructs `Event`.** `Event` becomes a store-subscriber-private view over `payload`.
- `origin_machine` + `origin_seq` — the *only* concession to cross-machine identity, and it's the minimum: `origin_seq == 0` means "ephemeral, no dedup, no resync" (chat); `origin_seq > 0` means "durable, dedup on `(origin_machine, origin_seq)`" (events). **The seq is assigned by the store-subscriber, not the bus** (today `EventStore.insert_local` computes `MAX(origin_seq)+1`). The bus core treats seq as an opaque tag it copies into remote frames.

Topic scheme:
```
events.<category>                         e.g. events.scheduler.run, events.cluster.host.rpc_fail
chat.<owner_machine>.<bot>.<chat_id>      e.g. chat.win-mini.assistant.web-42
```
`bot`/`level`/`origin_machine` that today are `Event` columns stay queryable because the **store-subscriber** unpacks `payload` into SQLite columns — the topic only needs to carry what *routing/subscription* needs. Level is a payload field, not topic, because subscribers filter level *after* topic match (matches current `EventStreamSubscriber.levels`).

### 2.2 The bus-core contract (minimal)

```python
Subscriber = Callable[[Message], None]        # sync; queue-subscribers wrap put_nowait

class MessageBus:
    def publish(self, message: Message) -> None: ...
    def subscribe(self, topic_pattern: str, subscriber: Subscriber) -> Subscription: ...
    # topic_pattern: exact "chat.m.b.c" OR prefix "events." OR "events.scheduler."

    def attach_link(self, peer_key: str, link: RemoteLink) -> None: ...
    def detach_link(self, peer_key: str) -> None: ...
    async def handle_remote_frame(self, peer_key: str, frame: dict) -> bool: ...
```

- `publish` fans out **synchronously** to all matching local subscribers (preserves the events invariant that store-write + notifier run inside publish; preserves chat ordering — no `create_task` per event, the坑 #1 in CLAUDE.md). Remote forwarding is a *subscriber* (see 2.3), so publish stays sync and the async send happens in that subscriber.
- `subscribe` returns a `Subscription` (has `.close()`), replacing the ad-hoc `unsubscribe(callback)` / `unsubscribe(chat_id, queue)`.
- `RemoteLink` is the one uniform remote abstraction: `Protocol` with `async def send(self, frame: dict) -> None`. The wiring closures in both `*_wiring.py` collapse into constructing one `RemoteLink` from a WS. **One wiring module, installed once, no chaining, ordering constraint gone.**

### 2.3 Where durability / broadcast / resync live: as SUBSCRIBERS and per-topic POLICY

The bus core does NOT know event vs chat. Two policy objects subscribe:

**(A) `StoreSubscriber`** — subscribes to `events.` prefix:
- on local publish: assigns `origin_seq`, writes SQLite (owns `insert_local`), keeps the `Event` view for the read path (`/api/events` query stays `store.query`, unchanged).
- on remote `Message` arriving durable: `insert_remote` (INSERT OR IGNORE dedup) — **all resync/cursor/dedup logic stays here, in the store, exactly as today** (constraint 2 honored by construction). Chat never routes here because it subscribes only `events.`.

**(B) `ReplicationSubscriber`** (the merged syncer) — owns the `_peers`/`RemoteLink` map and the two subscription *patterns*:
- **broadcast pattern** for `events.` : subscribe to `events.` prefix, debounce-batch, send to *all* peers, gossip onward, resync on attach via store cursors. This is `EventSyncer` verbatim.
- **demand pattern** for `chat.` : refcount `(topic)` subscriptions, `route()` to one peer, 2-hop relay. This is `ChatSyncer` verbatim.

Per-topic policy knob (constraint 1 — stream_delta must never hit SQLite):
```python
POLICY = {
  "events.": TopicPolicy(durable=True,  replication="broadcast"),
  "chat.":   TopicPolicy(durable=False, replication="demand"),
}
```
`durable=False` → `StoreSubscriber` doesn't subscribe that prefix → **stream_delta physically cannot reach SQLite.** This is not a runtime check; it's a wiring fact.

### 2.4 Module boundaries + dependency direction — the key decision

**Introduce a neutral `bus/` package that neither `events/` nor `cluster/` owns.**

```
bus/                         NEW, neutral core — depends on NOTHING in the project
  message.py                 Message dataclass, TopicPolicy
  core.py                    MessageBus (local fan-out + link registry)
  link.py                    RemoteLink Protocol

events/                      becomes "the durable-broadcast policy over the bus"
  store.py                   EventStore (unchanged internals)
  store_subscriber.py        StoreSubscriber (was EventBus.publish store-write + insert_remote)
  models.py                  Event (now a payload-view, read path only)
  retention.py, telegram_notifier.py, web_stream.py   (subscribers, ~unchanged)

cluster/
  registry.py, guest_client.py   grow one method: expose a RemoteLink per peer
  replication.py             merged EventSyncer+ChatSyncer skeleton (the shared part)
  bus_wiring.py              ONE wiring: attach RemoteLink on attach/detach, route frames

log/                         facade unchanged — log.bind() now binds a thin adapter
                             that turns publish(level,category,message,**meta) into
                             bus.publish(Message(topic=f"events.{category}", payload=...))
```

Dependency direction: **`bus/` is a leaf. `events/` depends on `bus/`. `cluster/replication.py` depends on `bus/`.** Neither `events/` nor `cluster/` depends on the other. Today `cluster/chat_bus.py` duck-types WebChannel to avoid importing transport — same discipline: the bus core imports nothing, everyone imports the bus.

**Why neutral core, not "put it in events/":** today the ugliest coupling is the two wiring modules fighting over registry callbacks. If the bus lived in `events/`, `cluster/` would depend on `events/` for the link abstraction, and chat (a cluster concern) would import events (a logging concern) — semantically backwards. A neutral `bus/` makes the dependency a clean fan-in.

### 2.5 The `boxagent.log` facade is untouched (constraint 3)

`log.bind(sink)` where `sink.publish(level, category, message, **meta)` — signature preserved. The bound sink is now a 10-line `LogToBusAdapter` that builds `Message(topic="events."+category, payload={"level":level,"message":message,"bot":meta.pop("bot",None),"meta":meta})` and calls `bus.publish`. Business code sees zero change. `/events` page, notifier, sync, retention all keep working because they become bus subscribers reading the same payloads.

### 2.6 Is this genuinely simpler, or a bad abstraction forced over two delivery models?

**Genuinely simpler — and here's the falsifiable test.** The two delivery models (durable-broadcast vs ephemeral-demand) do NOT collapse into one; I am **not** unifying them. I unify the *link* (already copy-pasted identical) and the *local fan-out* (two queue idioms), and I keep the two delivery models as two `ReplicationSubscriber` patterns + a per-topic policy. The bad-abstraction failure mode would be forcing chat through the durable path or events through demand-subscribe; the policy table prevents exactly that at wiring time.

Net line delta estimate: `bus/core.py`+`message.py`+`link.py` ≈ 150 lines NEW; deletes `EventBus` (63), collapses two `*_wiring.py` (~130) into one (~60), merges two syncers' skeletons (~100 lines of duplication gone). **Roughly net-neutral LOC but one fewer copy of the skeleton and one wiring path instead of a chained pair.** The win is not fewer lines; it's *one ordering-free wiring* and *content-agnostic core*, which is what survives the next 3 refactors (add a 3rd content type — presence? typing-across-machines? — as a topic+policy, zero core change).

If it were NOT simpler, the tell would be: the policy table needs a third dimension, or `Message` needs content-type-specific fields. It doesn't — `durable` + `replication` covers both, and that's the evidence the abstraction fits.

---

## 3. Risks & concerns

**R1 — Ordering & sync-publish invariant (highest).** Today `EventBus.publish` runs subscribers *synchronously* (store write, then notifier, then queues) and chat's owner-pump forwards *sequentially* (`await on_local_publish`, no `create_task` — CLAUDE.md 坑 #1). The unified `publish` must stay sync for local fan-out, and the remote-forward subscriber must not `create_task`-per-message or it reintroduces the Codex-style reordering bug. Mitigation: `ReplicationSubscriber` buffers + single debounced flush task (events already does this; chat pump is inherently sequential). But merging them risks someone "simplifying" to per-message tasks. Must be a black-box test asserting delta order.

**R2 — Backpressure semantics diverge and must NOT be homogenized.** Events drop on `QueueFull` with a warning (maxsize 1000); chat drops on `QueueFull` (maxsize 1024). Same policy today, fine. But stream_delta at hundreds/sec into a slow browser SSE will hit the cap; the drop must stay *per-subscriber* (slow client drops its own traffic — `web_stream.py` comment says exactly this). If unification accidentally shares one queue across topics, one slow chat client stalls events. Mitigation: subscription = its own queue, never shared. Verify with a full-queue test.

**R3 — Migration touching the live event pipeline.** `log.bind()` happens at gateway line 161, before almost everything. Any phase that swaps the sink risks a window where `log.info` no-ops or double-writes. Mitigation: the adapter is introduced *last*, behind the already-passing store/subscriber tests; `log.bind` swaps from `EventBus` to `LogToBusAdapter` in a single commit where both produce byte-identical SQLite rows (assert via `store.query`).

**R4 — `origin_seq` ownership move.** Today `EventStore.insert_local` computes the seq. If the bus assigns it, cross-machine dedup breaks. Decision in this design: **seq stays in the store**, bus copies it opaquely. Risk is a reviewer "tidying" seq into the envelope constructor. Must be documented as a load-bearing invariant.

**R5 — `route()` and topology timing.** `ChatSyncer`'s `route(machine)` reads `topology.guest_registry.sessions` live; on host-failover the peer set churns. The merged subscriber inherits `resubscribe` on reconnect (chat has it; events does resync-on-attach). Merging must preserve *both* reconnect behaviors (events: cursor resync; chat: re-send subscribes) — they're different and both needed.

**R6 — Test baseline (918 passed per decisions.md, ≥886 floor).** ~50 tests directly name `EventBus`/`ChatSyncer`/`EventSyncer`. Black-box ones (assert on store rows / queue contents) survive a rename; white-box ones (`test_event_bus.py` asserting `bus._subscribers`) break. Those must be *rewritten to the new boundary, not deleted* (tests only go up). Net test count should rise (bus core gets its own tests).

---

## 4. Questions for the engineer and tester

**For the engineer:**
1. **Prefix vs exact topic matching in the core** — do we want a real trie/prefix index, or is O(subscribers) linear scan with `startswith` fine at this scale (single user, tens of subscribers)? I lean linear (matches current `_matches`), but you own the fan-out hot path — stream_delta is the only high-frequency publisher; does linear scan on `chat.*` subscribers cost anything real?
2. **Do we keep `EventBus` as a deprecated shim during migration, or hard-swap?** I proposed hard-swap in one commit with byte-identical row assertions. Is there a phasing where the store-subscriber and bus-core land *green* behind the old `EventBus` (which internally delegates) before `log.bind` flips — so no phase has a red event pipeline?
3. **`RemoteLink` — do you want it to own the `send_json` + closed-check + try/except** (dedup'd from the 4 current copies in the two wiring modules), or stay a thin `send(frame)` and keep error handling in the subscriber? I lean fat link (kills the copy-paste).
4. Can the two syncers' skeletons *actually* share one class with a `pattern` strategy, or is the broadcast-vs-demand branching so pervasive that two subclasses over one shared base is cleaner? I sketched one class; you'll feel the branch density first.

**For the tester:**
1. What's the **black-box boundary** for the bus core? I propose: assert on (a) `store.query()` rows, (b) subscriber queue contents, (c) frames captured on a fake `RemoteLink`. Never peek `bus._subscribers`. Does that give you enough to prove broadcast-vs-demand replication without white-box?
2. The **stream_delta-never-hits-SQLite** constraint — how do we test a *negative*? I suggest: publish 500 `chat.*` messages, assert `store.query(category_prefix="chat")` returns 0 AND assert the store-subscriber was never called for `chat.` topics. Sufficient? Or do you want a policy-table unit test proving `durable=False` prevents subscription?
3. Is there an existing **end-to-end cross-machine test harness** (two in-process buses linked by a fake WS pair) I should assume, or does that need building? The migration's safety depends on a 2-node loopback test proving both event-resync and chat-demand over one link. If it doesn't exist, that's phase 0.
4. For the **reconnect behaviors** (R5): can you assert both — after link re-attach, events replay via cursor AND chat re-sends subscribes — on the same fake-WS harness, so the merge can't silently drop one?

---

## Appendix: phased migration (each phase independently testable + reversible, event pipeline never red)

**Phase 0 — 2-node loopback test harness (no production code).** Build a test-only fake `RemoteLink` pair that shuttles frames between two in-process instances. Write characterization tests against *current* `EventSyncer` and `ChatSyncer` through it (resync, demand, refcount, reconnect). These tests must pass before and after every later phase — they are the regression net. Reversible: pure test addition.

**Phase 1 — extract `bus/message.py` + `bus/core.py` + `bus/link.py`, unused.** Land the neutral core with its own unit tests. Nothing wired to it. Reversible: delete the package. Event pipeline untouched.

**Phase 2 — `StoreSubscriber` behind existing `EventBus`.** Refactor `EventBus.publish` to delegate store-write to a `StoreSubscriber` object, still called synchronously from `EventBus`. Assert byte-identical `store.query` rows (characterization). No external behavior change. Reversible: inline it back.

**Phase 3 — `EventBus` becomes a thin adapter over `MessageBus`.** `log.bind` still binds `EventBus`, but internally `EventBus.publish` now builds a `Message` and calls `bus.publish`; store-subscriber + web_stream + notifier re-subscribe via the bus. `/api/events` read path unchanged (still `store.query`). Assert: same rows, same SSE payloads, same notifier calls (MockChannel-style). This is the load-bearing phase — gated by phase-2 characterization tests staying green. Reversible: revert the one adapter file.

**Phase 4 — merge the two syncers into `cluster/replication.py` + one `bus_wiring.py`.** Chat and events both ride `ReplicationSubscriber` with policy. Delete `events/sync_wiring.py` + `cluster/chat_sync_wiring.py` chaining; install one link per peer. Gated by phase-0 loopback tests proving broadcast + demand + both reconnect behaviors. Reversible: the two old wiring modules stay in git; revert is one commit.

**Phase 5 — drop the `EventBus` shim; `log.bind(LogToBusAdapter)`.** Remove the compatibility layer. `ChatBus`/`WebChannel` local path either stays (duck-typed) or folds into the bus as `chat.` local subscriptions — decide based on whether the owner-pump indirection still earns its keep. Rewrite white-box tests (`test_event_bus.py`) to the new boundary. Net test count up.

Each phase: `uv run pytest -x -q` green, baseline never drops. Phases 2–3 keep the event pipeline behaviorally identical (characterization rows). Phase 4 is the only cross-machine-behavior change and is fully covered by phase-0's loopback harness before it lands.
