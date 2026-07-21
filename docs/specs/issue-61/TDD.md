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
- Auth stack for the new endpoint: X-API-Key (#10) + HMAC body signature
  (#11) + `X-Merchant-Id` binding (#24). The HMAC covers
  `"<ts>.<rawbody>"` only — the header is **outside** the signature, which
  is why revision 2 puts `merchant_id` inside the body (§4.2; converges with
  issue #73).

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
  `mirofish_*` ids). The **current** generation is resolved via the ledger
  (`zep:store:<merchant_id>.graph_id`, SCHEMA §3) with the deterministic
  generation-0 name as the fallback; all generations are enumerable from Zep
  itself by prefix `merchant_<hex32>` (Redis is reconstructible, never
  load-bearing for privacy operations).
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
  `event_id`.
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
  identity is thereby inside the HMAC-signed bytes with zero middleware
  change. When issue #73 lands its stronger signature (method/path/ts/
  merchant/body-hash), this endpoint adopts it as-is; the body field is the
  forward-compatible half.
- Idempotency: `Idempotency-Key` header required (= the attempt's
  `event_id`), scope `outcomes:<merchant_id>`, reusing `idempotency.py` with
  a **14-day TTL for this scope** (revision 2 — must exceed the engine
  outbox's backoff ceiling so a slow retry can never replay past the
  window; 24 h did not). The durable at-most-once layer remains the engine
  outbox's `UNIQUE (shopify_store_id, episode_key)` + terminal states
  (SCHEMA §5.3); the Redis TTL only has to cover one delivery sequence.
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
the **only** module allowed to read a `merchant_*` graph:

- Exposes exactly: `search_working_set(cart_anchors, limit, min_score)` —
  N × `graph.search` calls (one per anchor string: product titles,
  collection, price band), union, dedupe, hard caps
  (≤ 30 entities, ≤ 60 edges, `STORE_MEMORY_WORKING_SET_MAX`).
- Does **not** expose any list-all operation. Enforcement is structural and
  tested (revision 2, closing the ReportAgent hole):
  - `zep_tools.py` `get_all_nodes` / `get_all_edges` / `panorama_search`
    gain a guard that raises `StoreGraphScanBlocked` when
    `graph_id.startswith("merchant_")` — defense in depth even if a future
    caller bypasses the adapter.
  - For treated runs, ReportAgent is constructed with a **restricted
    toolset**: `panorama_search` removed from the tool schema and the
    dispatch map; its remaining tools operate on the bounded working set and
    query-scoped search only.
  - Tests fail the suite if any store-graph code path issues a list-all SDK
    call (fake client records call types; see §9).

**Priors come from the envelope, not the graph** (revision 2): the engine
ships `StorePriors` (recovery rate by actually-sent angle, discount
effectiveness, repeat-abandoner flag — engine#192-B, exposure-attributed via
#192-C attempts) as a block in the analyze payload. Wakaru passes it to
persona generation and the report requirement string verbatim. `graph.search`
supplies only qualitative episodes for the working set.

Injection points (unchanged seams, new inputs):

- Persona generation: `simulation_manager.py:419` seam gains an optional
  `entities_override` = working-set entities; the cart's own entities always
  dominate (anchor set), historical entities enrich.
- Report agent: receives the working set + `StorePriors` as context;
  restricted toolset as above.
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
- Rebuild vs analysis race: rebuild builds the **new** generation to
  completion, then flips the ledger pointer atomically; an in-flight
  analysis keeps reading the old generation for the rest of its run
  (graph ids are immutable per run — resolved once at run start). The old
  generation is deleted after a grace period ≥ the max run wall clock
  (2 h), so no run's reads can dangle.
- Redaction vs analysis race: tombstone check at `ensure_store_graph` +
  offboarding runs delete-and-verify with a re-list; a graph recreated by a
  straggler run started before the tombstone is caught by the verify pass
  (engine#193 re-drives until verified-empty).

## 7. Retention, erasure, offboarding (D4, PRD §4)

**Primary mechanism: rebuild from the engine event ledger** (revision 2 —
replaces episode-delete paging, which the `lastn`-only reader cannot support
and whose derived-artifact semantics are unproven):

- A scheduled maintenance job (on #72's `maintenance` queue, unique
  occurrence ids + singleton lock per #72 TDD §3.4, `job_id` prefix
  `store-memory-rebuild-`) processes each store graph on a rolling cadence
  (each graph rebuilt at least every 30 days, jittered):
  1. Request the merchant's retained events (≤ 180 d, minus redacted
     shoppers) from the engine (#192's event ledger is the source of truth;
     the replay feed is part of that contract).
  2. Create generation `_r<N+1>` via the §2 provisioning path (ontology
     applied, ledger `provisioning`).
  3. Replay episodes (`graph.add_batch`, stamped `created_at`), wait
     processed, spot-verify counts.
  4. Atomically flip `zep:store:<mid>.graph_id` to the new generation
     (`ready`); delete the old generation after the 2 h grace (§6).
- **This machinery is also the erasure mechanism** (customers/redact ⇒
  immediate rebuild excluding the shopper) and the ontology-migration
  mechanism (renames/removals ⇒ rebuild on the new version). One code path,
  exercised routinely — not a quarterly afterthought, and expiry is
  **proven** ("oldest episode age" metric from the rebuild's own replay
  window) rather than inferred.
- Opportunistic episode-delete (age > 180 d via `lastn`-windowed reads) may
  run between rebuilds **only after** V-1 verifies deletion semantics; it is
  an optimization, never the correctness mechanism.
- Per-graph cap: 20,000 episodes enforced at rebuild (excess oldest dropped,
  logged).

**Offboarding:** `DELETE /api/store-memory/<merchant_id>` (same internal auth
chain; path merchant must equal the bound `X-Merchant-Id` **and** the
body-bound merchant per §4.2). Writes the tombstone first, then deletes all
generations **enumerated from Zep by prefix** `merchant_<hex32>` (never from
Redis), then ledger cleanup; responds 202 with a redaction-status payload the
engine's state machine (#193) polls/re-drives until verified-empty.
Idempotent (repeat ⇒ re-verify ⇒ same terminal state; Zep 404s treated as
success — same V-class check as #72's V-1).

## 8. Config

| Var | Default | Notes |
|---|---|---|
| `STORE_MEMORY_ENABLED` | `false` | Global kill switch, read live per the `Config.validate` convention |
| `STORE_MEMORY_MERCHANT_ALLOWLIST` | empty | Comma-separated merchant UUIDs; empty = no one (Phase 1-3 gate) |
| `STORE_MEMORY_RETENTION_DAYS` | `180` | Floor 30 |
| `STORE_MEMORY_MAX_EPISODES` | `20000` | Per graph, enforced at rebuild |
| `STORE_MEMORY_REBUILD_EVERY_DAYS` | `30` | Rolling rebuild cadence, jittered |
| `STORE_MEMORY_WORKING_SET_MAX` | `30` | Entity cap (D3) |
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
5. **Scan ban (revision 2):** any `get_all_nodes`/`get_all_edges`/
   `panorama_search` against a `merchant_*` graph id raises
   `StoreGraphScanBlocked`; ReportAgent's treated-run toolset excludes
   `panorama_search`; the fake client asserts zero list-all calls across a
   full treated-run pipeline test.
6. Working set: caps enforced; cart anchors always present; empty store
   graph ⇒ degenerates to cart-only.
7. Outcomes endpoint: auth chain (reuse `test_api_auth` /
   `test_cart_recovery_hmac` fixtures); **body-merchant vs header mismatch ⇒
   403** (revision 2); idempotent replay within TTL; 503 releases the slot,
   post-add ambiguity does not; malformed-body 400 with no PII in logs.
8. Rebuild: pointer flip atomicity; old-generation grace; shopper-exclusion
   (customer erasure) drops exactly that `anonymous_id`'s episodes; cap
   enforcement; "oldest episode age" metric emitted.
9. Offboarding: prefix-enumeration covers stale generations; verify pass
   catches a straggler-recreated graph; idempotency.
10. #72 interlock: sweeper regex + registry `graph_kind` assertions reject
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
