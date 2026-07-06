# Round 2 — Engineer diffs

Diffs only. Read R1 for the full argument.

## Agreements (one line each)

- Architect's 2×2 framing (transport axis × content axis) is the right decomposition — adopt it as the shared vocabulary. ✓
- The link is already copy-pasted identical (`_send_to` byte-for-byte, `_peers` same shape); extracting it is pure win. ✓
- Store-write must stay synchronous, enrich-then-fanout, store-subscriber runs *first*; async-izing it breaks `/events` pagination + cross-machine dedup + 坑 #1. ✓ (architect R4, tester Q2 — we three agree.)
- `durable` must be a per-topic **declared** flag, not inferred from topic name (tester INV-A3 / arch-Q1). Explicit. ✓
- Wiring-chain fragility (event-then-chat ordering) is a real latent bug; one topic-routed `handle_frame` kills it. ✓
- Phase 0 = build the 2-node loopback harness + freeze characterization invariants **before** touching code (tester §2.1, architect Phase 0). ✓ This is non-negotiable and comes first.
- Backpressure stays per-subscriber, own queue, drop-on-full; never one shared queue (architect R2, tester INV-F1). ✓

## Divergence 1 — big-bang vs phased: **RECONCILED, I withdraw the objection**

I retract "big-bang is the wrong call." The owner's intent — full unification, reach the end-state, no half-measure — is **fully honored by "full unification executed as N revertable phases, each green."** That is not a half-measure; it is the same destination with a rollback seam at each step. The architect and I actually converged on nearly the same phase list; the only real disagreement was framing. Big-bang-as-one-commit and phased-to-the-same-end-state produce identical final code. Phased just means the diff lands in reviewable, revertable chunks with the test baseline green between each. So: **do the whole thing, in order, each phase green.**

Nothing should be left out. Every part gets done. The one thing I'd *sequence carefully*, not skip, is the wire-frame unification (see order below).

### Concrete ordered phase list (risk-minimizing order)

The two load-bearing questions the architect asked me to answer directly:

**Q: wire frames first or LAST?** → **LAST.** The WS frame `{topic,payload,ts}` is a cross-machine contract; changing it is the only step where a mixed-version cluster (one node upgraded, one not) breaks with no rollback. Everything internal (bus core, store-subscriber, local fan-out) can land while the *old* `event_batch`/`chat_subscribe` frames stay on the wire. Unify frames only once every internal seam is green and stable. This is the single highest-risk phase and must be terminal + soakable.

**Q: which phase touches the live `log.bind` pipeline?** → **the MIDDLE, and behind a shim, never a raw swap.** `log.bind` fires at gateway line ~161 before everything; a window where `log.info` no-ops is catastrophic and invisible. So `EventBus` stays bound and internally delegates to the new bus first; the raw `log.bind(LogToBusAdapter)` swap is the *last internal* step, gated on byte-identical `store.query` rows.

```
P0  Harness + frozen invariants. No product code.          revert: delete tests
P1  Extract PeerTransport (shared _peers/_send_to/attach/  revert: inline back
    detach/handle_frame). Both syncers delegate. Behavior
    identical, existing tests green. Banks the biggest
    real dedup at near-zero risk. Does NOT touch wire/log.
P2  Land bus/ core (Message, MessageBus, Subscriber,       revert: delete pkg
    Local/RemoteSubscriber) unused. Own unit tests only.
P3  Store-subscriber behind EventBus. EventBus.publish     revert: 1 file
    delegates store-write to StoreSubscriber, still sync,
    still first. Byte-identical rows. [touches store path]
P4  EventBus becomes thin adapter over MessageBus;         revert: 1 file
    web_stream/notifier/EventSyncer re-subscribe via bus.
    log.bind still binds EventBus. [middle log phase,
    shimmed — pipeline never red]
P5  Chat rides same bus (chat.* ephemeral topics).         revert: 1 file
    WebChannel._publish -> bus.publish; ChatBus subscribe
    -> bus.subscribe(LocalSubscriber). Demand/refcount/
    owner-pump preserved.
P6  Merge syncers into EventReplicator+ChatReplicator      revert: old wiring
    siblings over PeerTransport; ONE bus_wiring, drop the  stays in git
    event-then-chat chain.
P7  Unify wire frame -> {topic,payload,ts}; single topic-  revert: version gate
    routed handle_frame. Version-gated for mixed cluster.
    [LAST — wire contract] + soak.
P8  Drop EventBus shim; log.bind(LogToBusAdapter). Rewrite  revert: 1 file
    white-box tests to new boundary. Net test count up.
```

Each phase: `uv run pytest -x -q` ≥ 886, all tester invariants green. Nothing omitted; P7 is *sequenced* last, not skipped.

## Divergence 2 — class structure: refined, with signatures

The architect leaned toward more unification. I still say **one bus API, one shared transport, two replication coordinators** — but let me show the interfaces so the architect can judge "unified enough," and address the "two classes = two buses?" objection head-on.

