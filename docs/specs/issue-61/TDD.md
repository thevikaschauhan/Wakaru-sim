# TDD — Per-store persistent Zep graph with outcome feedback

**Issue:** [#61](https://github.com/thevikaschauhan/Wakaru-sim/issues/61)
**Companion docs:** [PRD](./PRD.md), [SCHEMA](./SCHEMA.md)
**Verified against:** Wakaru `origin/main` @ `a3d7a1a`; `zep-cloud==3.13.0` SDK source at tag `v3.13.0`; engine migrations `001,033,034,036,040`

## 1. Verified API and codebase facts this design uses

`zep-cloud` 3.13.0 (SDK source at tag `v3.13.0`):

- `graph.create(graph_id, name, description)`; `graph.get(graph_id)`;
  `graph.delete(graph_id)`.
- `graph.add(data, type, graph_id, created_at: str | None)` — **per-episode
  `created_at` override exists**; used to stamp episodes with the real
  abandonment/outcome time, keeping Zep's temporal model truthful.
- `graph.add_batch(episodes, graph_id)` (already used, `graph_builder.py:256`).
- `graph.search(query, graph_id, limit, min_score, reranker, center_node_uuid,
  bfs_origin_node_uuids, ...)` — the scoped-retrieval primitive for D3.
- `graph.set_ontology(graph_ids=[...])` external client (already used,
  `graph_builder.py:219-223`) — per-graph ontology assignment.
- `graph.episode.get_by_graph_id(graph_id, ...)`, `episode.get(uuid_)`,
  `episode.delete(uuid_)` — the pruning primitives (D4).
- `graph.clone(source_graph_id, target_graph_id)` — used only by the rebuild
  fallback (V-1).

Wakaru:

- `merchant_id`: canonical lowercase UUID, request-bound (`app/__init__.py:236-252`),
  in job meta on the async path; sentinel `00000000-0000-0000-0000-000000000000`
  (`paths.py:49`).
- Whole-graph read seam to replace for D3:
  `SimulationManager.prepare_simulation` → `ZepEntityReader`
  (`simulation_manager.py:419`) → `fetch_all_nodes/fetch_all_edges`
  (`zep_entity_reader.py:139`).
- Report seam: `ReportAgent(graph_id=...)` (`cart_recovery_workflow.py:335-339`).
- Auth stack for the new endpoint: X-API-Key (#10) + HMAC body signature (#11)
  + `X-Merchant-Id` binding (#24) — all existing middleware.

Engine (outcome source of truth):

- `orders` (migration 033) carries `matched_anonymous_id` — the
  abandonment→purchase attribution join key already exists.
- `abandonment_detections` (034) PK
  `(shopify_store_id, anonymous_id, episode_type, checkout_started_at)` with
  `episode_type ∈ {checkout, cart}` (040) — the natural idempotency key for
  outcome events.
- `036_orders_inkwell_forwarding` — the engine already has a
  forwarding-outbox pattern to copy for Wakaru outcome delivery.
- **No stored `recovered` flag exists** (verified by grep across migrations):
  outcome is *derived* — an episode is `recovered` when a matching order lands
  inside the attribution window, else `expired` when the window closes.

## 2. Graph identity and provisioning (D7, D9)

- `graph_id = "merchant_" + merchant_id.replace("-", "")` — deterministic,
  41 chars, charset `[a-z0-9_]` (same alphabet as the proven `mirofish_*`
  ids, sidestepping any unverified Zep id-charset constraint on `-`).
  Deterministic naming means no lookup table is needed to *find* a graph;
  the ledger (SCHEMA §3) exists for enumeration, version stamps, and audit.
- Provisioning is lazy and idempotent, in `store_memory.py` (new):

```python
def ensure_store_graph(client, merchant_id) -> str:
    graph_id = store_graph_id(merchant_id)          # pure function above
    try:
        client.graph.get(graph_id)                  # fast path: exists
        return graph_id
    except NotFoundError:                            # V-2 pins exact class
        pass
    try:
        client.graph.create(graph_id=graph_id, name=f"Store memory", description=ONTOLOGY_VERSION)
        apply_fixed_ontology(client, graph_id)       # set_ontology(graph_ids=[graph_id])
        ledger_record_created(graph_id, merchant_id, ONTOLOGY_VERSION)
    except AlreadyExistsError:                       # lost the race: fine
        pass
    return graph_id
```

  The get→create→get pattern makes the concurrent-first-abandonment race
  harmless without locks (D9); both racers converge on the same id. Sentinel
  merchant short-circuits before all of this (PRD §6).

## 3. Fixed ontology (D1)

`backend/app/services/store_ontology.py` (new) defines
`CART_RECOVERY_ONTOLOGY_V1` in the exact `{entity_types, edge_types}` shape
`set_ontology` already consumes (`graph_builder.py:136-223`), with the types in
SCHEMA §2. `ONTOLOGY_VERSION = "cr-v1"`.

- Phase 0 swaps `OntologyGenerator().generate(...)` out of the analysis path
  (`cart_recovery_workflow.py:118-122`) for the fixed ontology; the
  per-event LLM ontology call is deleted, not flagged (its output was never
  reviewed by anyone and varies per cart — the A/B in PRD Phase 0 validates
  insight quality holds).
- Evolution rule: additive only (new types/attributes ⇒ `cr-v2`). A version
  bump triggers `set_ontology` re-application to existing graphs at their next
  write (version read from ledger, applied lazily, stamped after success).
  Renames/removals require an explicit rebuild via `graph.clone` + replay
  (V-1 fallback machinery), which is why they are banned in normal evolution.

## 4. Write path (Phases 1-2)

### 4.1 Cart episodes (Phase 1, dual-write)

In `_run_analysis`, after the throwaway build (unchanged), when the merchant is
allowlisted:

- `ensure_store_graph(...)`, then `graph.add(graph_id=store_graph, type="json",
  data=cart_episode_json, created_at=<abandonment ISO time>)` — one episode per
  analysis, the **structured** cart summary (SCHEMA §4.1), not the prose seed
  doc. JSON episodes keep entity extraction anchored to the fixed ontology and
  make later pruning/audit tractable.
- Failure is guarded: a store-memory write error logs a warning and never
  fails the paid analysis (same contract as ledger writes in #72).
- No processed-wait on this write: nothing in the same run reads the store
  graph in Phase 1, so the pipeline's latency is untouched.

### 4.2 Outcome episodes (Phase 2, Track O)

New blueprint `backend/app/api/store_memory.py`:

- `POST /api/store-memory/outcomes` — internal, engine-only, behind the
  existing X-API-Key + HMAC + `X-Merchant-Id` middleware chain (no new auth
  code). Body: SCHEMA §4.2. Idempotency: `Idempotency-Key` header required,
  scope `outcomes:<merchant_id>`, reusing `idempotency.py` verbatim.
- Handler: `ensure_store_graph` → `graph.add(type="json",
  created_at=<outcome time>)` → 202. Engine-side delivery uses an outbox
  copied from the `036` inkwell-forwarding pattern (engine change, filed as a
  separate engine issue; the contract in SCHEMA §4.2 is the interface both
  sides build to).
- Outcome derivation stays in the engine (it owns orders + detections):
  `recovered` = order with `matched_anonymous_id = episode.anonymous_id` and
  `shopify_store_id` matching within `ATTRIBUTION_WINDOW_DAYS` (default 7) of
  the recovery email send; `expired` = window closed without a match.

## 5. Read path (Phase 3) — the D3 dilution defense

The store graph is never handed to `fetch_all_nodes` in the analysis path.
Instead, `store_memory.build_working_set(client, store_graph_id, cart)` returns
a bounded `WorkingSet`:

1. **Cart-anchored retrieval:** for each of the cart's salient strings
   (product titles, collection, price band, `ontology_code` candidates),
   `graph.search(graph_id=store_graph, query=<string>, limit=10,
   min_score=<tuned>)`; union, dedupe by node/edge uuid.
   Cap: ≤ 30 entities, ≤ 60 edges (`STORE_MEMORY_WORKING_SET_MAX`).
2. **Outcome priors:** `graph.search(query="recovery outcome", ...)` filtered
   to `RecoveryOutcome`-typed results, reduced in code to an aggregate
   `StorePriors` (recovery rate by angle, discount-effectiveness, repeat-
   abandoner flag for this `anonymous_id`) — a small dict, not graph objects.
3. **Injection points:**
   - Persona generation: the working set's entities are passed where the
     throwaway graph's `ZepEntityReader` output went
     (`simulation_manager.py:419` seam gains an optional
     `entities_override`); the cart's own entities always dominate (they are
     the anchor set), historical entities enrich.
   - Report agent: `ReportAgent` keeps `graph_id=store_graph` (its tool-based
     reads are query-scoped already) and receives `StorePriors` as additional
     context in its requirement string.
4. **Treated runs skip the throwaway graph entirely** (create/build/delete all
   gone); the seed episodes go to the store graph (4.1) *before* simulation,
   with a bounded processed-wait on just that run's episode uuids (the
   existing `_wait_for_episodes` machinery, `graph_builder.py:278`, takes
   explicit uuid lists — reused unchanged).

Rollback per merchant = removal from allowlist ⇒ next run takes the throwaway
path again. No data migration in either direction.

## 6. Concurrency (D9)

- Provisioning race: §2, lock-free idempotent converge.
- Same-store concurrent episode adds: Zep ingests episodes per graph
  asynchronously; batches from two runs interleave safely (order is by
  `created_at`, which we stamp). The only same-run ordering requirement is
  "this run's episodes processed before this run's search", enforced by the
  uuid-scoped `_wait_for_episodes`.
- Prune vs analysis race: prune deletes only episodes older than 180 d;
  the working set is dominated by recent episodes; a prune running during an
  analysis can at worst remove months-old context mid-retrieval — harmless
  and bounded.

## 7. Retention, pruning, offboarding (D4, PRD §4)

- **Prune job** on the #72 `maintenance` queue/scheduler (same worker, same
  boot-reconciler pattern, deterministic `job_id="store-memory-prune"`, daily):
  for each ledger-listed store graph, page
  `episode.get_by_graph_id(graph_id)`, `episode.delete(uuid_)` where
  `occurred_at < now − 180 d`, and enforce the 20k cap oldest-first. Stats
  logged + Sentry on failures, mirroring the sweep job.
- **V-1 (staging, blocks Phase 1 exit):** verify Zep's semantics for
  nodes/edges derived from a deleted episode (expected: derived artifacts are
  removed/invalidated; **not assumed**). If deletion does not fully reclaim
  derived data, the fallback is the rebuild path: quarterly
  `graph.clone`-free rebuild — create `merchant_<hex>_r<N>`, replay the
  retained episodes (they are structured JSON, SCHEMA §4), flip the ledger
  pointer, delete the old graph. The ledger's `graph_id` indirection exists
  precisely so the deterministic name can be superseded by a rebuild
  generation.
- **Offboarding:** `DELETE /api/store-memory/<merchant_id>` (same internal
  auth chain; merchant path segment must equal the bound `X-Merchant-Id`).
  Deletes: store graph (`graph.delete`), ledger hash, merchant index entry.
  Engine calls it from its `shop/redact` handling. Idempotent (404 from Zep
  treated as success — same V-class verification as #72's V-1).

## 8. Config

| Var | Default | Notes |
|---|---|---|
| `STORE_MEMORY_ENABLED` | `false` | Global kill switch, read live per the `Config.validate` convention |
| `STORE_MEMORY_MERCHANT_ALLOWLIST` | empty | Comma-separated merchant UUIDs; empty = no one (Phase 1-3 gate) |
| `STORE_MEMORY_RETENTION_DAYS` | `180` | Floor 30 |
| `STORE_MEMORY_MAX_EPISODES` | `20000` | Per graph |
| `STORE_MEMORY_WORKING_SET_MAX` | `30` | Entity cap (D3) |
| `ATTRIBUTION_WINDOW_DAYS` | `7` | Engine-side, in the outcome contract |

## 9. Testing plan

Unit (fake Zep client, existing patterns):

1. `store_graph_id` determinism + sentinel refusal.
2. `ensure_store_graph` — exists / not-exists / create-race paths; ontology
   applied exactly once per version.
3. Dual-write guard: store-memory write failure does not fail the analysis
   (mirror of `test_cart_recovery_cleanup` masking tests).
4. Working set: caps enforced; cart anchors always present; empty store graph
   ⇒ degenerates to cart-only (byte-equivalent behavior to throwaway inputs).
5. Outcomes endpoint: auth chain (reuse `test_api_auth` / `test_cart_recovery_hmac`
   fixtures), idempotent replay, merchant-mismatch 403, malformed-body 400
   with no PII in logs (reuse `test_cart_recovery_pii` patterns).
6. Prune: age + cap eviction ordering; per-graph failure isolation.
7. Offboarding: full deletion set; idempotency.
8. #72 interlock: sweeper regex test extended with `merchant_<32hex>` ⇒ never
   swept (already specified there, re-asserted here).

Integration (staging, gated `ZEP_API_KEY`, follows the repo's
env-gated-test convention): one real end-to-end pilot-merchant run per phase
exit — Phase 1 (episodes visible via `episode.get_by_graph_id`), Phase 2
(outcome episode lands + idempotent replay), Phase 3 (working set non-empty,
insight produced, latency delta < +10%).

## 10. Open verification items (all block the phase that uses them, none assumed)

- **V-1** (blocks Phase 1 exit): episode-deletion semantics for derived
  nodes/edges (§7).
- **V-2** (blocks Phase 1): exact SDK exception classes for get-miss /
  create-conflict (pins the `except` clauses in §2).
- **V-3** (blocks Phase 3): `graph.search` result-shape and score behavior on
  JSON-episode-derived entities at our data shape; tunes `min_score`.
- **V-4** (blocks Phase 3 default-on): Zep plan graph-count/size headroom at
  full merchant count (operator, PRD D6).
