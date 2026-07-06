# Round 2 — Architect diffs

Only what changed. Where engineer/tester convinced me, I concede in one line. Where I hold, I answer their direct question with code.

---

## Agreements (one line each)

- **Envelope = `{topic, payload, ts}`, nothing else promoted.** I concede: `origin_seq`/`origin_machine`/`level`/`bot` go in `payload`, NOT envelope fields (engineer §1f). Reason below in D3.
- **Store stays a *privileged synchronous* subscriber (enrich-then-fanout), NOT an unordered peer.** I concede fully (engineer §1d, tester Q2). See D3.
- **PeerTransport by composition** (shared `_peers`/`_send_to`/`attach`/`detach`/`handle_frame` ~40 lines) is the correct and only large dedup — engineer §1b nailed the measurement.
- **The wiring-chain fragility is the real justification** — one topic-routed `handle_frame`, no chaining, no ordering constraint (engineer §3d, my R1 §1.5 agreed already).
- **Backpressure is per-subscriber, never shared queue; durable subscriber never drops, ephemeral may drop** (engineer §3b, tester INV-F1). Owner: the *coordinator registering the subscriber* picks the policy, not the bus core.
- **Unify naming on `machine_id`** in merged code (engineer §3e).
- **Tester's characterization-first + `CountingEventStore` spy + `settle()`-on-observable-condition** harness is the gate. Adopt as-written; Phase 0.
- **WebChannel + EventStreamSubscriber zero-coverage is a precondition debt** — characterize before touching (tester §3.5).

---

## Disagreement 1 — big-bang vs phased. I take a position.

**"Full-unification GOAL, executed as N revertable phases each green" IS a faithful satisfaction of big-bang, not a dodge — with one hard condition.**

The owner's "big-bang" means *reach the full end-state, don't leave a half-merged hybrid that ossifies* (the failure mode BA已经有过: two syncers copy-pasting each other's skeleton for months). It does NOT mean "land it in one unrevertable commit." Those are orthogonal: **end-state completeness** vs **commit granularity**. Conflating them is the dodge risk in the other direction — someone ships Phase A (PeerTransport) and declares victory, leaving events/chat still divergent. That would be the real betrayal of "big-bang."

So my position: **commit to the full end-state as a contract (all 4 phases land, in one PR series, no indefinite pause at Phase A), execute as revertable phases.** The phasing is a *safety envelope inside a big-bang mandate*, not an alternative to it.

The one hard condition where I overrule the engineer's caution: **the wire-frame unification (engineer's Phase D) must actually land, not be deferred forever behind "mixed-version cluster risk."** Engineer §3a is right that a mixed-version cluster breaks on a frame change with no rollback — but BA is a **single-owner personal network**, not a fleet with staggered rollout. `git pull && restart` across 3-4 personal boxes is the deploy model. The mixed-version window is minutes, self-inflicted, and the owner controls both ends. I accept keeping Phase D last and gated, but I reject treating it as optional — deferring it permanently leaves TWO frame vocabularies, which is exactly the copy-paste we're deleting. **Concession to engineer:** land old frames through Phase C, flip to `{topic,payload}` in Phase D behind a one-line protocol-version byte, and the owner restarts all nodes together. No soak needed at personal scale.

Net: phased execution, YES; treating any phase (esp. D) as a place to stop, NO.

---

## Disagreement 2 — class structure. I CONCEDE, with the branch I owe you.

Engineer §2.b asked me to "show the pseudo-code where broadcast+cursor+debounce and subscribe+refcount+relay share a branch — otherwise concede." I tried to write it. Here is the honest result:

```
# The ONLY place they'd share a branch — on_local_publish for a topic:
def on_local(topic, envelope):
    for peer in peers_for(topic):          # ← events: ALL peers; chat: routed-to-one
        buffer_or_send(peer, envelope)       # ← events: buffer+200ms debounce+batch;
                                             #   chat: put_nowait to per-peer pump, immediate
# and on_remote_frame:
def on_remote(peer, frame):
    persist_or_deliver(frame)                # ← events: insert_remote+dedup+gossip-onward;
                                             #   chat: fan-out to local queues + relay-onward
```

