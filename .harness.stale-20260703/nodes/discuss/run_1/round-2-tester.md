# Round 2 — TESTER diffs

Read both R1 peers. Below: agreements (one line), disagreements (detailed), new insights, revised position. Only the deltas.

---

## Agreements (one line each)

- **A1.** Architect's 2×2 (transport axis × content axis) — agreed, and it's exactly the axis my INV groups B/C already split along.
- **A2.** Engineer's "40 shared : 380 divergent, base transport + two policy coordinators, NOT one strategy class" — agreed; from a test angle two siblings sharing `PeerTransport` is *easier* to gate than one forked class (fewer branches to prove).
- **A3.** Store must stay a **privileged, synchronous, envelope-transforming first subscriber** (engineer §1d) — this is precisely my INV-A1 no-go; both of us independently landed on "publish returns → row already written."
- **A4.** Keep `publish` sync for local fan-out; async confined to the per-topic pump (both peers) — my INV-E1/E2/E3 assume exactly this.
- **A5.** Envelope = `{topic, payload, ts}`, `origin_seq`/`level`/`bot` stay in payload (engineer §1f) — agreed; it makes INV-A3's param table cleaner (topic → policy, not envelope field → policy).
- **A6.** Phase 0 = build the loopback harness + freeze characterization *before* any production code moves (architect Phase 0, engineer's "write the node-pair harness first") — full agreement, this is my P0.
- **A7.** Per-topic backpressure must be policy-chosen, never global (engineer §3b) — my INV-F1/F2 fail if it's global.
- **A8.** Wiring-chain fragility is a real latent bug and the merge's chance to kill it (both) — my INV-D2 is its behavioral guard.

---

## Disagreements / sharper positions

### D1. Envelope has NO `durable` field — but I still get my declarative policy (resolves my own Q1 to architect)

In R1 I "strongly preferred an explicit `durable` field." **I withdraw that** — engineer §1f is right that promoting durability into the envelope re-couples the bus to event semantics. But I don't lose what I wanted. The declarative knob moves from *envelope* to a **bus-level topic-policy registration table** (architect's `POLICY = {"events.": durable, "chat.": ephemeral}`, engineer's "per-topic registration flag"). INV-A3 tests *that table*, not an envelope field. So: envelope stays `{topic,payload,ts}`; the durable/ephemeral decision is a one-line registration the store-subscriber keys off of. This satisfies both "bus is content-agnostic" and my "new topic that forgets `ephemeral` must fail a test" — the param table asserts every registered topic's durability.

### D2. Big-bang vs incremental — I side with the ENGINEER, and it changes my gate table

Architect's Phase 3 is "hard-swap `log.bind` in one commit, byte-identical rows." Engineer argues incremental behind façades because the blast radius is the two hardest-to-repro multi-node subsystems + a wire contract. **From regression-safety this is not close: incremental wins.** A big-bang cutover invalidates `test_event_syncer.py` (12) + `test_chat_sync.py` (16) + both wiring suites *simultaneously* — you delete your regression net at the exact commit you most need it. My frozen invariant layer (below) is what makes incremental *safe*, but it can't rescue a big-bang that rewrites the wire frame and both policies at once. **Revised gate table (§P table) adopts engineer's A→D phase names, not architect's 0-5.**

### D3. Wire-frame change (engineer Phase D) must be its OWN gate with a mixed-version test

Neither R1 gate explicitly tests the mixed-version cluster. Engineer flagged the risk but didn't give the gate. **New gate:** before Phase D lands, a harness test where node A speaks new `{topic,payload}` frames and node B still speaks old `event_batch`/`chat_subscribe` — assert **no crash, graceful ignore** (old node drops unknown frame, doesn't die). This is the one gate that can't be a pure in-process invariant; it needs the harness to run two frame vocabularies on one link.

---

## New insights (not in R1)

### N1. The FROZEN INVARIANT MECHANISM — concrete, answering the "886 trap"

Two artifacts, both land in **Phase 0, before any refactor**:

**(a) `tests/unit/test_message_bus_invariants.py`** — the frozen behavioral layer. Every assertion reads ONLY: `node.store_rows(**filter)`, subscriber `queue` contents, or frames captured on a fake `RemoteLink`. **Zero** references to `EventSyncer` / `ChatSyncer` / `_subscribers` / `_pumps` / `_buffer`. Contents = my INV-A1..G5 (already enumerated in R1 §2.2), phrased through the harness. This file is **append-only** across the whole migration — a diff that *modifies an existing assertion's expected value* is a red flag (behavior regression), not a refactor. CI-enforceable: `git diff` on this file may only add functions.

**(b) `docs/bus-migration-map.md`** — the mapping table that defeats the delete-and-add trap. One row per existing bus test:

| old test | asserts (behavior) | covered by invariant | delete-old-when |
|---|---|---|---|
| `test_event_syncer::test_local_publish_propagates_to_peer` | A publishes → B.store has it | INV-B1 | Phase C green |
| `test_chat_sync::test_owner_forwards_local_publish_to_subscribed_peer` | subscriber gets its chat only | INV-C1 | Phase C green |
| `test_chat_sync::test_aclose_cancels_pumps` (peeks `_pumps`) | after close, no delivery | INV-C7 (rewritten black-box) | Phase C green |
| ... (all 126) | ... | INV-x | ... |

