# Bus migration map — old bus tests → frozen invariants

> Phase 0 artifact. This table is the guard against the **886-baseline trap**:
> deleting a coupled old test while adding a new one can silently hide a
> regression. Rule: an old bus test may be deleted ONLY when every behaviour it
> covers is pinned by a GREEN invariant in `tests/unit/test_message_bus_invariants.py`,
> and the phase named in "delete-old-when" has landed. Until then, both live.
>
> The invariants file is FROZEN and append-only. Two harnesses back it:
> `tests/unit/_bus_harness.py` (event + chat fan-out shuttle; self-tests
> `tests/unit/test_bus_harness.py`) and `tests/unit/_rpc_bus_harness.py` (RPC
> round-trip over real aiohttp + real WS; self-tests
> `tests/unit/test_rpc_bus_harness.py`).

## How to read "delete-old-when"

Phases are from `.harness/nodes/discuss/run_1/decision.md §4`:

| Phase | What lands |
|-------|-----------|
| P1 | Extract `PeerTransport` (both syncers delegate) |
| P3 | `StoreSubscriber` behind `EventBus` (store path) |
| P4 | `EventBus` → thin adapter over `MessageBus` (LANDED: `EventBus` owns a `MessageBus`; `StoreSubscriber` is the first bus subscriber and stashes the enriched `Event` into `payload["event"]`; `subscribe(callback)` is a compat shim over the bus; `publish` fans out synchronously via `bus.publish("events.<category>", ...)`) |
| P5 | Chat rides the bus (`chat.*` ephemeral topics) |
| P6 | Merge syncers → `EventReplicator` + `ChatReplicator`, one `bus_wiring.py` |
| P8 | Drop `EventBus` shim; rewrite white-box tests |

A test whose assertions are pure external behaviour (store rows / queue contents
/ emitted frames) migrates its **behaviour** into an INV; a test that peeks
private state (`bus._pumps`, `sync_a._on_local_event`) is rewritten black-box on
migration. "Keep — frozen" means the test asserts an exit contract we are NOT
touching (store schema, facade signature, /api/events) and it stays as-is.

---

## test_event_bus.py (10 tests) — LOCAL pub/sub + sync store write

| Old test | Behaviour covered | Covering INV | Delete-old-when |
|----------|-------------------|--------------|-----------------|
| test_publish_writes_to_store | publish → 1 row, fields correct | **INV-A1**, INV-G1 | P8 (white-box `bus._store`; rewrite) |
| test_publish_extracts_bot_from_kwargs | `bot` is a column, not meta | INV-A1 (asserts bot column + meta) | P8 |
| test_publish_with_no_meta | empty meta / null bot | INV-A1 covers meta shape | P8 |
| test_subscribers_receive_event | subscriber gets Event with id | INV-A1 (id minted before fan-out) + INV-G5 | P8 |
| test_multiple_subscribers_all_receive | all subscribers fire | INV-G5 (isolation implies all fire) | P8 |
| test_subscriber_exception_does_not_block_others | one raise, others OK | **INV-G5** | P8 |
| test_subscriber_exception_does_not_break_store_write | store write survives raise | **INV-G5** | P8 |
| test_unsubscribe | unsubscribe stops delivery | (behaviour survives via bus API; no INV needed — internal) | P8 (rewrite if API kept) |
| test_publish_implements_logsink_protocol | `publish(self,level,category,message,**meta)` sig | **INV-G1** (signature check) | Keep — frozen (facade contract) |
| test_bus_can_be_bound_to_log_facade | log facade → SQLite e2e | **INV-G1** | Keep — frozen (facade contract) |

## test_event_syncer.py (11 tests) — cross-machine event replication

| Old test | Behaviour covered | Covering INV | Delete-old-when |
|----------|-------------------|--------------|-----------------|
| test_event_dict_roundtrip | event_to_dict/from_dict | (wire-encoding helper; retained until P7 frame flip) | P7 |
| test_local_publish_propagates_to_peer | A → B replication | **INV-B1** | P6 (migrate to harness) |
| test_bidirectional_sync | both directions | **INV-B2** | P6 |
| test_duplicate_batch_is_ignored | dedup, no double row | **INV-B3** | P6 |
| test_resync_on_attach_backfills | attach backfills backlog | **INV-B4** (superset: reconnect + contiguity) | P6 |
| test_host_gossips_guest_event_to_other_guests | g1 → host → g2 | **INV-B5** | P6 |
| test_old_events_excluded_from_resync | resync-side 3-day filter | **INV-B6** | P6 |
| test_old_event_not_pushed_on_publish | emit-side 3-day filter | INV-B6 (documented: partial — see FINDINGS) | P6 |
| test_detach_peer_stops_sync | detach stops delivery | **INV-B7** | P6 |
| test_send_failure_is_swallowed | send raise swallowed, local row kept | **INV-B8** | P6 |
| test_handle_frame_unknown_returns_false | unknown frame → False (chain fall-through) | INV-D2 territory (frame isolation; Phase 7) | P7 |

