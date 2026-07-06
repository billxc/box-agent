# ROUND 2 — ARCHITECT VERDICT: Fold RPC into the unified bus

> Owner rejected run_1's RPC-out-of-scope. Re-evaluating with RPC IN SCOPE, and with the
> mandate that unification must NET-REDUCE code. This builds on run_1's settled design
> (content-agnostic MessageBus + composed PeerTransport + Local/RemoteSubscriber + sibling
> EventReplicator/ChatReplicator + EventStore as privileged sync subscriber + neutral `bus/`
> package). I do not re-litigate any of that. I only add the RPC fold.

---

## 0. What I actually read (grounding)

| File | Lines | The load-bearing RPC surface |
|---|---|---|
| `cluster/rpc.py` | 108 | `dispatch_machine_request` (30–57), `_proxy_via_host` (59–76, guest→host), `_proxy_to_remote` (78–97, host→guest), `handle_guest_ws` (99–108) |
| `cluster/registry.py` | 438 | `GuestSession.call` (81–105), `_resolve` (107–110), `_PendingResponse` (59–69), `_serve_inbound_rpc` (211–269, HOST loopback re-issue), `handle_ws` frame-dispatch (271–438, the `rpc`/`rpc_resp`/`ping`/`bots_update`/`on_unknown_frame` switch at 353–399) |
| `cluster/guest_client.py` | 355 | `call` (87–115, GUEST→host), `_handle_rpc` (315–355, GUEST loopback re-issue), `_serve` frame-dispatch (275–313), pending-reject on disconnect (255–260) |
| `events/sync.py` | 248 | `EventSyncer` — role-agnostic, `_send_to` 227–234, broadcast `_flush` 210–223, gossip `_handle_batch` 130–161 |
| `cluster/chat_sync.py` | 209 | `ChatSyncer` — role-agnostic, `_send_to` 178–185, `route()`+`_send_toward` 173–176, two-hop relay `_deliver` 130–139 |
| `events/sync_wiring.py` | 46 | direct-assign hooks |
| `cluster/chat_sync_wiring.py` | 82 | **chained** hooks (must install after event wiring) |
| `cluster/chat_bus.py` | 87 | ChatBus wrapper |

Callers of `dispatch_machine_request`: **~15 call sites**, all in `transports/web/server.py`, each of the shape
`response = await self.cluster_rpc.dispatch_machine_request(machine, METHOD, PATH, request[, body]); if response is not None: return response`.
That caller shape is the *public contract* RPC must keep — it is untouched by this fold.

---

## 1. Is request/reply SOUNDLY expressible on pub/sub? — **YES, with one honest caveat**

### 1.1 The mechanics map cleanly

Request/reply on a topic-routed bus is a textbook pattern and every primitive it needs already
exists in the run_1 design:

| Request/reply needs | Bus already has |
|---|---|
| Route a request to machine M | `route(M)` → peer_key + two-hop host relay. **Chat already has this** (`ChatSyncer._send_toward` → `route()` → `_send_to`). RPC's `dispatch_machine_request` (host: pick `guest_registry.get(machine)`; guest: always `guest_client`) is **the exact same routing decision** re-implemented. `route_chat` in gateway.py:214 IS `dispatch_machine_request`'s routing, minus the response plumbing. |
| Correlate reply to request | `id` (uuid hex) + a pending-future map. `_PendingResponse` (registry.py:59) + `_pending` dict. This is pure request/reply bookkeeping, transport-independent. |
| Timeout | `asyncio.wait_for(pending.result, timeout=30)` — identical in both `call`s. |
| Reject in-flight on link loss | guest_client.py:255–260 (`set_exception`). Belongs on the RemoteSubscriber/peer detach, one place. |