```python
# bus/message.py
@dataclass(frozen=True)
class Message:
    topic: str          # "events.<category>" | "chat.<machine>.<bot>.<chat_id>"
    payload: dict       # opaque to the bus
    ts: float

# bus/core.py  — content-agnostic, ~50 lines
class MessageBus:
    def subscribe(self, topic_pattern: str, subscriber: "Subscriber") -> "Subscription": ...
    def publish(self, topic: str, payload: dict, ts: float | None = None) -> None: ...
    # sync local fan-out, ordered for-loop; durable topics enrich-first (store-subscriber)
    def attach_link(self, peer_key: str, link: "PeerTransport") -> None: ...
    def detach_link(self, peer_key: str) -> None: ...

# bus/subscriber.py
class Subscriber(Protocol):
    def deliver(self, message: Message) -> None: ...     # SYNC entry (put_nowait or callback)
class LocalSubscriber:   # wraps asyncio.Queue, put_nowait, drop-on-full
class RemoteSubscriber:  # bounded queue + ONE pump task -> await link.send(frame)

# cluster/peer_transport.py  — the extracted shared link, ~50 lines
class PeerTransport:
    def attach_peer(self, peer_key: str, send_frame: SendFrame) -> None: ...
    def detach_peer(self, peer_key: str) -> None: ...
    async def send_to(self, peer_key: str, frame: dict) -> None: ...   # was _send_to, dedup'd
    async def handle_frame(self, peer_key: str, frame: dict) -> bool: ...  # topic-routed dispatch
```

**On "two classes = two buses?"** No — and here is the falsifiable criterion. The owner's "one bus" means: **one publish/subscribe API, one wire frame, one link, one wiring, one `handle_frame` dispatch.** All five are singular in my design. `EventReplicator` and `ChatReplicator` are **not two buses** — they are two *subscribers* to the one bus, exactly like `TelegramNotifier` and `EventStreamSubscriber` are already two subscribers nobody calls "two buses." They publish/subscribe through the same `MessageBus` and forward through the same `PeerTransport`. What differs is their *replication policy* (broadcast+cursor vs demand+refcount) — and that difference is **essential, not incidental**: there is no shared branch between "resync backlog via store cursor + gossip to all" and "refcount demand + route to one." I challenged the architect in R1 to show the pseudo-code where those share a branch; absent that, forcing them into one class produces `if policy == "broadcast": ... else: ...` down every method — two classes wearing a trenchcoat, strictly worse to read than two named siblings.

So: **unify the five singular things (that IS one bus); keep the two policies as two named subscriber-coordinators.** If the architect can produce the shared replication algorithm, I'll fold them. I don't believe it exists.

## Divergence 3 — store as subscriber: refined mechanics

Confirming and sharpening how "privileged synchronous store-subscriber" coexists with "content-agnostic bus":

- The bus core supports **one policy knob only**: per-topic `durable: bool` (declared at subscribe/registration, tester INV-A3). It knows nothing about `Event`, `origin_seq`, or SQLite.
- For a **durable** topic, `publish` runs the registered **first, synchronous** subscriber (the store-subscriber) before the rest, and the enriched payload (now carrying `id`/`origin_seq`) is what downstream subscribers receive. This is not "the store is a peer of the SSE subscriber" — it is a **declared ordered-first synchronous slot** the bus honors for durable topics. Mechanically identical to what `EventBus.publish` does today (verified: `bus.py:47` insert_local → line 55 ordered for-loop).
- **Who owns it:** the events coordinator owns the `EventStore` object and registers it as that first-slot subscriber. The bus provides the *slot*; the coordinator provides the *store*. This keeps `origin_seq` assignment in the store (architect R4 — we agree it must NOT move to the envelope constructor) and all cursor/dedup/resync in the store, untouched.
- **Ephemeral** topics (`chat.*`) have `durable=False` → the store-subscriber never subscribes there → stream_delta physically cannot reach SQLite. Wiring fact, not runtime check (constraint 1 honored by construction).

**Answer to tester Q5 (and R1 risk 1):** Yes. **`EventStore` remains the sole SQLite write point** — verified in code: only `insert_local` (`storage.py:65`) and `insert_remote` (`storage.py:105`) INSERT; `mark_read` (line 254) is the lone other mutation, already store-owned. No new write path is introduced by this design. Your `CountingEventStore` spy subclassing those two methods will catch 100% of writes, so INV-A2's "chat publish → insert delta == 0" assertion is airtight. If any phase adds a second write path, that's a red flag we stop on.

## New insight

The `durable=True` first-slot subscriber and the "no `create_task` per event" invariant are the **same constraint viewed twice**: both demand that `publish` stay synchronous and ordered. Collapsing them into one enforced rule — *"local fan-out is always a synchronous ordered for-loop; all async lives in RemoteSubscriber's single pump task"* — gives the tester one thing to assert (INV-E1/E2 ordering + INV-A1 write-before-return) that simultaneously guards 坑 #1, pagination, and dedup. One rule, three footguns closed.

## Revised position (net)

Withdraw the big-bang objection: full unification, done as 9 revertable phases, each green — same end-state, safer diff. Wire frames LAST (mixed-cluster contract), `log.bind` in the middle behind a shim. Keep one bus API / one link / one wiring / one dispatch (that is genuinely "one bus"), with two policy subscribers, unless the architect shows a shared replication branch. Store stays the sole SQLite writer in a declared synchronous first-slot; tester's spy is airtight.
