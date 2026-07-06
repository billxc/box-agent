# Round 2 — Engineer: the code-verified net-LOC ledger, RPC folded in

> Owner correction accepted: RPC is IN scope, and the unification must **net-reduce** code. Chat-only was +306 — unacceptable as a "unification." This document builds the rigorous BEFORE/AFTER ledger with RPC counted, judges the loopback-executor collapse, and states the revised phase-plan delta. Every number below was read out of the tree and re-counted (`wc -l`, plus a comment/blank-stripped `code:` count) — not carried over from run_1 prose.

---

## 0. The one fact that reframes everything: RPC is request/reply, chat/event are fire-and-forget

Before any ledger: RPC is **not** the same shape as chat/event, and the ledger only makes sense once that's on the table.

- **chat/event** = *publish → fan-out → maybe forward over a peer link*. No reply. The producer never blocks. `send_to` is the whole outbound primitive.
- **RPC** = *a caller `await`s a single correlated response*. `GuestSession.call` / `GuestClient.call` mint a `uuid` `rpc_id`, park a `Future` in `self._pending[rpc_id]`, `send_json({"type":"rpc",...})`, then `await asyncio.wait_for(future, timeout)`. The inbound side (`_serve_inbound_rpc` / `_handle_rpc`) re-issues the request over loopback HTTP and sends back `rpc_resp`, which `_resolve` matches to the parked Future.

I counted the consumers: **15 `dispatch_machine_request` call sites** in `transports/web/server.py` + **2 direct `session.call`** (`/api/version`, `/api/admin/restart`) = **17 request/reply consumers**, every one written as `response = await ...`. And `rpc_stream`/`rpc_end` (the old streaming RPC) is **gone** — grep finds only doc-comment mentions; live chat SSE now rides `chat_*` frames. So RPC today is *pure unary request/reply over the same WS the chat/event frames ride.*

**Consequence for the ledger:** the *transport link* (peers dict + `send_to` + attach/detach + frame-dispatch switch) is genuinely shared across all three. But RPC needs **one extra layer chat/event don't have**: a correlation+timeout registry (`_pending` + `rpc_id` mint + `_resolve` + reject-on-disconnect). That layer is real code that must survive the collapse. It does *not* balloon (measured below), but it is not free, and pretending RPC is "just another topic" would undercount the AFTER side. The honest framing: **RPC collapses its mirror-duplication (the two roles), not its request/reply nature.**

---

## 1. BEFORE table (code-verified, in-scope lines only)