So request/reply is a `publish("rpc.<target>", {id, reply_topic, method, path, ...})` on the
outbound side, and a correlation of the reply frame by `id` on the return side. The bus core
never needs to know it is carrying an RPC — `rpc.<target>` is just another topic, the reply is
just another publish on `rpc.reply.<origin>` (or carried as a typed control frame on the same
peer link). **This is genuinely sound.** The topic router + PeerTransport `send_to` + a
correlation map is a complete request/reply substrate.

### 1.2 The caveat: request/reply is NOT plain fan-out pub/sub — it is a *pattern layered on top*

Three things RPC needs that broadcast-pub/sub does NOT, and I want to be honest that these do
NOT live in `bus/` core:

1. **Correlation (id → future).** The bus core is fire-and-forget publish. The
   request→reply→resolve-the-future loop is a stateful pattern. It lives in a
   `cluster/rpc_over_bus.py` adapter, NOT in `bus/core.py`.
2. **Point-to-point delivery, not fan-out.** An RPC goes to exactly ONE machine. `EventReplicator`
   broadcasts to all peers; `ChatReplicator` routes to one via `route()`. RPC is like Chat's
   `route()`-to-one, NOT like Event's broadcast. So RPC reuses the **ChatReplicator-style
   route-to-one primitive**, not the broadcast one. Good — that primitive already exists and is
   already role-agnostic.
3. **The loopback re-issue ("reuse all HTTP handlers").** `_serve_inbound_rpc` (host) and
   `_handle_rpc` (guest) both do: take the inbound rpc frame → `aiohttp` request against
   `127.0.0.1:<local_web_port><path>` with the local bearer token → send the JSON response back.
   This is the clever bit that lets a two-hop host relay work *for free*: when the host loopback-
   reissues a request whose `machine` targets yet another guest, the host's own
   `_handle_web_*` handler calls `dispatch_machine_request` AGAIN and proxies onward. **This trick
   is orthogonal to transport** — it is "execute this request locally and give me the response
   dict." Pub/sub does not give it and does not need to; it stays as ONE shared function.

