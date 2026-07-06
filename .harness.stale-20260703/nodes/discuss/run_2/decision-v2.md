# DECISION v2 — Fold RPC into the unified bus (RPC-inclusive convergence)

> Facilitator synthesis of run_2 Round 1 (architect / engineer / tester). The re-analysis, mandated by the owner rejecting run_1's RPC-out-of-scope, **converged** on folding RPC in. This document is the executable contract for the RPC-inclusive design. It **synthesizes only**; it adds no new scope. It is a **delta on top of run_1's `decision.md`** — everything run_1 settled STANDS unless this document explicitly changes it.

---

## 0. What STANDS from run_1 (unchanged, do not re-litigate)

The entire run_1 settled design is load-bearing and carries forward byte-identical:

- **Content-agnostic `MessageBus` core** (`bus/core.py`): routes on `topic` only, `payload` opaque, envelope `{topic, payload, ts}`, synchronous ordered local fan-out, no `create_task` per message (坑 #1).
- **Composed `PeerTransport`** (`cluster/peer_transport.py`): shared `_peers` + `send_to` + `attach_peer`/`detach_peer` + topic-routed `handle_frame`, extracted once by composition.
- **`Local`/`Remote` Subscriber abstraction** (`bus/subscriber.py`): `LocalSubscriber` (in-process queue, drop-on-full) + `RemoteSubscriber` (bounded queue + one pump task awaiting `link.send`).
- **Sibling `EventReplicator` + `ChatReplicator`** over a shared `PeerTransport` — NOT a strategy class, NOT base+subclass. No shared replication algorithm.
- **`StoreSubscriber` = privileged synchronous first-slot subscriber**; `EventStore` sole SQLite writer; durability = a per-topic subscriber-list fact, not an envelope field.
- **Neutral `bus/` package** that `events/` and `cluster/` both depend on; `events/` ↔ `cluster/` mutual import stays 0.
- **`boxagent.log` facade byte-identical**; `LogToBusAdapter` the bound sink.
- **Phase 0 harness + frozen invariants first, no product code.** Test floor 886, never drops, net rises.

All of run_1's consensus points 1–10, resolved disagreements 2.1–2.3, and risk mitigations R1–R6 remain in force. This document changes exactly one thing.

---

## 1. THE DELTA — RPC is folded in

**run_1 scoped RPC out.** The owner rejected that. run_2 re-analyzed with RPC IN scope and under the mandate that unification must **net-reduce** production LOC. All three agents converged: **RPC folds in, and it is folding RPC in that makes the whole effort net-negative.** Chat+event alone was the **+306 trap** — it built the entire bus core, subscriber abstraction, and PeerTransport but could not delete the single biggest duplication in the tree (RPC's role-split mirror). RPC is the tenant that makes the already-paid-for infrastructure rent worth it.

The delta, precisely:

- run_1's neutral `bus/` core, `PeerTransport`, `Local`/`Remote` subscribers, sibling `EventReplicator`/`ChatReplicator`, `StoreSubscriber`, and neutral package layout **all stand unchanged**.
- RPC (today: `cluster/rpc.py` + the role-split `GuestSession`/`GuestClient` call+loopback pairs) is unified onto the **shared transport layer** run_1 already extracts.
- One new sibling coordinator (`cluster/rpc_over_bus.py`), one new shared executor (`cluster/inbound_request_executor.py`), and the collapse of the role-split mirrors.
- Phase 7 (wire-frame unification) flips from "sequenced-last-but-arguably-deferrable" to **MANDATORY and load-bearing** — it now delivers real net reduction, not tidiness.

**The `bus/` core NEVER gains an "rpc" concept.** This is the invariant that keeps the fold clean.

---

## 2. HOW RPC FOLDS IN — the key architectural seam

The seam is precise and all three agents independently landed on it:

> **RPC = request/reply. It rides the TRANSPORT layer (PeerTransport + frame dispatch + wiring), NOT `MessageBus.publish`/topics.**

Why not `publish`/topics: chat/event are *fire-and-forget fan-out* over a **serial single-pump** (坑 #1 demands strict order). RPC is a *caller `await`s one correlated response* — it needs **id-correlation + concurrency**, the exact opposite of the serial pump. Putting RPC on the serial `RemoteSubscriber` pump would serialize concurrent RPCs so one slow `/api/logs` paginate blocks every other RPC. RPC is naturally per-request-id independent, at-most-once with a timeout — a **strictly weaker** consumer of ordering than chat. So it rides the transport, not the content bus.

Concretely, RPC folds in as **three pieces, all in `cluster/`, none touching `bus/` core**:

**(a) ONE role-agnostic `call`** — `cluster/rpc_over_bus.py`. Mint `rpc_id` (uuid hex) → park a `Future` in a **per-link** `_pending` map → `send(frame)` via the transport's route-to-one → `await wait_for(future, timeout)` → `_resolve` on the reply → reject-all-pending on link drop. Host and guest are **identical** — the role-split (`GuestSession.call` at registry.py:81–105 vs `GuestClient.call` at guest_client.py:87–115) was the ONLY reason this existed twice. `_pending` stays **per-link** (as today), not bus-global, or reject-on-disconnect grows a peer-scan (the one way this layer balloons — see §3 drift risk).

**(b) ONE shared `InboundRequestExecutor`** — `cluster/inbound_request_executor.py`. The loopback re-issue collapsed from host `_serve_inbound_rpc` (registry.py:211–269, 59 lines) + guest `_handle_rpc` (guest_client.py:315–355, 41 lines). One algorithm: read `{id,method,path,query,body}` → `http://127.0.0.1:{local_web_port}{path}` + `Bearer` header → `session.request(...)` → JSON-or-raw the response → send back `rpc_resp` → except→502. The three differences (HTTP-session field, log identity/category, host-only "loopback not configured"→503 guard) are trivially parameterized: `serve_inbound_request(send, http_session, local_web_port, local_web_token, role, fail_category, request_frame)`. **No behavioral fork survives** — the host's 503 guard is universally safe (a no-op on guest, whose port is always set). This is the highest-confidence collapse in the plan: genuinely ONE body, unlike the syncer merge which keeps two real policy bodies.

**(c) Routing via the SAME `route()`-to-one primitive chat uses.** An RPC goes to exactly one machine — like `ChatReplicator._send_toward` → `route()` → `_send_to`, NOT like `EventReplicator`'s broadcast. The two-hop host relay chat already owns serves RPC for free: `dispatch_machine_request`'s host branch (pick guest via registry) and guest branch (always host) IS chat's `route()` decision minus the response plumbing. RPC reuses it, adds no new transport.

**The facade is preserved.** `dispatch_machine_request`'s **~15 call sites** in `transports/web/server.py` (each `response = await self.cluster_rpc.dispatch_machine_request(...); if response is not None: return response`) keep calling ONE facade method, now backed by `rpc_over_bus.call` instead of the role-branch. The web server never learns the bus exists.

`rpc_over_bus` is the **third sibling coordinator** — the request/reply policy alongside `EventReplicator` (broadcast) and `ChatReplicator` (demand). Three named policies over one transport; no shared algorithm forced. Exactly run_1's resolution-2.2 pattern, extended by one.

---

## 3. THE RECOMPUTED NET-LOC LEDGER (engineer's verified numbers)

Every number below was read out of the tree and re-counted (`wc -l` + comment/blank-stripped), not carried from prose. Scope = the in-scope RPC/transport/wiring surface (Framing A — spans that get deleted/rewritten), NOT the ~370 lines of replication *policy* that move nearly verbatim (Framing B's ~1031 would fake a dishonest reduction).

```
BEFORE (surgical in-scope, RPC included):   ≈ 664 raw
AFTER:                                       ≈ 807 raw
   of which ≈ 230 is MOVED replication policy (EventReplicator 120 + ChatReplicator 110),
   not new code — held constant on both sides.
─────────────────────────────────────────────────────────────
Honest unification net (policy held constant): ≈ −90 to −115 lines
                                     ≈ −100, ±25% (roughly ±30 lines)
```

The naive `807 − 664 = +143` is the **same trap chat-only fell into**: it double-books the moved policy as "new." Holding policy constant and comparing only what actually unifies gives the honest **≈ −100**. It is negative **ONLY because RPC is in-scope** — chat+event alone banked ~−73 wiring + ~−25 transport ≈ −98 of savings against +122 of new bus core = **roughly neutral-to-positive (the +306 trap)**. RPC's ~−140 of mirror-collapse is what pays for the bus core.

**The 3 dominant reductions:**

1. **Loopback-executor collapse: ~−48.** `_serve_inbound_rpc` (59) + `_handle_rpc` (41) = 100 mirror lines → one ~52-line parameterized executor. The single biggest banked line; pure mirror redundancy the role-split forced. **This is why RPC must be in scope** — it is where the net actually goes negative.
2. **Dual→single wiring: ~−73.** `sync_wiring.py` (46, direct-assign) + `chat_sync_wiring.py` (82, chained fall-through) → one non-chained `bus_wiring.py` (~55). Deletes the fragile install-order chain outright (坑: chat wiring MUST install after event wiring).
3. **RPC proxy + call mirror collapse: ~−41 to −55 combined.** `_proxy_via_host` (18) + `_proxy_to_remote` (20) → one `_proxy` (~13); `GuestSession.call` (25) + `GuestClient.call` (29) → one shared `call` (~27, incl. `_resolve` + `_PendingResponse` + reject).

**The 2 drift risks that could pull it toward neutral:**

1. **The all-new `bus/` core is +122 with NO BEFORE counterpart.** `message.py` + `core.py` + `subscriber.py` are genuinely additive — there is no existing "content-agnostic bus" (`EventBus` is event-typed). If the `RemoteSubscriber` pump + prefix-match + `Subscription.close` run heavy, +122 becomes +150 and eats a third of the win.
2. **The additive request/reply-over-bus layer (~38) is tax, not pure dedup.** Chat/event never needed correlation+timeout+reject-on-disconnect. It is smaller than the 54 it replaces (net win), but it is the item most likely to balloon — specifically if `_pending` goes **bus-global keyed by `(peer, rpc_id)`** instead of per-link, forcing reject-on-disconnect to scan-filter (+15). **Mitigation: keep `_pending` per-link (mirrors today exactly).**

**Uncertainty band, stated plainly:** optimistic −140 (bus core lands at 45 not 55, request/reply collapses to 32, bus_wiring hits 45); pessimistic −40/near-neutral (RemoteSubscriber pump + correlation each run 15 over, bus_wiring needs per-frame-type shims for Phase-7 mixed-version). **Honest range: −90 to −115, ≈ −100, do not quote a fake-precise number.** If Phase 7 is skipped, dominant-reduction item 3's frame-dispatch portion survives and the net drifts toward the pessimistic −40 — another reason Phase 7 is not optional under the net-reduction mandate.

---

## 4. PHASE 7 IS NOW MANDATORY — the devil's-advocate objection COLLAPSES

run_1's devil's advocate argued Phase 7 (wire-frame unification) was "pure risk, no benefit" — **explicitly because RPC was out of scope.** With only event+chat frames on the WS, you *could* leave two vocabularies coexisting behind a chained `on_unknown_frame` fall-through, and the merge would be cosmetic.

**With RPC in scope, that argument dies for a concrete structural reason: there are already THREE wire vocabularies on the one WS today, not two:**

1. `rpc` / `rpc_resp` / `ping` / `pong` / `hello` / `welcome` / `bots_update` / `machines_snapshot` — the **registry/guest_client core switch** (registry.py:353–399, guest_client.py:282–302), dispatched **inline** as the privileged built-in vocabulary.
2. `event_batch` / `event_resync` — EventSyncer, a bolt-on behind the fall-through chain.
3. `chat_subscribe` / `chat_unsubscribe` / `chat_event` — ChatSyncer, a bolt-on behind the fall-through chain.

RPC is the privileged built-in (hard-coded because it was there first); event/chat got the fragile chained-hook treatment. **Once RPC becomes a `route()`-to-one publish handled by topic dispatch, `handle_ws` can no longer special-case `rpc`/`rpc_resp` inline.** You cannot have `rpc` on ONE topic-routed dispatch AND `event_batch`/`chat_event` on a DIFFERENT chained fall-through on the same socket — that is **two dispatchers on one socket**, precisely the ambiguity Phase 7 exists to kill. With RPC in, the frame dispatch is **either fully unified or it is a three-way chain**, and a three-way chain is strictly worse than the two-way one run_1 already called fragile. **There is no stable half-state.**

Phase 7 now deletes *real structure*, not paint: the inline `rpc`/`rpc_resp` special-case in BOTH `handle_ws` and `_serve`, the entire `on_unknown_frame` chained-hook machinery, and the "chat wiring must install after event wiring" ordering footgun. **It delivers the ~−40 from collapsing three dispatchers to one** (dominant-reduction item 3's frame portion), which only unlocks *because* Phase 7 lands. Leaving the vocabularies split is strictly worse than today. The devil's-advocate "no benefit" was load-bearing on the RPC-out assumption and collapses with it.

---

## 5. REVISED PHASED PLAN

run_1's P0–P8 carry forward. The RPC work inserts as a new phase after `PeerTransport` is extracted, and Phase 7 becomes load-bearing. Every phase stays revertable with its gate. **Every phase gate = (full frozen invariant set green) + (that phase's named no-go green) + (`uv run pytest -x -q` passed ≥ 886, net count rising).** The frozen invariant set never shrinks.

| Phase | Work | Change from run_1 |
|---|---|---|
| **P0** | Harness + frozen invariants + migration map, no product code. **Now also builds the RPC harness dimension** (§5a) and freezes INV-R1..R6 + INV-DR1..DR4 against OLD code before any RPC move. | EXTENDED |
| **P1** | Extract `PeerTransport` (shared `_peers`/`send_to`/attach/detach/`handle_frame`). | unchanged |
| **P1.5 (NEW)** | **RPC onto the shared transport.** Collapse `call`×2 → one role-agnostic `call` + per-link `_pending` correlation + `_resolve` + reject-on-disconnect into `rpc_over_bus.py`; collapse loopback `_serve_inbound_rpc`+`_handle_rpc` → one `InboundRequestExecutor`; collapse `_proxy_via_host`/`_proxy_to_remote` → one `_proxy`. `rpc`/`rpc_resp` frames keep their literal form for now. Both `GuestSession`/`GuestClient` delegate. Pure delegation, near-zero risk, banks the biggest dedup (~−140-ish) before touching any content abstraction. RPC keeps its **independent concurrent model** (per-inbound-rpc task, NOT the serial pump — §5b). | **INSERTED** (after P1, before bus core; must precede P6 so merged coordinators + RPC share one transport) |
| **P2** | Land `bus/` core (message + core + subscriber), unused. | unchanged |
| **P3** | `StoreSubscriber` behind existing `EventBus` (sync, first-slot). | unchanged |
| **P4** | `EventBus` → thin adapter over `MessageBus` (shimmed, `log.bind` still binds `EventBus`). | unchanged |
| **P5** | Chat rides the same bus (`chat.*` ephemeral topics). | unchanged |
| **P6** | Merge syncers → `EventReplicator` + `ChatReplicator` siblings; ONE `bus_wiring.py`. **Now also folds `rpc`/`rpc_resp` frame arms into `PeerTransport.handle_frame`**, leaving role-specific handshake/lifecycle arms (hello/welcome/bots_update/machines_snapshot) in host `handle_ws` / guest `_serve`. | EXTENDED (absorbs RPC frame arms) |
| **P7** | **Unify the wire frame → topic-addressed `{v, topic, payload, ts}` + typed control frames, carrying rpc+chat+event on ONE topic dispatch.** `rpc`/`rpc_resp` ride as **typed control frames** on the `v:2` envelope (request/reply, not topic-fanout — like event batch/resync). **LOAD-BEARING, not optional** (§4). Version-gated (`v` byte); a `v:1` node drops an unparseable `v:2` frame — now across **three** frame families, not two. | **NOW MANDATORY / load-bearing** |
| **P8** | Drop the `EventBus` shim; `log.bind(LogToBusAdapter)`; rewrite white-box tests; migrate (never delete) old syncer/rpc tests into the frozen layer. | unchanged |

### 5a. Harness extension (P0, the largest structural addition)

run_1's harness modeled **one-way fan-out** (`publish_*` → remote subscriber queue/store). RPC is **request→reply round-trip + correlation + two-hop nesting** — run_1's primitives cannot express it. P0 adds:

- **`node.serve_web(path, handler)`** — register fake local web handlers the loopback executor re-issues against (spy-wrapped to record called + method/path/query/body); proves the executor hits a **real** handler, not a harness-faked reply.
- **`node.rpc(target, method, path, query, body, timeout) -> (status, body)`** — the RPC publish point, through real `dispatch_machine_request` → transport route-to-one → remote loopback executor → real handler → correlated reply.
- **`node.pending_rpc_count()`** — read-only observation of the reply-waiter table length (no private-state peek); INV-R5 asserts it hits 0 after timeout.
- **`cluster.hold_replies(node, predicate)` / `release_replies()`** — hold/reorder reply injection; INV-R4 (concurrent isolation) and INV-R5 (never-reply→timeout) use it. RPC's analog of run_1's `reorder_tasks`. Must be proven RED against a deliberately-broken "match reply by topic not id" implementation before it is trusted.
- **Two-hop nesting** on `ThreeNodeCluster`: the host node's `serve_web` `/api/history` handler itself calls `dispatch_machine_request("gB", ...)`, so the host's loopback executor really re-issues onward to gB.

### 5b. The new RPC invariants (frozen layer, gates on P1.5/P6/P7)

Freeze in `test_message_bus_invariants.py` against OLD code first (P0 discipline). All external-boundary only (store rows / queue contents / frames / returned body); never peek `_pending`/`_subscribers`/`_pumps`.

- **INV-R1** — single-hop RPC to machine B returns B's **real** HTTP response body (row-for-row), correlated by id — not a stub/echo/product-of-the-correlation-mechanism.
- **INV-R2** — the collapsed loopback executor still reaches the **real** `_handle_web_*` handler with method/path/query/body transparently threaded (proves "one shared executor" isn't "one shared empty shell").
- **INV-R3** — two-hop RPC (guest A → host → guest B) returns B's correct body; host does not tamper; the **two independent `(rpc_id, _pending)` pairs** (A↔host, host↔B) correlate without crossing. *(The RPC analog of run_1's INV-A2 — a by-construction fact a "tidy-up" can silently violate; see §6.)*
- **INV-R4** — 50 concurrent in-flight RPCs (distinct paths, distinguishable bodies), handlers returning **out of order**, each `dispatch_machine_request` gets exactly its own body, zero cross-talk. Must go RED if reply is matched by topic rather than precise id.
- **INV-R5** — unreachable-machine RPC times out cleanly (504, no hang) **and** leaves zero pending-future residue (`pending_rpc_count() == 0`).
- **INV-R6** — concurrent RPCs are not blocked by each other: a slow RPC (handler sleep) + a fast RPC → the fast one returns first. Nails "**RPC is NOT on the serial single-pump**" as an invariant.
- **INV-DR1** — RPC frames do NOT leak to chat/event subscribers and vice versa; on the unified `v:2` dispatch, an RPC request only triggers the loopback executor, a chat delta only enters the chat queue, an event only enters the store. **Phase 7 head no-go.**
- **INV-DR2** — an RPC reply is point-to-point back to the originator's pending future, never gossiped/fan-out/stored (Phase 7 gate).
- **INV-DR3** — RPC request/reply never lands in the event store (`CountingEventStore` delta == 0 over 100 RPCs) — RPC topics are ephemeral (no `StoreSubscriber`); the RPC version of INV-A2.
- **INV-DR4** — reconnect: **three recovery semantics coexist and are each correct** in one drop→relink: (a) events backfill via cursor, (b) chat re-subscribes (dropped deltas allowed lost), (c) **an in-flight side-effecting POST is immediately rejected to the caller on drop — NEVER replayed after relink.** The strict superset of run_1's INV-D1. Replaying an in-flight `/api/send` or `/api/admin/restart` as backlog is the catastrophic failure this freezes out.

**Gate placement:** P1.5 head no-go = **INV-R4** (concurrent id-correlation isolation) + secondary **INV-R2** (loopback still hits real handler), plus INV-R1/R3/R5/R6/DR3 green. P6 no-go adds INV-RPC-frame (an `rpc` frame and a `chat_event` frame on one WS both dispatch, neither swallows the other — INV-D2 generalized to three families). P7 head no-go = **INV-DR1** (+ INV-DR2, + mixed-version now covering rpc/chat/event **three** families). P7's reconnect gate = **INV-DR4** (the three-semantics reconnect).

---

## 6. TOP RISK + MITIGATION

**The loopback re-issue is a hidden control-flow cycle the fold must PRESERVE, not "simplify."**

`InboundRequestExecutor` (the collapsed `_serve_inbound_rpc`/`_handle_rpc`) deliberately **re-enters the local web server over HTTP** so the host's `_handle_web_*` handlers — which themselves call `dispatch_machine_request` — provide **two-hop guest→host→guest relay for free**. An inbound `rpc.<host>` request targeting guest-B is executed on the host's web port, whose handler calls `dispatch_machine_request("guest-B", ...)` → routes onward to B. The recursion is topology-bounded (hub-and-spoke, max two hops), not code-bounded, and it is load-bearing and non-obvious.

**The tempting "it's one bus now, publish in-process" simplification silently breaks the relay.** Dispatching the inbound RPC directly to the target topic in-process — skipping the HTTP loopback — skips the web handler's **auth**, its **machine-resolution**, and its **onward `dispatch_machine_request`**, so a host-mediated guest→guest RPC silently 404s or mis-routes. It also skips endpoints' own local enrichment before/after the remote fetch. The recursion exists today and the fold does not deepen it — but it must not be "optimized" away.

**Mitigation:** keep `InboundRequestExecutor` a **genuine HTTP loopback** against `127.0.0.1:<local_web_port>` — do NOT collapse it into an in-process bus publish. Freeze a **two-hop-relay invariant (INV-R3)** as the RPC analog of run_1's INV-A2 (chat-never-hits-SQLite): a `ThreeNodeCluster` test where guest-A issues an RPC targeting guest-B, asserting it transits host and returns B's response, with the host's web port confirmed re-entered. A by-construction fact a reviewer's "tidy-up" can silently violate.

**Secondary risk (lower):** reply-correlation must reject in-flight futures on peer detach in ONE place (the RemoteSubscriber/peer teardown) — guest_client.py:255–260 does this today; it must **move, not vanish**, or a dropped link leaves RPC callers hanging until the 30s timeout. This also fixes the **asymmetry footgun**: guest side has disconnect-cleanup, host `GuestSession` does not. The fold must **decide and test** symmetric disconnect-reject (recommended) — guarded by INV-DR4 (c) and NEG-R5, and it must be pinned by a test, never silently changed.

---

## 7. UPDATED ACCEPTANCE CRITERIA

run_1's criteria 1–7 all carry forward. Fold in the RPC criteria:

1. **One transport carries rpc + chat + event on ONE topic-routed dispatch.** `grep` shows the inline `rpc`/`rpc_resp` switch in `handle_ws`/`_serve` GONE, the `on_unknown_frame` chained-hook machinery GONE, `sync_wiring.py` + `chat_sync_wiring.py` GONE, and the copy-pasted `_send_to` / dual `call` / dual loopback executor GONE. One `bus_wiring.py`, one `handle_frame`, one `v:2` frame.
2. **Net production LOC strictly DOWN vs current HEAD** (target ≈ −100, honest range −90 to −115), verified by line count with moved replication policy held constant on both sides.
3. **RPC invariants green.** INV-R1..R6 (single-hop real body, real-handler reuse, two-hop nested correlation, 50-concurrent id-isolation, timeout-no-leak, not-on-serial-pump) + INV-DR1..DR4 (topic isolation, reply-not-broadcast, never-stored, three-semantics reconnect with immediate-reject of in-flight side-effecting POST) all green.
4. **`bus/` core never gained an "rpc" concept** — verifiable by grep: no "rpc" string in `bus/`; RPC lives entirely in `cluster/`, touches `bus/` only via publish/subscribe/transport.
5. **The ~15 `dispatch_machine_request` call sites in `transports/web/server.py` are byte-for-byte unchanged** — the web server never learns the bus exists.
6. **RPC never lands in SQLite** (INV-DR3) and **is never serialized behind the chat/event pump** (INV-R6).

run_1's criteria (one bus / five singular things; stream_delta never touches SQLite; event durability + ordering; reconnect recovers both halves; facade byte-identical; clean dependency fan-in; test baseline only rises) remain in force alongside these.

---

## 8. Through-line (one paragraph)

Everything run_1 settled STANDS; the one delta is RPC folds in, and it is the fold that makes the unification NET-REDUCE code (chat+event alone was the +306 trap). RPC = request/reply and rides the **transport** (PeerTransport + frame dispatch + wiring), NOT `MessageBus.publish`/topics — it needs id-correlation + concurrency, the opposite of chat/event's serial single-pump. It folds as a third sibling coordinator `cluster/rpc_over_bus.py` (one role-agnostic `call`), one shared `InboundRequestExecutor` (the loopback re-issue collapsed from host+guest), and the same `route()`-to-one + two-hop-host-relay primitive chat already owns; the `bus/` core never gains an "rpc" concept and the ~15 `dispatch_machine_request` call sites keep their facade. The recomputed net is **≈ −100 lines (−90 to −115, ±25%)**, driven by loopback-executor collapse (−48), dual→single wiring (−73), and RPC proxy+call mirror (−41..−55), with two drift risks (the all-new +122 bus core and the additive request/reply layer). Phase 7 flips from optional-pure-risk to MANDATORY: with RPC in there are THREE dispatchers on one socket, so leaving them split is strictly worse than today — Phase 7 delivers the −40 from collapsing them to one. The top risk is the loopback re-issue's hidden control-flow cycle: keep `InboundRequestExecutor` a real HTTP loopback so two-hop relay stays free, and freeze INV-R3 as the RPC analog of run_1's INV-A2.
