# PRD — Delete per-cart Zep graphs and sweep historical orphans

**Issue:** [#72](https://github.com/thevikaschauhan/Wakaru-sim/issues/72) (P1)
**Related:** #61 (per-store persistent memory, separate spec), #24 (multi-tenancy), #25 (persistence/retention)
**Companion docs:** [TDD](./TDD.md), [SCHEMA](./SCHEMA.md)
**Status:** Proposed
**Verified against:** `origin/main` @ `a3d7a1a`, `zep-cloud==3.13.0` (SDK source at tag `v3.13.0`)

## 1. Problem

Every cart-recovery analysis creates a throwaway Zep Cloud graph named
`mirofish_<16 hex>` (`backend/app/services/graph_builder.py:126`). The
post-run cleanup (`_cleanup_artifacts`,
`backend/app/services/cart_recovery_workflow.py:241-275`) removes only the
local project/simulation/report scratch directories. `delete_graph`
(`graph_builder.py:434-436`) exists but has **zero callers** in the repo.

Consequences, in priority order:

1. **PII retention.** Shopper-derived cart data (products, prices, behavioral
   signals) remains at rest in a third-party vendor indefinitely, violating the
   transient-scratch intent of #24 CP2b.
2. **Cost.** Graph count in the single Zep account grows by one per paid
   analysis, forever.
3. **Compliance.** Orphaned graphs carry no merchant association, so merchant
   offboarding (including Shopify's mandatory `shop/redact` erasure flow, which
   reaches Wakaru via the engine) cannot enumerate or delete them.

## 2. Goals

- G1: Every analysis's Zep graph is deleted after the insight is extracted, on
  both success and failure paths.
- G2: Deletion is guaranteed even across worker crashes, restarts, deploys, and
  transient Zep outages, without silent orphaning.
- G3: All historical `mirofish_*` orphans are inventoried and deleted under a
  dry-run-first runbook.
- G4: Any graph's merchant attribution, creation time, and deletion status are
  recorded and queryable for offboarding and audit.
- G5: An orphan-age signal exists and alerts when deletion stops working.

## 3. Non-goals

- Per-store persistent memory (Model B). That is #61; this spec deliberately
  keeps the throwaway-graph model and only fixes its hygiene.
- Any change to the analysis pipeline's inputs, outputs, or timing.
- Introducing a SQL database. Wakaru's stack is Flask + Redis/RQ + local
  scratch (verified: no SQL dependency in `backend/requirements.txt`); this
  spec stays inside that stack. See SCHEMA.md §1 for the rationale and the
  explicit durability boundary.

## 4. Product requirements

### FR-1 — Inline deletion (fast path)
After an analysis completes (success or failure), the run's Zep graph is
deleted in the same `finally` cleanup that removes local scratch. Failure to
delete must never mask the analysis result or its propagating exception
(same guarded-best-effort contract the existing cleanup uses).

### FR-2 — Sweeper (guarantee path)
A periodic job lists all graphs in the Zep account, and deletes every graph
matching `^mirofish_[0-9a-f]{16}$` whose `created_at` is older than a
configurable TTL (default **24 h**). This single mechanism is simultaneously:
the retry path for failed inline deletes, the crash-recovery path (state lives
in Zep itself, not in any local queue), and the historical-orphan sweep (G3).

> **Deviation from the issue text, owned explicitly:** #72 asks for a "durable
> deletion outbox/retry job". An outbox requires durable local storage Wakaru
> does not have (no SQL DB; Redis on Railway is not a durability guarantee).
> The sweeper achieves the same property with *Zep itself as the outbox*: an
> undeleted graph is, by definition, still listed by `graph.list_all` and will
> be retried on the next sweep. This is strictly more robust than a local
> outbox, which could lose records the graphs would then outlive.

### FR-3 — Safety invariants
- The sweeper deletes **only** ids matching `^mirofish_[0-9a-f]{16}$` (the
  exact format `create_graph` produces). Future `merchant_*` graphs (#61) and
  any manually created graph are structurally untouchable.
- The TTL floor is enforced in code at **6 h**, far above the maximum possible
  in-flight run (`job_timeout` ceiling 5400 s ≈ 1.5 h), so a live run's graph
  can never be swept mid-pipeline.
- Dry-run mode (`ZEP_SWEEP_DRY_RUN=true`) logs what would be deleted without
  deleting; the first production sweep runs dry (see Runbook, TDD §8).

### FR-4 — Lifecycle ledger
At graph creation, record `{graph_id, merchant_id, created_at}`; at deletion,
record `{deleted_at, delete_source: inline|sweep}`. The ledger is Redis-backed
(SCHEMA.md) and serves **observability and offboarding enumeration only** —
sweep correctness never depends on it (FR-2 reads Zep, not the ledger).
Merchant offboarding enumerates `zep:merchant:<merchant_id>:graphs`.

### FR-5 — Orphan-age metric and alert
Each sweep emits `zep_sweep` structured log lines (scanned, matched, deleted,
skipped, failed, oldest_active_age_seconds) and reports to Sentry when
(a) the sweep itself fails, or (b) `oldest_active_age_seconds > 2 × TTL`
(deletion has been failing for at least one full extra cycle).

## 5. Retention policy (G5 documentation requirement)

| Data | Location | Retention |
|---|---|---|
| Cart-derived graph (entities, episodes) | Zep Cloud | Deleted at end of analysis; hard ceiling = sweep TTL (24 h default) |
| Local project/sim/report scratch | Railway container disk | Deleted at end of analysis (existing #24 CP2b behavior) |
| Lifecycle ledger entry | Redis | 30 days after deletion, then expires |
| Insight (the paid output) | Returned to engine; Wakaru keeps job result in Redis | 24 h (existing `RESULT_TTL_SECONDS`) |

Vendor data-processing note: with this spec live, Zep holds shopper-derived
data for at most `ZEP_GRAPH_TTL_HOURS`. The operator item "confirm Zep DPA /
data-deletion SLA covers this window" is tracked in the rollout checklist
(TDD §8) — it is a documentation task, not a code dependency.

## 6. Acceptance criteria (mapped to #72)

| #72 criterion | Met by |
|---|---|
| Successful or failed analysis eventually deletes its graph | FR-1 (fast) + FR-2 (guarantee) |
| Retries survive worker restarts; orphan-age metric/alert | FR-2 (state in Zep) + FR-5 |
| Historical orphans inventoried and deleted under approved runbook | FR-2 + FR-3 dry-run + TDD §8 runbook |
| Offboarding can enumerate and delete every merchant graph | FR-4 ledger index (and ≤ TTL lifetime makes the set near-empty) |
| Retention period and vendor controls documented | §5 |

## 7. Success metrics

- Zep account graph `total_count` (returned by `graph.list_all`, verified
  field on `GraphListResponse`) trends to ≈ number of in-flight analyses
  within 48 h of the historical sweep, and stays flat.
- `oldest_active_age_seconds` stays < TTL in steady state.
- Zero sweeps of non-`mirofish_` graphs (assert-level invariant, tested).

## 8. Rollout

Single deploy of web + worker (same image), no new Railway service, no new
operator-provisioned infrastructure. Feature order: ledger writes → inline
delete → sweeper in dry-run → operator reviews one dry sweep's counts →
`ZEP_SWEEP_DRY_RUN=false`. Detail in TDD §8.