**Does RPC need ordering / exactly-once that pub/sub can't give?** No. RPC is already
*per-request-id independent* — each request has its own future, order between requests is
irrelevant (unlike stream_delta where order is 坑 #1). RPC is naturally at-most-once with a
timeout; the loopback re-issue is idempotent-agnostic (it's HTTP the caller already accepted could
be retried). So RPC actually needs **LESS** ordering guarantee than chat. The single-pump ordering
rule from run_1 (坑 #1) is a *superset* of what RPC needs — RPC rides it for free and imposes no
new constraint. **This is the strongest soundness signal: RPC is a strictly weaker consumer of the
bus than chat is.**

### VERDICT 1: Request/reply is soundly expressible. The route-to-M primitive that chat already
owns serves RPC directly. RPC needs three things beyond fan-out (correlation, point-to-point,
loopback-reissue) — all three are patterns that live in a thin `cluster/rpc_over_bus.py` adapter,
none of them belong in or contaminate `bus/` core. RPC imposes NO new ordering/delivery
requirement the bus doesn't already meet for chat.

---

## 2. Clean design or leaky? — **CLEAN, if the loopback re-issue is extracted as ONE shared inbound executor**

The single biggest architectural question. My answer: it is clean, and the fold actually
*forces* a cleanup that is overdue.

### 2.1 The loopback re-issue must become ONE `InboundRequestExecutor`

Today `_serve_inbound_rpc` (registry.py:211–269, ~58 lines) and `_handle_rpc`
(guest_client.py:315–355, ~40 lines) are **near-identical**:

- both build `url = f"http://127.0.0.1:{local_web_port}{path}"`
- both set `Authorization: Bearer {local_web_token}`
- both `self._http_session.request(method, url, params=query, json=body)`
- both `response.json(content_type=None)` with raw-text fallback
- both send back `{"type":"rpc_resp","id",...,"status","body"}`
- both `log.warning(...RPC_FAIL...)` on exception

The ONLY differences: (a) host uses `Category.CLUSTER_HOST_RPC_FAIL`, guest uses
`CLUSTER_GUEST_RPC_FAIL`; (b) host lazily creates `_http_session`, guest asserts it exists; (c)
host guards `if not self.local_web_port`. These are trivial. This is a textbook copy-paste that
the role-split forced.

**Post-fold this is ONE class:** `cluster/inbound_request_executor.py` — "given an inbound request
dict `{id, method, path, query, body}`, execute it against the local web port, return the response
dict." It is transport-agnostic and role-agnostic; it does not know host from guest. The Remote
subscriber for the `rpc.<self>` topic hands each inbound request to it and publishes the reply.
**This is clean** — it is a pure function of (local_web_port, local_web_token, request) → response,
with zero coupling to who sent it or over which link. It is exactly the kind of thing that should
be extracted once, and the role-split is the ONLY reason it exists twice today.

### 2.2 Where does request/reply live? — `cluster/`, NOT `bus/`

- `bus/core.py` stays content-agnostic: topics, payloads, sync fan-out, link registry. It never
  sees the word "rpc."
- `cluster/rpc_over_bus.py` (NEW) owns the request/reply *pattern*: the pending-future map
  (`_PendingResponse` moves here), `call()` (ONE copy — see §4), correlation on reply, timeout,
  reject-on-detach. It publishes requests via the bus's route-to-one primitive and subscribes to
  its own reply topic.
- `cluster/inbound_request_executor.py` (NEW) owns the loopback re-issue (ONE copy).

This is the same dependency shape run_1 already blessed: policy/pattern lives in `cluster/`,
neutral mechanism lives in `bus/`. RPC-over-bus is a *third sibling coordinator* alongside
`EventReplicator` (broadcast) and `ChatReplicator` (demand). It is the **request/reply** policy.
Three named policies over one transport — no shared algorithm forced, exactly the run_1 resolution
2.2 pattern, extended by one.

### 2.3 The one genuine leak-risk (name it honestly)

The loopback re-issue means an inbound RPC re-enters the local web server, whose handler may call
`dispatch_machine_request` → publish another `rpc.<other>` → two-hop. In the OLD code this
recursion is bounded by hub-and-spoke topology (guest→host→guest, max two hops) and works because
each hop is a fresh HTTP request. On the bus, the same recursion must remain bounded. **This is not
new** — the recursion exists today and is topology-bounded, not code-bounded. The fold does not
deepen it. But a reviewer must not "optimize" the loopback away into a direct in-process call that
skips `dispatch_machine_request`, or the two-hop relay silently breaks. I flag it as a load-bearing
invariant (see §6), not a defect.

### VERDICT 2: Clean. The fold FORCES extraction of the duplicated loopback re-issue into ONE
`InboundRequestExecutor` (that is a strict improvement, not a compromise), request/reply lives in a
`cluster/rpc_over_bus.py` adapter as a third sibling coordinator, and `bus/` stays content-agnostic.
The only thing to protect is the topology-bounded loopback recursion — pre-existing, not worsened.

---

## 3. Does Phase 7 (unify the wire vocabulary) become MANDATORY? — **YES. The devil's-advocate argument collapses.**

run_1's devil's advocate argued Phase 7 was "pure risk, no benefit" **explicitly because RPC was
out of scope**: if only event+chat frames ride the WS, you *could* leave two frame vocabularies
coexisting behind a chained `on_unknown_frame` fall-through, and the merge would be cosmetic.

With RPC in scope that argument dies, for a concrete structural reason:

### 3.1 Today there are already THREE frame vocabularies on the one WS, not two

1. `rpc` / `rpc_resp` / `ping` / `pong` / `hello` / `welcome` / `bots_update` / `machines_snapshot`
   — the **registry/guest_client core switch** (registry.py:353–399, guest_client.py:282–302)
2. `event_batch` / `event_resync` — EventSyncer
3. `chat_subscribe` / `chat_unsubscribe` / `chat_event` — ChatSyncer

Crucially: the `handle_ws` switch dispatches `rpc`/`rpc_resp` **inline** (353–365), and only
UNKNOWN frames fall through to `on_unknown_frame` → event → chat chain. So RPC is the **privileged
built-in vocabulary** and event/chat are **bolt-ons behind a fall-through chain**. That asymmetry is
exactly the copy-paste smell: RPC got hard-coded into the WS loop because it was there first; the
two syncers got the fragile chained-hook treatment.

### 3.2 If RPC rides the topic-routed dispatch, the built-in switch MUST dissolve

Once RPC is a `route()`-to-one publish on `rpc.<target>` handled by a RemoteSubscriber, there is
no longer any reason for `handle_ws` to special-case `rpc`/`rpc_resp` inline. But you cannot have
`rpc` dispatched by ONE topic-routed `handle_frame` AND `event_batch`/`chat_event` dispatched by a
DIFFERENT chained fall-through on the same link — that is two dispatchers on one socket, which is
precisely the ambiguity Phase 7 exists to kill. **With RPC in, the frame dispatch is either fully
unified or it is a three-way chain**, and a three-way chain is strictly worse than the two-way one
run_1 already called fragile. There is no stable half-state. Phase 7 stops being "flip the last two
vocabularies for tidiness" and becomes "the RPC fold is not COMPLETE until the built-in `rpc` switch,
event frames, and chat frames all route through the one topic dispatcher." The `hello`/`welcome`/
`bots_update`/`machines_snapshot` handshake+topology frames stay as typed control frames, but they
too ride the unified `v:2` framing and one dispatcher.

### 3.3 The benefit is now concrete, not cosmetic

The devil's advocate's "no benefit" rested on: with RPC out, unifying event+chat frames deletes
one chained fall-through and nothing structural. With RPC in, unifying deletes:
- the inline `rpc`/`rpc_resp` special-case in BOTH `handle_ws` and `_serve` (~2× ~4-line arms plus
  the create_task dispatch),
- the entire `on_unknown_frame` chained-hook machinery (the thing chat_sync_wiring.py exists to
  chain onto — 82 lines of chained wiring vs 46 direct),
- the "chat wiring must install after event wiring" ordering constraint (a documented footgun).

That is real structure removed, not paint. **The mixed-version risk (run_1 R1) is unchanged and
already mitigated** (the `v` byte + all-nodes-restart-together + mixed-version gate test); folding
RPC in does not add wire-compat surface because RPC frames already have `id` correlation that
version-tolerates naturally (an unknown-version reply just times out the future cleanly — no
crash).

### VERDICT 3: Phase 7 becomes MANDATORY. It was "optional/pure-risk" only under RPC-out-of-scope.
With RPC in, leaving the vocabularies split means THREE dispatchers on one socket (the privileged
inline `rpc` switch + the two-way chained fall-through) — strictly worse than today. The fold is
definitionally incomplete until all three ride one topic-routed dispatch. The devil's-advocate
argument was load-bearing on the RPC-out assumption and collapses with it.

---

## 4. Dependency direction — **`bus/` stays a neutral leaf; rpc-over-bus lives in `cluster/`**

Confirmed clean, and it strengthens run_1's fan-in rather than complicating it:

```
bus/                          neutral leaf — knows topic/payload/link. NEVER "rpc"/"chat"/"event".
  core.py, message.py, subscriber.py, peer_transport.py*

cluster/                      depends on bus/  (NOT on events/)
  rpc_over_bus.py             NEW — request/reply pattern (pending map, call, correlation, timeout)
  inbound_request_executor.py NEW — the ONE loopback re-issuer (was _serve_inbound_rpc + _handle_rpc)
  event_replicator.py         broadcast policy (was EventSyncer)
  chat_replicator.py          demand policy (was ChatSyncer)
  registry.py                 SHRINKS — handshake/topology only; rpc switch + loopback + call GONE
  guest_client.py             SHRINKS — dial/reconnect only; rpc switch + loopback + call GONE
  bus_wiring.py               ONE wiring — attach PeerTransport per peer, topic-routed frames

events/                       depends on bus/  (NOT on cluster/)
  store_subscriber.py, telegram_notifier.py, web_stream.py, ...
```
*(run_1 placed PeerTransport in cluster/; either cluster/ or a bus/-adjacent module works — the
point is it is neutral link mechanism, not policy. I keep run_1's placement.)*

- `bus/` imports nothing project-internal. It does not gain an `rpc` concept. **Content-agnostic
  invariant preserved.**
- `cluster/rpc_over_bus.py` depends on `bus/` (publishes/subscribes) — same direction as the two
  replicators. RPC-over-bus is a peer of them, not a special citizen.
- `events/` ↔ `cluster/` mutual import stays **0** — RPC lives entirely in `cluster/`, touches
  `bus/` only. No new cross-edge.
- The ~15 `dispatch_machine_request` call sites in `transports/web/server.py` keep calling ONE
  facade method (now backed by `rpc_over_bus.call` instead of the role-branch). **Caller contract
  unchanged** — the web server never learns the bus exists.

### VERDICT 4: `bus/` stays a content-agnostic neutral leaf — it never gains an "rpc" concept.
rpc-over-bus is a third `cluster/` sibling coordinator depending only on `bus/`. No new
`events/`↔`cluster/` edge. The web-server caller facade is preserved byte-for-byte.

---

## 5. NET-LOC LEDGER (independent, verified against the code)

I count only the **in-scope RPC/transport/wiring** surface — the delta the fold adds ON TOP of
run_1's already-scoped event+chat unification. I am NOT re-counting event/chat unification (run_1
owns that). "BEFORE" = RPC-specific lines that survive or die; "AFTER" = their unified replacement.

### 5.1 BEFORE — the RPC/transport surface today (lines that the fold touches)

| Component | File:lines | ~LOC | Fate |
|---|---|---|---|
| `dispatch_machine_request` + `_proxy_via_host` + `_proxy_to_remote` | rpc.py 30–97 | **68** | COLLAPSE → one `call` path (host/guest branch gone) |
| `handle_guest_ws` | rpc.py 99–108 | 10 | keep (thin registry delegation) |
| `GuestSession.call` (host→guest RPC) | registry.py 81–105 | **25** | COLLAPSE → shared `call` |
| `GuestClient.call` (guest→host RPC) | guest_client.py 87–115 | **29** | COLLAPSE → shared `call` (mirror dup) |
| `_PendingResponse` + `_resolve` | registry.py 59–69, 107–110 | 14 | move to rpc_over_bus (kept, ~unchanged) |
| `_serve_inbound_rpc` (host loopback) | registry.py 211–269 | **58** | COLLAPSE → shared InboundRequestExecutor |
| `_handle_rpc` (guest loopback) | guest_client.py 315–355 | **41** | COLLAPSE → shared InboundRequestExecutor (mirror dup) |
| `handle_ws` rpc/rpc_resp inline switch arms + create_task | registry.py 353–365 | ~15 | COLLAPSE → topic dispatch |
| `_serve` rpc/rpc_resp inline arms + create_task | guest_client.py 282–294 | ~13 | COLLAPSE → topic dispatch |
| pending-reject-on-disconnect | guest_client.py 255–260 | 6 | move to peer detach (kept, ~unchanged) |
| `event_replicator`/`chat_replicator` `_send_to` dup (run_1 already counts) | sync.py 227–234 + chat_sync.py 178–185 | (run_1) | — |
| `chat_sync_wiring.py` chained hooks | 82 | (run_1) | (run_1 deletes) |
| `sync_wiring.py` direct hooks | 46 | (run_1) | (run_1 deletes) |
| `on_unknown_frame` chain machinery in registry/guest_client | scattered | ~20 | COLLAPSE with Phase 7 topic dispatch |

**BEFORE in-scope collapsible total (RPC-specific, excluding run_1's event/chat lines):**
68 + 25 + 29 + 58 + 41 + 15 + 13 + 20 = **~269 lines** of RPC/transport/dispatch that today is
role-split-duplicated or hard-coded.

Of which the **mirror-duplicated pairs** (the core claim) are:
- `call` × 2: 25 + 29 = **54**
- loopback re-issue × 2: 58 + 41 = **99**
- response-builder proxy (`_proxy_via_host` ≈ `_proxy_to_remote`): ~18 + ~20 = **~38**
- inline rpc switch × 2: 15 + 13 = **28**

Duplicated-pair subtotal ≈ **219 lines**, of which roughly HALF is pure mirror redundancy that a
role-agnostic design deletes outright.

### 5.2 AFTER — the unified replacement

| New component | ~LOC | Replaces |
|---|---|---|
| `cluster/rpc_over_bus.py` — pending map + ONE `call` + correlation + timeout + reject-on-detach | **~55** | both `call`s + both `_proxy_*` + `_PendingResponse` + dispatch_machine_request routing |
| `cluster/inbound_request_executor.py` — ONE loopback re-issuer | **~45** | `_serve_inbound_rpc` + `_handle_rpc` (99 → 45) |
| `dispatch_machine_request` facade (thin: publish to `rpc.<M>` via route-to-one, await future) | **~12** | rpc.py 30–97 (68 → 12) |
| topic-dispatch arms for `rpc`/`rpc_resp` in the ONE `handle_frame` (Phase 7, shared) | **~8** | 4 inline arms across 2 files (28 → 8) |
| `handle_guest_ws` | 10 | unchanged |
| pending-reject on peer detach (in PeerTransport/RemoteSubscriber) | **~6** | guest_client 255–260 (moved, not added) |

**AFTER in-scope total:** 55 + 45 + 12 + 8 + 10 + 6 = **~136 lines**.

### 5.3 The delta

```
BEFORE (in-scope RPC/transport/dispatch, RPC-specific):   ~269
AFTER (unified):                                          ~136
─────────────────────────────────────────────────────────────
NET RPC-fold delta:                                       ~ -133   (before any run_1 event/chat delta)
```

Now stack it on run_1's own ledger. run_1's chat-only work was **+306** *because RPC was out of
scope* — the +306 built PeerTransport + bus core + subscribers + the store-subscriber refactor
WITHOUT getting to delete the biggest duplication (RPC's role-split mirror). Folding RPC in adds
the ~-133 above AND lets the already-built PeerTransport/route-to-one primitives absorb RPC at
near-zero marginal infrastructure (RPC reuses ChatReplicator's route-to-one, adds no new
transport). The infra was already paid for by chat; RPC is the tenant that makes the rent worth it.

**Independent net range for the WHOLE unified effort with RPC IN:**

- Optimistic: run_1's +306 was inflated by counting bus infra that RPC now amortizes; with RPC's
  ~-133 collapse and shared route-to-one/loopback extraction, the whole effort lands **slightly
  negative to break-even: roughly −40 to +60 net LOC.**
- Realistic: **−20 to +120 net.** The bus core + subscriber abstraction + harness are genuine new
  code (~150–200 lines that did not exist), but they retire ~400+ lines of duplicated
  syncer-skeleton + RPC role-split + chained wiring. The RPC fold is what tips the balance from
  run_1's clearly-positive +306 toward the owner's demanded net-reduction.

**Verdict on the owner's claim (net must be NEGATIVE):** With RPC in scope, net-negative is
**achievable but tight**, and it is achievable *specifically because* the three biggest collapse
line-items are RPC's:

1. **Loopback re-issue de-dup: 99 → ~45 (−54).** Biggest single win. Pure mirror redundancy the
   role-split forced.
2. **The two `call`s + two `_proxy_*` → one `call`: ~92 → ~55 (−37).** The canonical mirror the
   claim names.
3. **Inline rpc switch + on_unknown_frame chain → one topic dispatch: ~48 → ~8 (−40).** Only
   unlockable *because* Phase 7 is now mandatory (§3).

If Phase 7 were skipped, item 3 stays and the net drifts positive — another reason Phase 7 is not
optional under the owner's net-reduction mandate.

---

## 6. Top architectural risk of folding RPC in

**The loopback re-issue is a hidden control-flow cycle that the fold must preserve, not "simplify."**

`_serve_inbound_rpc` / `_handle_rpc` deliberately re-enter the local web server over HTTP so the
host's `_handle_web_*` handlers (which themselves call `dispatch_machine_request`) provide two-hop
relay *for free*. This is load-bearing and non-obvious:

- An inbound `rpc.<host>` request targeting guest-B is executed on the host's web port, whose
  handler calls `dispatch_machine_request("guest-B", ...)` → publishes `rpc.<guest-B>` → routes to
  guest-B. The recursion is what makes hub-and-spoke two-hop work.
- The tempting "simplification" during the fold is: since it's all one bus now, dispatch the
  inbound RPC *directly* to the target topic in-process and skip the HTTP loopback. **That breaks
  the relay**: the direct path skips the web handler's auth, its `machine`-resolution, and its
  onward `dispatch_machine_request`, so a host-mediated guest→guest RPC silently 404s or
  mis-routes. It also skips the local handler's own logic (some endpoints do local enrichment
  before/after remote fetch).

**Mitigation (must be a named invariant in the migration plan):** keep `InboundRequestExecutor` as
a genuine loopback HTTP re-issue against `127.0.0.1:<local_web_port>` — do NOT collapse it into an
in-process bus publish. Freeze a **two-hop relay invariant**: a `ThreeNodeCluster` test where
guest-A issues an RPC targeting guest-B, asserting it transits host and returns B's response, with
`CountingWebRequests` confirming the host's web port was re-entered. This is the RPC analog of
run_1's INV-A2 (chat-never-hits-SQLite) — a by-construction fact that a reviewer's "tidy-up" can
silently violate.

Secondary risk (lower): the reply-topic correlation on a shared bus must reject in-flight futures
on peer detach in ONE place (the RemoteSubscriber/peer teardown), or a dropped link leaves RPC
callers hanging until the 30s timeout instead of failing fast (guest_client.py:255–260 does this
today; it must move, not vanish). Guard with a "detach rejects pending RPC futures" invariant.

---

## 7. One-paragraph through-line

Folding RPC in is architecturally SOUND and, unlike the chat-only run_1 scope, it is what makes the
unification NET-REDUCE code. Request/reply rides the bus as a third sibling coordinator
(`cluster/rpc_over_bus.py`) using the **same route-to-one primitive chat already owns** — it needs
correlation, point-to-point delivery, and a shared loopback re-issuer, all of which live in
`cluster/` and none of which contaminate the content-agnostic `bus/` leaf. The role-split
(host-in-registry, guest-in-guest_client) is the sole reason `call`, the loopback re-issue, and the
proxy response-builder each exist TWICE; a role-agnostic bus deletes those mirrors, collapsing
~269 in-scope lines to ~136 (net ~−133 on the RPC surface), and tipping the whole effort from
run_1's +306 toward net-negative. Phase 7 flips from optional-pure-risk to MANDATORY: with RPC in,
the frame vocabulary is either fully unified or it is THREE dispatchers on one socket (the
privileged inline `rpc` switch + the two-way chained fall-through), which is strictly worse than
today — the devil's-advocate "no benefit" argument was load-bearing on RPC-out-of-scope and
collapses. The one thing to protect is the loopback re-issue's topology-bounded recursion: keep it
a real HTTP loopback so two-hop guest→host→guest relay stays free, and freeze it as an invariant so
nobody "optimizes" it into a direct in-process publish.