**Rule enforced at review:** an old test file may be deleted ONLY when every one of its rows points at a *green* invariant. No row → the behavior is silently dropped → block the PR. This is the concrete answer to "delete a coupled test + add a new one hides a regression": you can't delete until the map proves the behavior migrated, and the frozen file's expected-values can't shift.

### N2. The harness must land as `tests/unit/_bus_harness.py` reusing BOTH R1 patterns literally

I verified the two patterns exist and are compatible:
- `test_event_syncer.py::_wire_pair(syncer_a, syncer_b)` — cross-wires two syncers so `a.attach_peer(key, a_to_b)` routes into `b.handle_frame(key_a, frame)`. This IS the in-memory link.
- `test_chat_sync.py::_make(local, route)` — recording fake peers (`sent[peer]` frame list), and `route=lambda target: "host"|target|None` models 2-hop topology.

The harness generalizes both: a `TwoNodeCluster` / `ThreeNodeCluster` whose `link()` installs a bidirectional `_wire_pair`-style shuttle between two *real* nodes (real store + real bus + real replicator), and whose `route` closure picks host-relay vs direct exactly as `_make` does. `settle()` polls observable conditions (buffer empty + no pending flush task + target queue reached expected count) — **never bare `sleep(0.05)`** (R1 §3.3). The harness self-tests first (`test_bus_harness.py`) or every dependent invariant is falsely green.

### N3. Answering the injected reordering question (architect Q4 / engineer Q2 to me): YES, and it's mandatory

The footgun test (INV-E1: 100 events in order → B.store in order) is **itself suspect of being falsely green** — single-thread asyncio may happen to preserve order even with a buggy per-event `create_task`. So the harness ships a **`reorder_tasks` injection hook**: a test-loop policy wrapper that, when armed, randomly permutes the completion order of pending tasks scheduled during a publish burst. Proof obligation, done in Phase 0: run INV-E1 against a *deliberately broken* stub replicator that does `create_task` per event, **with `reorder_tasks` armed, and confirm it goes RED**. Only then is INV-E1 trustworthy against the real implementation. A footgun guard you never saw fail is not a guard. (This also answers engineer Q7: point me at the single per-topic pump task and I'll arm reorder around it.)

### N4. `CountingEventStore` spy is the load-bearing half of the negative test (my R1 risk 1, now concrete)

INV-A2 (stream_delta → 0 SQLite rows) cannot rest on `store.query()==[]` alone — a leak into an unqueried category/table is falsely green. **Mechanism:** `CountingEventStore(EventStore)` subclass increments a counter in `insert_local`/`insert_remote`. INV-A2 asserts the counter's **delta == 0** across 200 chat publishes, AND a before/after full-snapshot equality. This is only sound if the store has exactly those two write points (engineer Q5 — please confirm). If a third write path appears, the spy under-counts and the negative test is compromised; that confirmation is a Phase-C precondition.

---

## Revised position: per-phase gate PRINCIPLE + table

**Principle (unchanged core, sharpened):** every phase's gate = **(the full frozen invariant set green) + (that phase's specific high-risk invariant called out as the named no-go) + (`pytest -x -q` passed ≥ 886)**. The frozen set never shrinks; each phase merely *adds* the invariant that its new code path first makes reachable. A phase is done only when its named no-go is green AND the map's deletable-rows are all green.

Adopting the engineer's A–D phasing (D2 above):

| phase | named no-go for this phase |
|---|---|
| **P0** harness + freeze | harness self-test green; ALL invariants green **against old code** (proves they describe real behavior, not fiction). Reorder-hook proven RED against a broken per-event-`create_task` stub (N3). No product code touched. |
| **A** extract `PeerTransport` | INV-B*/C* unchanged-green (behavior identical); 126 old tests still green (pure delegation). |
| **B** `EventBus`→`MessageBus`, store as privileged subscriber | **INV-A1** (row written before publish returns) + INV-E1/E3 (order) + INV-G1/G2/G3/G4/G5. No-go: A1 — if store went async, A1 reds. |
| **C** chat onto same bus, ephemeral topics | **INV-A2 + INV-D3** (chat never hits SQLite, spy delta 0) + INV-C* + INV-E2 + INV-F1. No-go: A2. |
| **D** unify wire frame `{topic,payload,ts}` | **INV-D1** (reconnect: event cursor-resync AND chat re-subscribe in ONE test) + INV-D2 (frames don't swallow each other) + **new mixed-version test** (D3). No-go: D1 + mixed-version. |

**The one cross-cutting no-go (INV-D1)** deserves emphasis because it's the invariant *no existing test covers* and the merge is most likely to half-break: one harness test, ThreeNodeCluster, drop link → A emits 2 events + 3 chat deltas → relink → settle → assert **(a)** B.store backfilled the 2 events via cursor (backlog recovered) **and (b)** B's chat queue resumes receiving *new* deltas (subscription re-sent); the 3 dropped deltas are allowed lost (chat is live, no backlog). Event-resync and chat-re-subscribe verified in the *same* reconnect, so the merge cannot silently drop one.
