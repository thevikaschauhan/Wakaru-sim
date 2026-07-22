# TDD — Per-store persistent Zep graph with outcome feedback

**Issue:** [#61](https://github.com/thevikaschauhan/Wakaru-sim/issues/61)
**Companion docs:** [PRD](./PRD.md), [SCHEMA](./SCHEMA.md)
**Verified against:** Wakaru `origin/main` @ `a3d7a1a`; `zep-cloud==3.13.0` SDK source at tag `v3.13.0`; vakaru-engine @ `1bb1d95`

> **Revision 2 (2026-07-21), after external design review.** Changes: store
> priors move to engine SQL delivered in the request envelope (never
> `graph.search`); a bounded read adapter replaces direct Zep reads and the
> ReportAgent full-scan tools are disabled for store graphs; provisioning
> gains a readiness barrier; retention/erasure is rebuild-based; the outcomes
> endpoint binds tenant identity into the signed body and its idempotency
> horizon is raised; engine prerequisites are filed issues
> (vakaru-engine#192, #193).
>
> **Revision 3 (2026-07-21), after review-round 2.** Lifecycle mutations are
> one signed **command contract** (§7): engine-driven `rebuild` /
> `redact_customer` / `offboard` POSTs with operation ids and verified
> completion — the r2 DELETE endpoint is gone (the HMAC middleware is
> POST-only; a DELETE cannot bind tenant into signed bytes). The replay feed
> is normative (engine#192-E). Customer redaction gains a per-shopper
> **watermark** enforced on every write and replay page. The current
> generation is **envelope-authoritative** (engine ledger → `memory_generation`
> on every analyze request); the r2 generation-0 Redis fallback is removed as
> unsafe. ReportAgent no longer holds a store-graph id at all — it receives
> one materialized, capped working set (§5). Idempotency keys standardize on
> `attempt_id`.

## 1. Verified facts this design uses

`zep-cloud` 3.13.0 (SDK source at tag `v3.13.0`):

- `graph.create(graph_id, name, description)`; `graph.get(graph_id)`;
  `graph.delete(graph_id)`.
- `graph.add(data, type, graph_id, created_at: str | None)` — per-episode
  `created_at` override exists; episodes are stamped with real event time.
- `graph.add_batch(episodes, graph_id)` (already used, `graph_builder.py:256`).
- `graph.search(query, graph_id, limit, min_score, reranker,
  center_node_uuid, ...)` — **relevance-ranked, bounded**. Fit for
  qualitative retrieval; unfit for counts/rates (PRD invariant).
- `graph.set_ontology(graph_ids=[...])` external client (already used,
  `graph_builder.py:219-223`) — per-graph ontology assignment.
- `graph.episode.get_by_graph_id(graph_id, lastn)` — **`lastn`-only; no
  cursor pagination, no time filter** (revision 2 correction — the previous
  revision assumed pageable enumeration). `episode.get(uuid_)`,
  `episode.delete(uuid_)` exist; the effect of episode-delete on derived
  nodes/edges is **unproven** (V-1).
- `graph.clone(source_graph_id, target_graph_id)` — available; rebuild uses
  replay, not clone (clone would copy the data we are trying to expire).

Wakaru:

- `merchant_id`: canonical lowercase UUID, request-bound
  (`app/__init__.py:236-252`), in job meta on the async path; sentinel
  `00000000-0000-0000-0000-000000000000` (`paths.py:49`).
- Whole-graph read seams that must be bounded for store graphs (revision 2 —
  verified, this list is why the adapter exists):
  `SimulationManager.prepare_simulation` → `ZepEntityReader` →
  `fetch_all_nodes/fetch_all_edges` (`simulation_manager.py:419`,
  `zep_entity_reader.py:139`), and **ReportAgent's LLM-invocable tools**:
  `panorama_search` (`zep_tools.py:1146`) calls `get_all_nodes` (`:651`) and
  `get_all_edges` (`:679`); `get_all_*` has ~8 further call sites in
  `zep_tools.py`. The previous revision's claim that ReportAgent reads are
  "query-scoped already" was **wrong**.
- Auth stack for the new endpoints: X-API-Key (#10) + HMAC body signature
  (#11) + `X-Merchant-Id` binding (#24). The HMAC covers
  `"<ts>.<rawbody>"` only — the header is **outside** the signature, which
  is why revision 2 puts `merchant_id` inside the body (§4.2; converges with
  issue #73) — and `verify_internal_hmac` is **POST-only** (`if
  request.method != "POST": return None`, `cart_recovery.py`), which is why
  revision 3 models every lifecycle mutation as a signed POST command (§7)
  and no DELETE endpoint exists in this design.

Engine (@ `1bb1d95`):

- `orders.matched_anonymous_id` (033); `abandonment_detections` PK
  `(shopify_store_id, anonymous_id, episode_type, checkout_started_at)`
  (034/040); tenant-scoped identity (035); forwarding-outbox pattern (036).
- **Gaps now owned by filed issues:** the Wakaru-bound payload lacks the
  episode-key fields and carries `email`/`customer_name` instead
  (envelope = engine#192-A); no durable episode↔send↔angle/discount link
  exists — `handlers/email_send.go` receives only
  `{email_document_id, to, subject, html, text}` (attempts = engine#192-C);
  outcome derivation + outbox = engine#192-D; `shop/redact` /
  `customers/redact` are stubs (state machine = engine#193; the PII log line
  in those stubs = engine#191).

## 2. Graph identity and provisioning (D7, D9)

- `graph_id = "merchant_" + merchant_id.replace("-", "") + suffix` where
  `suffix` is empty for generation 0 and `_r<N>` for rebuild generations —
  deterministic, charset `[a-z0-9_]` (same alphabet as the proven
  `mirofish_*` ids).
- **Current-generation resolution (revision 3 — the r2 generation-0 Redis
  fallback is removed: after a rebuild, generation 0 is deleted or contains
  exactly the expired/redacted data the rebuild removed, so falling back to
  it on Redis loss was unsafe).** The durable pointer lives in the **engine
  operations ledger** (#192-E) and arrives on **every analyze request** as
  the envelope's `memory_generation` field; lifecycle commands (§7) carry it
  too. Wakaru's `zep:store:<merchant_id>` hash is a **cache**: on mismatch,
  the envelope/command value wins, the cache is reconciled, and drift is
  alerted. If neither an envelope value nor a cache entry is available
  (fresh merchant), generation 0 applies; if the cache is lost *and* the
  request predates #192's envelope, the treated run **falls back to the
  throwaway path** (fail closed) rather than guessing a generation. All
  generations remain enumerable from Zep by prefix `merchant_<hex32>` —
  used for offboarding enumeration and the stale-generation check (§7),
  which prove existence; *currency* is always the ledger's claim.
- Provisioning with a **readiness barrier** (revision 2 — closes the
  loser-before-ontology race):

```python
def ensure_store_graph(client, merchant_id) -> str:
    if is_tombstoned(merchant_id):                    # PRD §4; redaction wins
        raise StoreMemoryTombstoned(merchant_id)
    rec = ledger_get(merchant_id)                     # zep:store:<mid> hash
    if rec and rec.status == "ready":
        if rec.ontology_version != ONTOLOGY_VERSION:  # lazy version upgrade
            apply_fixed_ontology(client, rec.graph_id)
            ledger_stamp_version(merchant_id, ONTOLOGY_VERSION)
        return rec.graph_id
    graph_id = store_graph_id(merchant_id)            # generation 0
    claimed = ledger_claim_provisioning(merchant_id, graph_id)  # SET NX on status=provisioning
    if claimed:
        try:
            client.graph.create(graph_id=graph_id, name="Store memory",
                                description=ONTOLOGY_VERSION)
        except AlreadyExistsError:                    # V-2 pins exact class
            pass                                      # graph exists; finish setup idempotently
        apply_fixed_ontology(client, graph_id)        # idempotent set_ontology
        ledger_mark_ready(merchant_id, graph_id, ONTOLOGY_VERSION)
        return graph_id
    return wait_until_ready(merchant_id, timeout=30)  # poll ledger; raise on timeout
```

  **No write path may touch a graph whose ledger status is not `ready`** —
  the losing racer waits on the barrier instead of writing episodes into an
  ontology-less graph. The claim is a Redis `SET NX` with a short TTL so a
  crashed provisioner's claim expires and the next caller retries setup
  (create + set_ontology are both idempotent). Sentinel merchant
  short-circuits before all of this (PRD §6).

## 3. Fixed ontology (D1)

`backend/app/services/store_ontology.py` (new) defines
`CART_RECOVERY_ONTOLOGY_V1` in the exact `{entity_types, edge_types}` shape
`set_ontology` already consumes (`graph_builder.py:136-223`), with the types
in SCHEMA §2. `ONTOLOGY_VERSION = "cr-v1"`.

- Phase 0b swaps `OntologyGenerator().generate(...)` out of the analysis path
  (`cart_recovery_workflow.py:118-122`) for the fixed ontology.
- **Revision 2 (per the deterministic-source invariant): the ontology carries
  no counters.** Attributes are qualitative descriptors; anything countable
  lives in engine SQL (SCHEMA §2 notes what was removed).
- Evolution rule: additive only (new types/attributes ⇒ `cr-v2`), lazily
  re-applied via the `ensure_store_graph` version check. Renames/removals
  require a rebuild generation, which the retention machinery (§7) already
  exercises routinely.

## 4. Write path (Phases 1-2)

### 4.1 Cart episodes (Phase 1, dual-write) — gated on engine#192-A

The episode is built **from the envelope**, not from `ShopifyCartData`
prose fields (revision 2 — the required identity fields do not exist in
today's payload; verified `cart_recovery/shopify_formatter.py`). In
`_run_analysis`, after the throwaway build (unchanged), when the merchant is
allowlisted and the envelope carries `schema_version >= 1`:

- `ensure_store_graph(...)`, then `graph.add(graph_id=store_graph,
  type="json", data=cart_episode_json, created_at=<envelope occurred_at>)` —
  one structured episode per analysis (SCHEMA §4.1), keyed by the envelope's
  `event_id`, into the envelope-named generation (§2).
- **Watermark enforcement (revision 3):** before any episode write, the
  shopper's redaction watermark (delivered via commands/envelope, cached per
  SCHEMA §3) is checked — an episode for a watermarked `anonymous_id` with
  `occurred_at < cutoff` is **dropped and counted**, never written. This is
  defense in depth on top of engine-side filtering, and it closes the
  in-flight-analysis race that could re-write erased data after a rebuild.
- Failure is guarded: a store-memory write error logs a warning and never
  fails the paid analysis (same contract as ledger writes in #72). A
  tombstone hit is treated the same way (skip + warn), not an error.
- No processed-wait in Phase 1 (nothing reads the store graph in the same
  run yet).

### 4.2 Outcome episodes (Phase 2, Track O) — gated on engine#192-C/D

New blueprint `backend/app/api/store_memory.py`:

- `POST /api/store-memory/outcomes` — internal, engine-only, behind the
  existing X-API-Key + HMAC + `X-Merchant-Id` middleware chain. **Revision 2
  identity binding:** the body carries `merchant_id` (SCHEMA §4.2) and the
  handler rejects (403) any mismatch with the bound header identity — tenant
  identity is thereby inside the signed bytes. **Signing (plan revision 3 —
  the complete issue-#73 contract, not a subset):** the store-memory
  blueprint implements, from day one, the full canonical envelope
  `version | key_id | method | canonical_path | merchant_id | timestamp |
  nonce | SHA256(body)` with:
  - **atomic nonce consumption** server-side (`SET NX` with the replay-window
    TTL) — an identical replayed request is rejected even inside the
    timestamp window;
  - **fail-closed nonce store**: if Redis is unavailable, these privileged
    endpoints return 503 rather than accepting an unverifiable nonce (the
    engine's outbox/state machine re-drives, so fail-closed costs latency,
    never data);
  - **`Idempotency-Key` bound to the body hash**: the recorded slot stores
    `SHA256(body)`; the same key with a different body is a 409 conflict,
    never a stale replay;
  - **versioned, key-id'd credentials** supporting overlapping rotation;
    unknown or retired key ids fail closed;
  - constant-time signature comparison and the existing timestamp window.
  Cross-repo conformance tests cover canonicalization, clock skew, nonce
  replay, nonce-store outage, tenant substitution, same-key/different-body
  conflict, and key rotation. Both sides are new code, so there is no
  migration burden; the legacy cart-recovery endpoints migrate separately
  under #73.
- Idempotency: `Idempotency-Key` header required — **= `attempt_id`**
  (revision 3: r2 left `event_id` here while SCHEMA said `attempt_id`; the
  attempt is the correct granularity because one cart may legitimately
  receive multiple recovery attempts, and all three sources — this TDD, the
  SCHEMA, and engine#192-D — now agree). Scope `outcomes:<merchant_id>`,
  reusing `idempotency.py` with a **14-day TTL for this scope** (revision 2
  — must exceed the engine outbox's backoff ceiling so a slow retry can
  never replay past the window; 24 h did not). The durable at-most-once
  layer remains the engine outbox's `UNIQUE (shopify_store_id, attempt_id)`
  + terminal states (SCHEMA §5.5); the Redis TTL only has to cover one
  delivery sequence.
  Duplicates that somehow pass both layers land in the graph as
  duplicate-keyed episodes — tolerable because **no exact value is computed
  from the graph** (PRD invariant); the engine ledger, where exactness
  lives, is UNIQUE-constrained.
- Handler: `ensure_store_graph` → `graph.add(type="json",
  created_at=<resolved_at>)` → 202. A 503 (Zep down) must `release()` the
  idempotency slot (work not performed — the safe-to-retry path per
  `idempotency.py`'s contract); an ambiguous failure after `graph.add` must
  **not** release it.

## 5. Read path (Phase 3) — the D3 bounded adapter

**`StoreGraphReader`** (new, `backend/app/services/store_graph_reader.py`) is
the **only** module allowed to read a `merchant_*` graph, and **the graph id
never leaves it** (revision 3 — the r2 text let ReportAgent keep
"query-scoped search," which meant it held the raw graph id and bypassed the
declared single read boundary; that contradiction is removed):

- Exposes exactly one operation:
  `build_working_set(store_graph_id, cart_anchors) -> WorkingSet` — called
  **once per treated run**, before simulation. Hard caps at every level
  (revision 3 — all were previously uncapped except entities/edges):
  anchors ≤ 8 (deterministic priority: product titles, then collection,
  then price band; excess dropped and logged), exactly one `graph.search`
  call per anchor (call budget = anchor count), per-call `limit` 10,
  union/dedupe to ≤ 30 entities / ≤ 60 edges
  (`STORE_MEMORY_WORKING_SET_MAX`), serialized size ≤ 8 KB (truncated
  oldest-first, truncation logged).
- The returned `WorkingSet` is a **plain materialized object** (entities,
  edges, provenance timestamps). Persona generation and ReportAgent consume
  this object; **neither receives a store-graph id, ever** — for treated
  runs, ReportAgent is constructed with `graph_id=None`, its Zep-reading
  tools (`panorama_search` and the query tools) removed from the tool schema
  and dispatch map, and two context inputs instead: the `WorkingSet` and the
  envelope's `StorePriors`. There is no store-graph read a treated-run
  ReportAgent can express, capped or otherwise.
- Defense in depth, enforced and tested:
  - `zep_tools.py` `get_all_nodes` / `get_all_edges` / `panorama_search`
    (and `zep_entity_reader.fetch_all_*` callers) gain a guard that raises
    `StoreGraphScanBlocked` when `graph_id.startswith("merchant_")` — even
    if a future caller bypasses the adapter.
  - Tests fail the suite if any store-graph code path issues **any** Zep
    read outside `StoreGraphReader` (fake client records call types and
    graph ids; see §9).

**Priors come from the envelope, not the graph** (revision 2): the engine
ships `StorePriors` (recovery rate by actually-sent angle, discount
effectiveness, repeat-abandoner flag — engine#192-B, exposure-attributed via
#192-C attempts) as a block in the analyze payload. Wakaru passes it to
persona generation and the report requirement string verbatim. `graph.search`
supplies only qualitative episodes for the working set.

Injection points (unchanged seams, new inputs):

- Persona generation: `simulation_manager.py:419` seam gains an optional
  `entities_override` = the `WorkingSet`'s entities; the cart's own entities
  always dominate (anchor set), historical entities enrich.
- Report agent: receives the `WorkingSet` + `StorePriors` as context;
  `graph_id=None`, Zep tools removed (revision 3, as above).
- Treated runs skip the throwaway graph entirely; the run's seed episodes go
  to the store graph with a bounded processed-wait on just that run's episode
  uuids (existing `_wait_for_episodes`, `graph_builder.py:278`, takes
  explicit uuid lists — reused unchanged).

Rollback per merchant = removal from allowlist ⇒ next run takes the
throwaway path again. No data migration in either direction.

## 6. Concurrency (D9)

- Provisioning: readiness barrier (§2) — no writer proceeds before `ready`;
  crashed claims expire; setup steps idempotent.
- Same-store concurrent episode adds: Zep ingests per graph asynchronously;
  batches interleave safely (order is by stamped `created_at`). Same-run
  ordering ("this run's episodes processed before this run's search") is
  enforced by the uuid-scoped `_wait_for_episodes`.
- Rebuild vs analysis race: the rebuild operation builds the **new**
  generation to completion, verifies it, reports it in its terminal status
  (the engine ledger then names it in subsequent envelopes), and deletes the
  old generation only after a grace period ≥ the max run wall clock (2 h) —
  an in-flight analysis keeps reading the generation it resolved at run
  start (graph ids are immutable per run), so no run's reads can dangle.
- Redaction vs analysis race (revision 3 — two independent guards):
  merchant tombstone at `ensure_store_graph` blocks recreation after
  `offboard`; the **per-shopper watermark** (§4.1) blocks re-writes of
  erased episodes by in-flight runs after `redact_customer`. Both flows end
  with a verify pass (re-enumeration / generation check) and the engine
  re-drives until the evidence holds (engine#193).

## 7. Retention, erasure, offboarding (D4, PRD §4)

**Primary mechanism: engine-driven rebuild from the engine event ledger**
(revision 2 replaced episode-delete paging, which the `lastn`-only reader
cannot support; revision 3 makes the feed a **normative contract** —
engine#192-E — instead of a hand-wave, with the engine as the driver so the
data flows in the direction the topology already supports):

- The **engine** schedules rebuilds (rolling cadence: every merchant at
  least every `STORE_MEMORY_REBUILD_EVERY_DAYS`, jittered; plus immediately
  on `redact_customer` and on ontology-version migrations) and drives each
  one through the command contract (SCHEMA §5.2-5.4):
  1. `rebuild` command → Wakaru: `{operation_id, snapshot_id,
     memory_generation_next, expected_event_count, redaction watermarks,
     retention cutoff}`. Wakaru provisions generation `_r<N+1>` via the §2
     path (ontology applied, `provisioning`).
  2. Engine pushes **replay pages** referencing the `operation_id`: stable
     `occurred_at`-ascending order, `page_no`/`page_count`, per-page count +
     checksum, bounded page size, resume-from-page on retry. Wakaru applies
     each page with `graph.add_batch` (stamped `created_at`), enforcing
     watermarks per episode (§4.1), idempotent per `(operation_id, page_no)`.
  3. On the final page: wait processed, verify episode count against
     `expected_event_count`, flip the current-generation pointer, and report
     **`cleanup_pending`** (plan-r3 correction: this is explicitly an
     in-progress state, not completion — the r2 plan had the terminal status
     emitted here, contradicting step 4). On a count/checksum discrepancy:
     `failed`, no flip. The engine ledger records the new generation and
     starts naming it in envelopes (§2) from the flip.
  4. Wakaru deletes prior generations after the 2 h grace (§6), re-lists the
     `merchant_<hex32>` prefix, and only when stale generations are
     **verified absent** reports terminal **`verified_current`**. The
     operation lifecycle is `running → cleanup_pending → verified_current |
     failed`; the engine re-drives on `failed`, treats `cleanup_pending` as
     in-progress, and marks retention/redaction complete on nothing short of
     the terminal status.
- **This machinery is also the erasure mechanism** (customers/redact ⇒
  immediate rebuild excluding the watermarked shopper) and the
  ontology-migration mechanism (renames/removals ⇒ rebuild on the new
  version). One code path, exercised routinely, and expiry is **proven** by
  the replay window + count verification rather than inferred.
- **Stale-generation reaper (revision 3):** a Wakaru maintenance occurrence
  (same scheduling fabric as #72 TDD §3.4, own Sentry cron monitor)
  enumerates `merchant_*` graphs by prefix and **alerts** on any graph that
  is neither a ledger-known current generation nor inside a live operation
  — it never auto-deletes (deletion authority stays with verified
  operations); the alert routes to a re-driven cleanup operation.
- Opportunistic episode-delete (age > 180 d via `lastn`-windowed reads) may
  run between rebuilds **only after** V-1 verifies deletion semantics; it is
  an optimization, never the correctness mechanism.
- Per-graph cap: 20,000 episodes enforced at rebuild (excess oldest dropped,
  logged).

**Offboarding (revision 3 — the r2 `DELETE /api/store-memory/<merchant_id>`
is removed: `verify_internal_hmac` is POST-only and signs only
`timestamp.body`, so a bodyless DELETE cannot bind the tenant into signed
bytes; the r2 text's "body-bound merchant" was inapplicable to it).**
Offboarding is the `offboard` command on the same signed POST contract:
tombstone first, delete **all** generations enumerated from Zep by prefix
`merchant_<hex32>` (never from Redis), ledger cleanup, then terminal status
`verified_empty` only after a re-enumeration returns none — a
straggler-recreated graph fails the verify and the engine re-drives
(engine#193). Idempotent per `operation_id` (repeat ⇒ re-verify ⇒ same
terminal state; Zep 404s treated as success — same V-class check as #72's
V-1).

## 8. Config

| Var | Default | Notes |
|---|---|---|
| `STORE_MEMORY_ENABLED` | `false` | Global kill switch, read live per the `Config.validate` convention |
| `STORE_MEMORY_MERCHANT_ALLOWLIST` | empty | Comma-separated merchant UUIDs; empty = no one (Phase 1-3 gate) |
| `STORE_MEMORY_RETENTION_DAYS` | `180` | Floor 30 |
| `STORE_MEMORY_MAX_EPISODES` | `20000` | Per graph, enforced at rebuild |
| `STORE_MEMORY_REBUILD_EVERY_DAYS` | `30` | Rolling rebuild cadence (engine-side scheduling input, #192-E), jittered |
| `STORE_MEMORY_WORKING_SET_MAX` | `30` | Entity cap (D3); edges 2×, anchors ≤ 8, one search call per anchor, per-call limit 10, serialized ≤ 8 KB (§5, revision 3) |
| `STORE_MEMORY_OUTCOME_IDEM_TTL_DAYS` | `14` | Outcomes idempotency scope TTL (§4.2) |
| `ATTRIBUTION_WINDOW_DAYS` | `7` | Engine-side, in the #192 contract |

## 9. Testing plan

Unit (fake Zep client that **records call types**, existing patterns):

1. `store_graph_id` determinism + generation suffixes + sentinel refusal.
2. Provisioning: exists-ready / not-exists / create-race → **loser blocks on
   readiness barrier and never writes pre-ontology** (revision 2); crashed
   claim expires and setup resumes; lazy ontology-version upgrade.
3. Tombstone: `ensure_store_graph` raises; write path skips + warns; a
   post-tombstone provisioning attempt cannot recreate.
4. Dual-write guard: store-memory write failure does not fail the analysis.
5. **Read-boundary ban (revision 3, was scan ban):** any
   `get_all_nodes`/`get_all_edges`/`panorama_search` against a `merchant_*`
   graph id raises `StoreGraphScanBlocked`; a treated-run ReportAgent is
   constructed with `graph_id=None` and no Zep tools in its schema/dispatch;
   the fake client asserts **zero Zep reads of any kind outside
   `StoreGraphReader`** across a full treated-run pipeline test.
6. Working set: all caps enforced (anchors dropped beyond 8 with logging,
   one call per anchor, per-call limit, entity/edge caps, serialized-size
   truncation); cart anchors always present; empty store graph ⇒ degenerates
   to cart-only.
7. Outcomes endpoint: auth chain (reuse `test_api_auth` /
   `test_cart_recovery_hmac` fixtures); **body-merchant vs header mismatch ⇒
   403** (revision 2); `Idempotency-Key = attempt_id` replay within TTL;
   503 releases the slot, post-add ambiguity does not; malformed-body 400
   with no PII in logs.
8. Commands endpoint (revision 3): same auth chain; unknown `kind` ⇒ 400;
   `operation_id` idempotent replay returns the same operation; watermark
   write-barrier drops pre-cutoff episodes for the watermarked shopper on
   both the write path and replay pages (counted, logged).
9. Rebuild operation: page ordering/`page_no` idempotency; count-vs-manifest
   verification failure ⇒ `failed` terminal (no flip); **the full state walk
   `running → cleanup_pending` (at flip) `→ verified_current` (only after
   grace deletion + stale generations re-listed absent)** — asserting the
   status is *not* terminal at flip; shopper-exclusion drops exactly the
   watermarked `anonymous_id`'s pre-cutoff episodes; cap enforcement;
   envelope `memory_generation` mismatch with cache ⇒ envelope wins + drift
   alert.
10. Offboard operation: tombstone-first ordering; prefix enumeration covers
    stale generations; `verified_empty` only on empty re-list (straggler
    recreation ⇒ `failed`, engine re-drives); idempotency per
    `operation_id`.
11. Stale-generation reaper: alerts on unknown generations, never deletes.
12. #72 interlock: sweeper regex + registry `graph_kind` assertions reject
    `merchant_*` ids (asserted in both specs' suites).

Integration (staging, env-gated like the repo's other gated tests): one real
end-to-end pilot run per phase-gate (PRD §6), including a replay of the same
outcome delivery twice (idempotency) and one full rebuild-and-verify cycle.

## 10. Open verification items (all block the phase that uses them, none assumed)

- **V-1** (blocks the *opportunistic* delete optimization only — no longer
  blocks retention, which is rebuild-based): episode-deletion semantics for
  derived nodes/edges.
- **V-2** (blocks Phase 1): exact SDK exception classes for get-miss /
  create-conflict (pins the `except` clauses in §2).
- **V-3** (blocks Phase 3): `graph.search` result shape and score behavior on
  JSON-episode-derived entities at our data shape; tunes `min_score`.
- **V-4** (blocks Phase 4 default-on): Zep plan graph-count/size headroom at
  full merchant count (operator, PRD D6).
- **V-5** (blocks Phase 1, revision 2): `get_by_graph_id` `lastn` practical
  ceiling at our episode volumes (used by rebuild spot-verification and the
  optional delete optimization; rebuild replay itself does not depend on it).