## test_chat_sync.py (15 tests) — cross-machine chat subscription

| Old test | Behaviour covered | Covering INV | Delete-old-when |
|----------|-------------------|--------------|-----------------|
| test_owner_forwards_local_publish_to_subscribed_peer | owner fans local event to subscribed peer | **INV-C1** | P6 |
| test_owner_publish_to_unwatched_chat_sends_nothing | unwatched chat → nothing | **INV-C1** (negative half) | P6 |
| test_owner_unsubscribe_stops_delivery | unsubscribe stops delivery | INV-C1 + INV-C2 | P6 |
| test_owner_ignores_subscribe_with_missing_fields | malformed subscribe ignored | (defensive; retain thin unit until P6) | P6 |
| test_subscriber_sends_upstream_and_enqueues_events | subscribe upstream + enqueue | **INV-C1**, INV-C3 | P6 |
| test_subscriber_event_for_other_key_not_delivered | wrong-key event not delivered | **INV-C1** (negative) | P6 |
| test_subscriber_refcount_single_upstream_sub | 2 watchers → 1 upstream sub | **INV-C2** | P6 |
| test_subscriber_reconnect_resends_subscribe | reconnect re-sends subscribe | **INV-C6**, INV-D1 | P6 |
| test_host_relays_subscribe_and_events_between_guests | two-hop relay | **INV-C3** | P6 |
| test_host_relay_refcount_across_two_downstream_guests | relay refcount across 2 guests | INV-C2 (relay variant) + INV-C3 | P6 |
| test_host_relay_detach_releases_upstream | detach releases upstream sub | INV-C2 (release edge) + INV-D1 | P6 |
| test_handle_frame_returns_false_for_unknown_type | unknown frame → False | INV-D2 (Phase 7) | P7 |
| test_subscriber_queue_full_drops_without_crashing | bounded queue drop-on-full | **INV-F1** (superset: isolation) | P5/P6 |
| test_local_demand_fires_on_first_and_last_owner_sub | demand edge first/last | INV-C1/D1 (demand drives owner pump; behaviour observable via delivery) | P5/P6 |
| test_local_demand_deactivates_on_peer_detach | demand off on detach | INV-D1 (reconnect) | P5/P6 |

## test_chat_bus.py (7 tests) — location-transparent façade + owner pump

| Old test | Behaviour covered | Covering INV | Delete-old-when |
|----------|-------------------|--------------|-----------------|
| test_subscribe_local_returns_channel_queue | local subscribe → channel queue | (INV-A2/D3 use local subscribe path) | P5 |
| test_subscribe_local_unknown_bot_returns_none | unknown bot → None | (thin unit; retain until P5) | P5 |
| test_subscribe_remote_goes_through_syncer | remote subscribe → upstream frame | **INV-C1**, INV-C2 | P5/P6 |
| test_unsubscribe_local_releases_channel | local unsubscribe releases channel | INV-F1 (channel lifecycle) | P5 |
| test_owner_pump_forwards_local_events_in_order | owner pump forwards in order | **INV-E2** (100+ deltas in order) | P5/P6 |
| test_owner_pump_stops_and_unsubscribes_on_last_leave | pump stops on last leave | INV-C1 (delivery stops) | P5/P6 |
| test_aclose_cancels_pumps | aclose cancels pumps (peeks `bus._pumps`) | (white-box) rewrite black-box: after aclose, publish → subscriber gets nothing | P5/P6 (rewrite — was already private-state peek) |

## test_event_web_api.py (11 tests) — /api/events HTTP contract