Every single line's body is a policy fork with **no shared statements** — `peers_for` differs (broadcast set vs `route()` one), `buffer_or_send` differs (debounce-batch vs immediate-pump), `persist_or_deliver` differs (store+gossip vs queue+relay). A "strategy class" here is two method bodies with a shared *signature* and zero shared *code* — that's two classes with a trenchcoat, exactly as engineer said. The shared 40 lines are transport (`_peers`, `_send_to`, `handle_frame` dispatch), which composition already extracts.

**I concede: sibling `EventReplicator` + `ChatReplicator` over a shared `PeerTransport` (composition), NOT one strategy class, NOT base+subclass.** My R1 "one unified core" was wrong at the *replication* layer. It was right at the *bus API + transport + local-link* layer, which is where the actual unification lives (`MessageBus` + `Local/RemoteSubscriber` + `PeerTransport`). We agree there. The four→one story is: **one bus API, one transport, one local-link abstraction, two policy coordinators.** That is still a real unification (deletes the copy-pasted skeleton and the dual wiring), just not a single replication code path — because there is no single replication algorithm to have.

---

## Disagreement 3 — envelope durability field. Tester + engineer win; I refine, don't overload.

I proposed `origin_seq==0 ⇒ ephemeral, >0 ⇒ durable`. **Concede: that overloads a payload field to carry a routing policy — a footgun.** Tester Q1 wants explicit; engineer §1f wants it off the envelope entirely. Both are right, and they're compatible:

- **Durability is NOT an envelope field and NOT inferred from `origin_seq`.** It's a **per-topic registration flag on the bus** (engineer §2c: `subscribe(topic, sub, durable=...)` / a topic-policy table). `events.*` registered durable; `chat.*` registered ephemeral. This is a *wiring fact*, statically enumerable — which is exactly what tester's INV-A3 parametrized table needs to catch "new topic forgot to mark ephemeral."
- **`origin_seq` lives in `payload`, minted by the store-subscriber, never read by the bus core.** So it can't be overloaded as a policy signal — the bus never looks at it.
- **The store stays privileged-synchronous.** Answering engineer §1d / tester Q2 directly: the `events` topic has the `DurableStoreSubscriber` registered **first**; `publish` on a durable topic does enrich-then-fanout (store mints `id`+`origin_seq`, the enriched envelope is what the remaining N-1 subscribers see). Making it an unordered peer breaks `/events` `before_id` pagination (needs `id` at fanout time) and cross-machine dedup (needs `origin_seq`) — I accept both as load-bearing. `publish` stays **synchronous** for local fanout; async is confined to the RemoteSubscriber pump.

The policy table is thus 2 knobs, matching my R1 falsifiability test: if it needed a 3rd dimension the abstraction would be wrong. It doesn't — `durable` (store subscribes or not) + `replication` (which coordinator owns the topic). Still holds.

---

## New insight (didn't surface in R1)

**The `durable` flag and the "which coordinator" choice are the SAME registration, so fold them.** Instead of two parallel lookups (`POLICY[prefix].durable` and `POLICY[prefix].replication`), a topic is registered once with its subscriber set: `events.*` → `[StoreSubscriber(durable, first), EventReplicator, Notifier, SSE]`; `chat.*` → `[ChatReplicator, local-queues]`. "durable" is just "is StoreSubscriber in this topic's list." No separate policy table to drift from the wiring — the subscriber list **is** the policy. This directly kills tester's INV-A2 worry (chat can't leak to SQLite because StoreSubscriber is not in `chat.*`'s list — a wiring fact, not a runtime check) and answers tester Q1: explicit, and enforced by construction rather than a `bool`.

---

## Revised position (the through-line)

Unify the **API + transport + local-link** (real, deletes copy-paste + dual wiring). Keep **two replication coordinators** (no shared algorithm exists). Envelope is `{topic, payload, ts}`; durability is a per-topic subscriber-list fact, not an envelope field; store is privileged-synchronous. Execute as **N revertable phases toward the full end-state — all phases land, Phase D included; phasing is the safety envelope, not an exit ramp.**