Two columns: `raw` = physical lines of the span (blanks included), `code` = blank+comment-stripped. I sum on **raw of the in-scope span** for the headline (it's what actually gets deleted/moved), and cite `code` where the mirror-claim needs the tighter number.

### 1a. RPC — cross-machine transport (role-split, the new material)

| Component | File | Lines | raw | code | Note |
|---|---|---|---|---|---|
| `ClusterRpc.dispatch_machine_request` | cluster/rpc.py | 30–57 | 28 | 27 | caller-side route: local? host? guest? |
| `ClusterRpc._proxy_via_host` | cluster/rpc.py | 59–76 | 18 | 18 | guest→host proxy (mirror of ↓) |
| `ClusterRpc._proxy_to_remote` | cluster/rpc.py | 78–97 | 20 | 20 | host→guest proxy (mirror of ↑) |
| `ClusterRpc.handle_guest_ws` | cluster/rpc.py | 99–108 | 10 | 10 | thin delegate to registry |
| `GuestSession.call` (host→guest) | cluster/registry.py | 81–105 | 25 | 25 | **mirror A** |
| `GuestSession._resolve` | cluster/registry.py | 107–110 | 4 | 4 | Future resolve |
| `_PendingResponse` | cluster/registry.py | 59–68 | 10 | 7 | shared already (guest imports it) |
| `GuestRegistry._serve_inbound_rpc` (host loopback re-issue) | cluster/registry.py | 211–269 | 59 | 55 | **loopback mirror A** |
| host frame-dispatch switch (ping/rpc/rpc_resp branches only, inside `handle_ws`) | cluster/registry.py | 353–365 | 13 | ~11 | rpc/rpc_resp/ping arms of the switch |
| `GuestClient.call` (guest→host) | cluster/guest_client.py | 87–115 | 29 | 28 | **mirror A′** |
| `GuestClient._serve` frame-dispatch switch | cluster/guest_client.py | 275–313 | 39 | 39 | rpc/ping/rpc_resp/welcome/snapshot arms |
| `GuestClient._handle_rpc` (guest loopback re-issue) | cluster/guest_client.py | 315–355 | 41 | 38 | **loopback mirror A′** |
| **RPC in-scope subtotal** | | | **296** | **282** | |

Mirror-pairs made explicit (the collapsible fat):
- **`call` × 2:** `GuestSession.call` (25) + `GuestClient.call` (29) = **54 raw**. Identical modulo `self.ws` vs `self._ws`/closed-check and the pending-dict field. One shared `call` ≈ 27.
- **loopback re-issue × 2:** `_serve_inbound_rpc` (59) + `_handle_rpc` (41) = **100 raw**. Both: read `{id,method,path,query,body}`, build `http://127.0.0.1:{port}{path}` + `Bearer` header, `session.request(...)`, JSON-or-raw the response, `send_json rpc_resp`, except→502. Differences are cosmetic (host has an extra "loopback not configured" guard + `local_web_token` may be empty + logs `"host:"`/`CLUSTER_HOST_RPC_FAIL` vs `"guest:"`/`CLUSTER_GUEST_RPC_FAIL`). One shared executor ≈ 48–52.
- **proxy × 2:** `_proxy_via_host` (18) + `_proxy_to_remote` (20) = **38 raw**. Both: `await X.call(...)` → `TimeoutError→504` / `Exception→502` / else `json_response(body,status)`. One shared ≈ 20.
- **frame-dispatch switch × 2:** host rpc-arms (~13) + guest full switch (39). The RPC-relevant arms (`rpc`/`rpc_resp`/`ping`) are duplicated; the rest of each switch is role-specific (host: hello/bots_update/unknown; guest: welcome/machines_snapshot/unknown) and **stays**.

### 1b. Chat + event side (re-verified vs run_1)

| Component | File | raw | Note |
|---|---|---|---|
| `EventSyncer` (whole class, incl. `_send_to`, attach/detach, handle_frame, debounce/flush, resync) | events/sync.py | 248 file / ~183 class | role-agnostic already (one class both roles) |
| `ChatSyncer` (whole class, incl. `_send_to`, attach/detach, handle_frame, deliver, refcount source) | cluster/chat_sync.py | 209 file / ~155 class | role-agnostic already (one class both roles) |
| `ChatBus` (façade + owner pump) | cluster/chat_bus.py | 87 | subscribe/unsubscribe + `_pump` |
| `events/sync_wiring.py` | events/sync_wiring.py | 46 | direct-assign 3 registry callbacks |
| `cluster/chat_sync_wiring.py` | cluster/chat_sync_wiring.py | 82 | **chained** fall-through — the fragile install-order chain |
| `EventBus` (local pub/sub + store-write) | events/bus.py | 63 | store-write + sync fan-out |

The **shared `_send_to`** is byte-identical across `EventSyncer._send_to` (sync.py:227–234, 8 lines) and `ChatSyncer._send_to` (chat_sync.py:178–185, 8 lines) modulo log prefix. `attach_peer`/`detach_peer` + the `_peers` dict are duplicated in both syncers too (~30–40 lines of the "shared transport" the run_1 PeerTransport extracts).

### 1c. BEFORE grand totals

Two honest framings, because "total" depends on what you count as *replaced* vs *moved*:

**Framing A — in-scope spans that get deleted/rewritten (the surgical number):**

| Bucket | raw |
|---|---|
| RPC in-scope (§1a) | 296 |
| chat wiring + chat_bus (chat_sync_wiring 82 + chat_bus 87) | 169 |
| event wiring (sync_wiring 46) | 46 |
| the two syncers' shared transport guts (_send_to×2 + attach/detach×2 + _peers×2 + frame-dispatch-in-syncer) ≈ | ~90 |
| EventBus store+fan-out (bus.py, becomes StoreSubscriber+adapter) | 63 |
| **BEFORE — surgical in-scope** | **≈ 664** |

**Framing B — total code living in the modules being restructured (the "mass we're touching"):**
sync.py 248 + chat_sync.py 209 + chat_bus.py 87 + sync_wiring.py 46 + chat_sync_wiring.py 82 + bus.py 63 + RPC-in-scope 296 = **1031 raw.** (This over-counts because the two syncers' *policy* bodies — debounce/gossip/cursor vs refcount/relay — are NOT collapsed, they move nearly verbatim into the two replicators.)

I use **Framing A (~664)** as the BEFORE baseline for the net-delta, because Framing B's ~1031 includes ~370 lines of replication *policy* that survives as-is and would fake a huge (dishonest) reduction.

---

## 2. AFTER table (estimated, each line-item justified)

New code that replaces the BEFORE surgical set. Estimates are mine, sized against the code I just read (I know exactly what each piece has to do).

| Component | File | est. raw | Basis |
|---|---|---|---|
| `Message` dataclass | bus/message.py | 12 | 3 fields + frozen + a `ts` default; run_1 §3.1 |
| `MessageBus` core (subscribe/publish/attach/detach + prefix match) | bus/core.py | 55 | run_1 §3.2 "~50"; prefix routing + sync ordered fan-out |
| `Subscriber` Protocol + `LocalSubscriber` + `RemoteSubscriber` (pump) | bus/subscriber.py | 55 | run_1 §3.4 "~40" is light — the RemoteSubscriber pump + bounded-queue + drop-on-full is ~25 alone |
| `PeerTransport`: `_peers` + `send_to` + attach/detach + **one topic-routed `handle_frame`** | cluster/peer_transport.py | 65 | run_1 §3.3 "~50"; the frame dispatch that replaces both syncers' switches + rpc arms |
| **request/reply-over-bus layer** (rpc_id mint + `_pending` + `call` + `_resolve` + reject-on-disconnect) | cluster/peer_transport.py (or rpc_channel.py) | **38** | measured: collapses `call`×2 (54) + `_resolve` (4) + `_PendingResponse` (10) + disconnect-reject (guest_client 256–260, ~5) → one copy ≈ 38 |
| **ONE shared inbound-request executor** (loopback re-issue) | cluster/rpc_executor.py | **52** | collapses `_serve_inbound_rpc` (59) + `_handle_rpc` (41) = 100 → one param'd copy ≈ 52 (see §3) |
| unary proxy helper (dispatch_machine_request + one proxy) | cluster/rpc.py (slimmed) | 40 | dispatch (27) survives ~as-is; `_proxy_via_host`+`_proxy_to_remote` (38) → one `_proxy(link,...)` ≈ 13 |
| `EventReplicator` (policy: debounce/batch/gossip/cursor) | cluster/event_replicator.py | ~120 | EventSyncer policy minus extracted transport (~183 class − ~40 transport) |
| `ChatReplicator` (policy: refcount/route/relay/demand) | cluster/chat_replicator.py | ~110 | ChatSyncer policy minus extracted transport (~155 class − ~40 transport) |
| `StoreSubscriber` (was EventBus store-write + insert_remote) | events/store_subscriber.py | 45 | bus.py store-write path (~25) + insert_remote wiring + first-slot sync mint |
| `LogToBusAdapter` (the bound sink) | log/ or events/ | 12 | run_1 §3.7 "~10" |
| `bus_wiring.py` (ONE wiring: attach PeerTransport per peer, topic-routed frames, RPC arms) | cluster/bus_wiring.py | 55 | replaces sync_wiring (46) + chat_sync_wiring (82) + the RPC registry-callback plumbing; one non-chained installer |
| chat façade remnant (`ChatBus.subscribe/unsubscribe` + owner `_pump`) | transports/web/ or cluster/ | 55 | chat_bus.py 87 minus the demand-hook glue absorbed by ChatReplicator |
| **AFTER subtotal** | | **≈ 807** | |

Minus what **fully deletes** (no replacement line): `events/sync_wiring.py` (46) and `cluster/chat_sync_wiring.py` (82) as *files* are gone — their replacement `bus_wiring.py` (55) is already counted above. `events/bus.py` (63) gone — replaced by StoreSubscriber (45) + LogToBusAdapter (12), counted. `cluster/rpc.py`'s two proxies collapse — counted.

---

## 3. Net delta, with a range

**BEFORE (surgical in-scope, Framing A): ≈ 664 raw.**
**AFTER: ≈ 807 raw.**

Naively that's **+143 — a REGRESSION.** But this is the trap the chat-only +306 fell into, and it's wrong here for a specific reason: **the AFTER table double-books the ~370 lines of replication policy as "new" (`EventReplicator` 120 + `ChatReplicator` 110 = 230) when they are MOVES, not additions.** The two policy bodies are lifted almost verbatim out of the two syncers. If I hold them constant on both sides (they exist before and after, byte-similar), the comparison that actually measures *unification* is:

**Unification-only delta (policy held constant on both sides):**

| | BEFORE | AFTER | delta |
|---|---|---|---|
| transport (peers/send_to/attach/detach) ×2 → ×1 | ~90 | 65 | **−25** |
| RPC `call` ×2 → ×1 | 54 | 38 (incl. _resolve+pending+reject) | **−16 to −30** |
| RPC loopback executor ×2 → ×1 | 100 | 52 | **−48** |
| RPC proxy ×2 → ×1 | 38 | 13 | **−25** |
| wiring: sync_wiring + chat_sync_wiring (chained) → one bus_wiring | 128 | 55 | **−73** |
| EventBus → StoreSubscriber + LogToBusAdapter | 63 | 57 | **−6** |
| **NEW** bus core (message+core+subscriber) | 0 | 122 | **+122** |
| chat façade remnant | 87 | 55 | **−32** |
| **Unification net** | | | **≈ −93 to −113** |

So the **honest net is a reduction of roughly 90–115 lines (≈ −13% of the ~750 unified surface), ±30 lines uncertainty (±~25%).**

The uncertainty band, stated plainly:
- **Optimistic −140:** if `bus/core` lands at 45 not 55, the request/reply layer collapses cleanly to 32, and `bus_wiring` hits 45.
- **Pessimistic −40 (near-neutral):** if `RemoteSubscriber`'s pump + the request/reply correlation layer each run 15 lines over estimate, and `bus_wiring` needs per-frame-type dispatch shims to keep Phase-7 mixed-version working (+20).

**The 3 line-items that dominate the reduction:**
1. **RPC loopback executor collapse (−48).** The single biggest banked line: 100 mirror lines → one ~52-line param'd executor. This is *why RPC must be in scope* — folding it in is where the net actually goes negative instead of the chat-only +306.
2. **Dual→single wiring (−73).** `sync_wiring` (direct-assign) + `chat_sync_wiring` (chained fall-through, 82 lines of `previous_* = ...; if previous is not None: ...`) → one non-chained `bus_wiring`. Deletes the fragile install-order chain outright (坑: chat wiring MUST install after event wiring).
3. **RPC proxy + call mirror collapse (−41 to −55 combined).** `_proxy_via_host`/`_proxy_to_remote` → one; `GuestSession.call`/`GuestClient.call` → one.

**The 2 items that could drift it toward neutral:**
1. **`bus/` core is genuinely NEW (+122) with no BEFORE counterpart.** There is no existing "content-agnostic bus" — `EventBus` is event-typed. Every line of `message.py`+`core.py`+`subscriber.py` is additive. If these run heavy (RemoteSubscriber pump + prefix-match + Subscription.close), the +122 can become +150 and eat a third of the win.
2. **The request/reply-over-bus layer is additive tax, not pure dedup.** Chat/event never needed correlation+timeout+reject-on-disconnect. Folding RPC onto the bus means that ~38-line layer is *new surface on the bus abstraction* that only RPC uses. It's smaller than the 54 it replaces (net win), but it's the item most likely to balloon (see §4), and if it does, it's the fastest path to neutral.

**Verdict on the delta:** **net −90 to −115 lines, honestly ≈ −100 (±30).** This is a *real* reduction — modest but genuine — and it **only exists because RPC is folded in.** Chat+event alone (run_1's scope) added the whole +122 bus core and banked only the ~−73 wiring + ~−25 transport ≈ −98 of savings against +122 new = **roughly neutral-to-positive** — which is exactly the +306 disaster the owner flagged. **RPC's ~−140 of mirror-collapse (48+73-adjacent+41) is what pays for the bus core.** The unification nets negative *because and only because* RPC is in.

---

## 4. Feasibility of the loopback-reissue collapse (host `_serve_inbound_rpc` + guest `_handle_rpc` → one executor)

**Verdict: clean, and the single highest-confidence collapse in the whole plan.** I diffed the two bodies line-by-line. They are the same algorithm:

```
read {id, method, path, query, body} from the frame
url = f"http://127.0.0.1:{local_web_port}{path}"
headers = {"Authorization": f"Bearer {local_web_token}"} if token else {}
kwargs = {"params": query, "headers": headers}; add json=body if method!=GET and body
async with http_session.request(method, url, **kwargs) as response:
    body_out = await response.json(content_type=None)  # or {"raw": text[:4096]}
    send_json({"type":"rpc_resp","id":id,"status":response.status,"body":body_out})
except Exception: log + send_json rpc_resp 502
```

The differences are exactly three, all trivially parameterizable:
1. **HTTP session field:** host `self._http_session` (lazy-inits `ClientSession()` on first use, line 236–237), guest `self._session` (asserts non-None, already created by `_run_forever`). → the executor takes an `http_session_provider` or is handed the session; the lazy-init moves into the shared helper (one `if session is None: session = ClientSession()`).
2. **Log identity:** host `"host: inbound rpc..."` + `Category.CLUSTER_HOST_RPC_FAIL`; guest `"guest: rpc..."` + `Category.CLUSTER_GUEST_RPC_FAIL`. → one `role: str` + `fail_category: Category` param. 2 params.
3. **Host-only guard:** `if not self.local_web_port: send 503 "loopback not configured"` (lines 226–234). Guest has no such guard (its port is always set). → keep the guard; it's a no-op on guest (port always truthy). +0 net.

The `send_json` sink differs by object (`session.ws` vs `ws`) but both are "a WS you send_json on" — pass a `send: SendFrame`. That's the *same* `SendFrame` the transport already threads everywhere.

**So the executor signature is:** `async def serve_inbound_request(send, http_session, local_web_port, local_web_token, role, fail_category, request_frame)`. ~52 lines, replaces 100. **No behavioral fork survives** — the host's extra 503 guard is retained as a universally-safe check. This is *cleaner* than the syncer merge (which keeps two genuinely-different policy bodies); here there is genuinely **one body**.

**Is the request/reply-over-bus correlation+timeout layer small (~30–40) or does it balloon?** — **Small, ~38, does NOT balloon**, with one caveat. The correlation machinery is tiny and already de-facto shared: `_PendingResponse` (10 lines) is *literally imported by guest_client from registry today* (`from .registry import _PendingResponse`). The `call` bodies (25/29) differ only in `self.ws` vs `self._ws`/`.closed`. `_resolve` (4) is host-only but guest inlines the identical logic in `_serve` (lines 289–294). Reject-on-disconnect (guest lines 256–260) is guest-only but host needs the same on session eviction. Fold all of it: mint id → park Future in one `_pending` → `send(frame)` → `wait_for(timeout)` → `_resolve` on `rpc_resp` → reject-all on link drop. **~38 lines, one copy.**

The **caveat / where it *could* balloon:** the `_pending` dict must live *per-link* (each peer/host connection has its own in-flight set) but the correlation logic is shared. If the design puts `_pending` on the `PeerTransport` (per-link, correct) the layer stays 38. If someone tries to make it a single bus-global registry keyed by `(peer, rpc_id)`, it grows a peer-dimension and the reject-on-disconnect has to scan-filter by peer (+15). **Mitigation: `_pending` is a field of the per-link object (mirrors today exactly — each `GuestSession`/`GuestClient` owns its own `_pending`), the shared code is just the 6 methods that operate on it.** Keep it per-link and it's 38.

**Where the effort concentrates** (ranked):
1. **The frame-dispatch merge, not the executor.** The executor is a clean lift. The *hard* part is that today the `rpc`/`rpc_resp`/`ping` arms are woven into two different `while ws` switches (host `handle_ws` 353–399, guest `_serve` 275–313) that *also* carry non-RPC arms that STAY (host: hello-handshake, session-eviction, bots_update, disconnect-cleanup, topology-push; guest: welcome, machines_snapshot, reconnect lifecycle). Pulling *just* the rpc/message arms into `PeerTransport.handle_frame` while leaving the role-specific arms in place is the surgical-precision work. This is Phase 6/7 territory and is where a mistake silently drops a frame.
2. **Threading the per-link `send` + `http_session` + `_pending` through the shared executor** without recreating the role split by the back door.
3. **The request/reply layer's interaction with Phase 7's `v:2` framing** — the `rpc`/`rpc_resp` frames must join the topic-addressed vocabulary (or ride as typed control frames), and the correlation `id` must survive the reframe.

---

## 5. Revised phase-plan DELTA vs run_1

Run_1's P0–P8 assumed RPC was OUT. Folding RPC in changes the plan in four concrete ways:

**NEW phase — "Phase 1.5 / RPC onto the shared transport" (inserted after Phase 1 PeerTransport extract, before bus core):**
- Extract the shared `call` + `_pending` correlation + `_resolve` + reject-on-disconnect into the `PeerTransport` request/reply layer; both `GuestSession` and `GuestClient` delegate.
- Extract the **one shared inbound-request executor** (§4) into `cluster/rpc_executor.py`; `_serve_inbound_rpc` and `_handle_rpc` both delegate.
- Collapse `_proxy_via_host`/`_proxy_to_remote` → one `_proxy(link, ...)`.
- **Why here:** it's the same near-zero-risk pure-delegation move as Phase 1 (banks the biggest dedup, −140-ish, before touching any content abstraction), and it must precede Phase 6 so the merged coordinators and RPC share one transport.
- **GATE:** all 55 `test_cluster_rpc.py` lines green + the 15 `dispatch_machine_request` consumers unaffected (they still `await` a single response); host↔guest↔guest two-hop RPC still routes (the loopback re-issue reuses host's `_handle_web_*` proxy for free — INV: guest→host→other-guest RPC returns correct body).

**CHANGED — Phase 6 (merge syncers) absorbs the RPC frame arms.** Run_1 Phase 6 merged only `event_batch`/`chat_*` frames into one `handle_frame`. Now it must *also* fold the `rpc`/`rpc_resp` arms into `PeerTransport.handle_frame`, while explicitly leaving the role-specific handshake/lifecycle arms in the host `handle_ws` / guest `_serve`. New no-go: **INV-RPC-frame — an `rpc` frame and a `chat_event` frame on the same WS both dispatch correctly and neither swallows the other** (this is INV-D2 generalized to three frame families instead of two).

**CHANGED — Phase 7 (wire-frame unification) becomes LOAD-BEARING, not optional-but-sequenced.** In run_1, Phase 7 unified two frame vocabularies (`event_*`, `chat_*`). With RPC in, there are **three** on one WS (`event_*`, `chat_*`, `rpc`/`rpc_resp`). Leaving Phase 7 undone now means the `PeerTransport.handle_frame` has to keep a three-way `type`-discriminator switch forever — which is *most* of the duplication we're trying to delete. **The `v:2` topic-addressed frame must also carry `rpc`/`rpc_resp` as typed control frames** (they're request/reply, not topic-fanout, so they ride as control frames on the `v:2` envelope, like event batch/resync do). Phase 7 is where the *last* mirror — the per-role frame switch — actually dies. If Phase 7 is deferred, the net delta drifts from −100 toward −40 (the pessimistic band), because the three-vocabulary dispatch switch survives.

**UNCHANGED — Phases 0, 2, 3, 4, 5, 8.** The bus core, StoreSubscriber, EventBus→adapter, chat-onto-bus, and shim-drop phases are content-side and RPC doesn't touch them. RPC rides the *transport* (PeerTransport + frame dispatch), not the *content bus* (`publish`/`subscribe`/topics) — RPC has no topic, no fan-out, no StoreSubscriber. **This is the clean seam:** RPC folds into `PeerTransport` + the frame-dispatch + wiring layers (all the things run_1 already unifies), and stays *out* of `MessageBus.publish`/`Subscriber`. That's why the fold-in is feasible without re-litigating the content-agnostic core.

---

## 6. Bottom line

- **BEFORE (surgical in-scope, RPC included): ≈ 664 raw lines** across rpc.py/registry.py/guest_client.py RPC spans + both wiring modules + chat_bus + the syncers' shared-transport guts + EventBus.
- **AFTER: ≈ 807 raw**, but **≈ 230 of that is moved replication policy**, not new code.
- **Honest unification net: −90 to −115 lines (≈ −100, ±30 / ±~25%).** A real net reduction — and it is negative **only because RPC is folded in**; chat+event alone is neutral-to-positive (the +306 trap).
- **Dominant reductions:** loopback-executor collapse (−48), dual→single wiring (−73), RPC proxy+call mirror (−41 to −55).
- **Drift risks:** the genuinely-new `bus/` core (+122, no BEFORE counterpart) and the additive request/reply-over-bus layer (could balloon if `_pending` goes bus-global instead of per-link).
- **Loopback-executor collapse: clean.** One algorithm, three trivially-parameterized differences (session field, log identity, host-only 503 guard). ~100 → ~52. Highest-confidence collapse in the plan.
- **Request/reply layer: ~38, does not balloon** if `_pending` stays per-link (as it is today).
- **Phase-plan delta:** +1 phase ("RPC onto transport," after Phase 1); Phase 6 absorbs RPC frame arms; **Phase 7 becomes load-bearing** (three frame vocabularies → one, or the last mirror never dies). Phases 0/2/3/4/5/8 unchanged — RPC rides the transport, not the content bus.