| Old test | Behaviour covered | Covering INV | Delete-old-when |
|----------|-------------------|--------------|-----------------|
| all 11 (query/filter/pagination/mark_read/SSE handler) | /api/events read path contract | **INV-G2** (query/filter/before_id/mark_read) | Keep — frozen (exit contract; #3 must-keep) |

## test_event_storage.py (39 tests) — SQLite store internals

| Old test | Behaviour covered | Covering INV | Delete-old-when |
|----------|-------------------|--------------|-----------------|
| all 39 (schema/dedup/cursor/resync/retention SQL) | EventStore is the sole SQLite writer; dedup, cursor, seq | **INV-A1/A2/B3/B4** rely on this; store is a retained component | Keep — frozen (store semantics; changing it is a STOP-and-ask signal) |

## test_telegram_notifier.py (16 tests) — event push notifier

| Old test | Behaviour covered | Covering INV | Delete-old-when |
|----------|-------------------|--------------|-----------------|
| test_no_rate_limit_every_event_delivers | every match → 1 POST, no throttle | **INV-G3** | Keep — frozen (notifier contract) |
| test_delivery_posts_to_bot_api / level & category filters / http-error-safe / detach | notifier delivery + filtering | INV-G3 (delivery + filter behaviour) | Keep — frozen |
| pure helpers (_format_message, _matches_category) | formatting/prefix helpers | (unit-level; retained) | Keep |

## test_event_retention.py (5 tests) — retention sweeper

| Old test | Behaviour covered | Covering INV | Delete-old-when |
|----------|-------------------|--------------|-----------------|
| all 5 (sweep_once deletes old, keeps fresh, loop lifecycle) | retention deletes older-than-cutoff | **INV-G4** | Keep — frozen (retention contract) |

## test_chat_sync_wiring.py (4 tests) — chat hooks chained onto registry/guest_client

| Old test | Behaviour covered | Covering INV | Delete-old-when |
|----------|-------------------|--------------|-----------------|
| test_registry_unknown_frame_chains_event_and_chat | on_unknown_frame offers event syncer first, then chat (the single-WS two-vocabulary chain) | INV-B* + INV-C* (harness _Shuttle reproduces this exact chain end-to-end) | Keep — frozen (wiring contract; Phase 1 PeerTransport must preserve the event-then-chat fall-through) |
| test_registry_attach_registers_chat_peer_and_chains_prior | guest attach registers a chat peer + chains prior on_guest_attached | INV-C1/C2 (delivery proves the peer got attached) | Keep — frozen (wiring contract) |
| test_registry_detach_removes_chat_peer_and_chains_prior | guest detach removes chat peer + chains prior | INV-B7/D1 (detach stops delivery) | Keep — frozen (wiring contract) |
| test_guest_client_unknown_frame_chains_and_connects_peer | guest-side unknown-frame chain + connect attaches "host" chat peer | INV-C3 (two-hop relies on this) | Keep — frozen (wiring contract) |

## test_event_sync_wiring.py (8 tests) — event hooks bridging registry/guest_client → EventSyncer

| Old test | Behaviour covered | Covering INV | Delete-old-when |
|----------|-------------------|--------------|-----------------|
| test_registry_hook_attaches_peer_on_guest_connect / _detaches_on_guest_disconnect | on_guest_attached/detached attach/detach the EventSyncer peer keyed by machine_id | INV-B1/B7 (delivery + detach observe it) | Keep — frozen (wiring contract) |
| test_registry_hook_dispatches_event_batch | registry on_unknown_frame routes event_batch into the syncer | **INV-B1/B3** (replication + dedup) | Keep — frozen (wiring contract) |
| test_guest_client_hook_attaches_host_on_connect / _detaches_on_disconnect | guest on_connect/on_disconnect attach/detach the "host" peer | INV-B1/D1 | Keep — frozen (wiring contract) |
| test_guest_client_hook_dispatches_event_batch | guest on_unknown_frame routes event_batch into the syncer | **INV-B1/B5** | Keep — frozen (wiring contract) |
| test_real_registry_wiring_uses_session_ws / _guest_client_wiring_uses_underscore_ws | the real wiring sends over session.ws / guest_client._ws (the seam the RPC net also links) | INV-R1/R2 (RPC harness links these same WS seams) | Keep — frozen (wiring contract; Phase 1 PeerTransport must send over the same WS) |

## test_tool_log_event.py (7 tests) — log_event MCP builtin → boxagent.log facade

| Old test | Behaviour covered | Covering INV | Delete-old-when |
|----------|-------------------|--------------|-----------------|
| all 7 (basic/explicit level/prefix idempotence/meta passthrough/level fallback/missing category+message errors) | the log_event tool writes through the boxagent.log facade → EventBus.publish with correct level/category/message/meta | **INV-G1** (facade signature/behaviour) + INV-A1 (row shape) | Keep — frozen (facade contract; the log_event tool is a public write path onto the bus and must not regress) |

---

## FINDINGS surfaced by building the net against TODAY's code

1. **EventSyncer cross-batch tail-drop (latent).** When a single debounce
   window buffers more than `MAX_BATCH` (500) events, `_flush` delivers the
   first 500 and calls `_schedule_flush()` for the remainder — but that call
   no-ops because the *currently running* flush task is not `done()` yet
   (`sync.py:199`). The tail sits in `_buffer` until the NEXT locally published
   event triggers another flush. **Order is never scrambled and nothing is
   permanently lost** (the tail arrives contiguously on the next flush, and
   cursor-resync would recover it on reconnect regardless), but a naive
   "publish 550, read immediately" observes only 500. `INV-E3` characterizes
   this exactly (drives the tail with one nudge event, then asserts full
   contiguity + order). The bus migration should FIX the one-shot-tail gap
   (flush the remainder in the same debounce cycle); when fixed, `INV-E3`'s
   `_nudge` becomes a harmless no-op extra event and the test still passes.

2. **Emit-side vs resync-side 3-day filter asymmetry (by design, pinned by
   INV-B6).** The window is filtered on both the emit path (`_on_local_event`,
   `sync.py:190`) and the resync path (`_handle_resync` `since_ts`). INV-B6
   pins the resync-side filter (deterministic). The emit-side filter is exercised
   by the old `test_old_event_not_pushed_on_publish` via a private-method call
   (`sync_a._on_local_event(ancient)`); that is white-box and is NOT ported into
   the frozen black-box layer — INV-B6 covers the observable resync-side filter,
   and the emit-side path is otherwise unreachable through the public publish
   API without back-dating a store row. Flagged so P6 does not silently drop the
   emit-side filter.

3. **WebChannel and EventStreamSubscriber had zero direct unit tests.** The
   frozen net now covers WebChannel fan-out via INV-A2/C*/E2/F1 (through the
   real `_publish`) and the chat path end to end. EventStreamSubscriber (the
   /events SSE subscriber) is covered indirectly by INV-G2 for the read path;
   its `call_soon_threadsafe` enqueue path is a Phase 4 concern (cross-thread
   publish boundary, run_1 risk R6) and is intentionally left for that phase.

## RPC (R1..R6) — built in Phase 0

RPC round-trip invariants need a real per-node aiohttp server (loopback
re-issue hits `http://127.0.0.1:port` and re-runs real `_handle_web_*`
handlers), which does not fit the fan-out-only in-process shuttle. They are
therefore built on a SEPARATE harness — `tests/unit/_rpc_bus_harness.py` — that
stands up an `aiohttp.test_utils.TestServer` per node (three real routes each,
spy-wrapped) and links the nodes' real `GuestRegistry`/`GuestClient` over a real
WebSocket (production wiring minus the devtunnel dial). Self-tests:
`tests/unit/test_rpc_bus_harness.py`. Invariants R1..R6 live in the same frozen
`test_message_bus_invariants.py`:

| INV | Guards | Green against |
|-----|--------|---------------|
| R1 | single hop host→guest returns guest's REAL body, id-correlated | `GuestSession.call` + guest `_handle_rpc` loopback |
| R2 | reverse RPC loopback re-issue hits the REAL host handler (spy-proven) | `registry._serve_inbound_rpc` |
| R3 | two-hop gA→host→gB returns correct body (nested pending pairs) | host loopback forwarding onward |
| R4 | 50 concurrent out-of-order replies never cross (id correlation) — the key one | `_pending` dict correlation |
| R5 | unreachable machine times out cleanly + no pending-future leak | `call`'s `wait_for` + `finally` pop |
| R6 | RPC is concurrent, not serialized behind one pump | `asyncio.create_task` per inbound rpc |

Built NOW (Phase 0), not deferred, because the RPC host/guest mirror collapses
at **Phase 1.5** — this net is the guard for that refactor.

Existing RPC tests and their disposition (from run_2 tester):

| Old test | Disposition | Migrates to |
|----------|-------------|-------------|
| test_cluster_rpc.py::test_local_returns_none / _unknown_machine_404 / _no_routing_503 | Keep — frozen (routing contract, bus-independent) | — |
| test_cluster_registry.py::TestRpcRoundtrip::test_call_resolves_on_rpc_resp | Migrate (impl-coupled) | **INV-R1** (built in Phase 0) |
| test_cluster_registry.py::TestRpcRoundtrip::test_call_timeout | Migrate + strengthen (add pending-cleanup) | **INV-R5** (built in Phase 0; adds pending-cleanup assertion) |
| test_admin_cluster_restart.py (3) | Keep — frozen (HTTPS side-path `fetch_host_json`, NOT WS RPC) | — |


## Phase 6 update — wiring merged

`events/sync_wiring.py` + `cluster/chat_sync_wiring.py` (and their tests
`test_event_sync_wiring.py` / `test_chat_sync_wiring.py`) were DELETED and
replaced by `cluster/bus_wiring.py` + `test_bus_wiring.py`. The old chained
`on_unknown_frame` install-order constraint is gone: one installer owns the
registry/guest_client callbacks and dispatches event_* then chat_* frames.
Behavior preserved (each syncer still receives its own frames, no swallow) —
covered by test_bus_wiring.py (5) + the frozen INV-B*/C*/D2.

Also this phase: one shared MessageBus instance is now created in gateway and
injected into EventBus + every WebChannel (events + chat ride one instance).
