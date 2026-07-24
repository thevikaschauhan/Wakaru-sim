# PRD — Delete per-cart Zep graphs and sweep historical orphans

**Issue:** [#72](https://github.com/thevikaschauhan/Wakaru-sim/issues/72) (P1)
**Related:** #61 (per-store persistent memory, separate spec), #24 (multi-tenancy), #25 (persistence/retention)
**Companion docs:** [TDD](./TDD.md), [SCHEMA](./SCHEMA.md)
**Status:** Proposed (revision 2)
**Verified against:** `origin/main` @ `a3d7a1a`, `zep-cloud==3.13.0` (SDK source at tag `v3.13.0`)

> **Revision 2 (2026-07-21), after external design review:** deletion eligibility
> is now **fail-closed** on unknown graph age; the orphan-age metric is derived
> from the Zep scan itself, not Redis; the scratch registry is structurally
> separated from #61's store-graph registry; dry-run has one unambiguous
> parser rule; the offboarding acceptance criterion is narrowed for pre-ledger
> orphans; the scheduler contract is redesigned (see TDD §3.4).
>
> **Revision 3 (2026-07-21), after review-round 2:** chain liveness no longer
> depends on the chain itself — every sweep occurrence checks in with a
> **Sentry Cron Monitor**, whose missed-check-in alert fires from Sentry's
> infrastructure regardless of whether the cause is a dead chain, starvation,
> or a wedged worker (TDD §3.4); an RQ failure callback re-seeds the chain
> when a work horse dies. Lock release is atomic-only (Lua compare-and-delete).
> The sweep uses a bounded candidate heap with streamed metrics. The
> shared-worker latency trade is now an explicit, signed-off product
> acceptance (§3) instead of a contradiction with the non-goals.

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
   offboarding (including Shopify's mandatory `shop/redact` erasure flow,
   which reaches Wakaru via the engine — engine-side implementation tracked as
   vakaru-engine#193) cannot enumerate or delete them.

## 2. Goals

- G1: Every analysis's Zep graph is deleted after the insight is extracted, on
  both success and failure paths.
- G2: Deletion is guaranteed even across worker crashes, restarts, deploys, and
  transient Zep outages, without silent orphaning.
- G3: All historical `mirofish_*` orphans are inventoried and deleted under a
  dry-run-first runbook.
- G4: Any graph's merchant attribution, creation time, and deletion status are
  recorded and queryable for offboarding and audit (post-ledger graphs; see
  §6 for the pre-ledger narrowing).
- G5: An orphan-age signal exists, is computed from the vendor's own listing
  (not from local state that can be lost), and alerts when deletion stops
  working.

## 3. Non-goals

- Per-store persistent memory (Model B). That is #61; this spec deliberately
  keeps the throwaway-graph model and only fixes its hygiene.
- Any change to the analysis pipeline's inputs or outputs. **Timing carve-out
  (revision 3, explicit product acceptance):** the maintenance queue shares
  the RQ worker, so a queued paid analysis can wait out one in-flight sweep —
  bounded by the sweep's 300 s job timeout (TDD §3.4). This ≤ 5-minute
  worst-case addition to an 8-17-minute job is accepted in exchange for not
  provisioning a dedicated worker service; the dedicated worker is the
  documented scale-out if the bound is hit in practice. Sweep starvation
  under sustained load is alerted externally (FR-5's heartbeat), not assumed
  away.
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
A periodic job lists all graphs in the Zep account and deletes every graph
that satisfies **both** eligibility conditions in FR-3. This single mechanism
is simultaneously: the retry path for failed inline deletes, the
crash-recovery path (state lives in Zep itself, not in any local queue), and
the historical-orphan sweep (G3). Each run is bounded (delete-count cap and
timeout) so a large backlog is drained across cycles rather than in one
unbounded pass.

> **Deviation from the issue text, owned explicitly:** #72 asks for a "durable
> deletion outbox/retry job". An outbox requires durable local storage Wakaru
> does not have (no SQL DB; Redis on Railway is not a durability guarantee).
> The sweeper achieves the same property with *Zep itself as the outbox*: an
> undeleted graph is, by definition, still listed by `graph.list_all` and will
> be retried on the next sweep. This is strictly more robust than a local
> outbox, which could lose records the graphs would then outlive.

### FR-3 — Deletion eligibility and safety invariants
A graph is deletion-eligible only when **all** of the following hold:

1. **Identity:** its id matches `^mirofish_[0-9a-f]{16}$` exactly (the format
   `create_graph` produces). Future `merchant_*` graphs (#61) and any
   manually created graph are structurally untouchable.
2. **Proven age:** its age exceeds the TTL (default **24 h**), established by
   a **parseable vendor `created_at`**, or — when the vendor timestamp is
   missing/unparseable — corroborated by the lifecycle ledger's own
   `created_at` for that graph id. A graph whose age cannot be proven by
   either source is **skipped and alerted, never deleted** (fail-closed;
   revision 2 — a vendor timestamp-format regression must degrade to loud
   retention, not mass deletion of in-flight graphs).

Additional invariants:

- The TTL floor is enforced in code at **6 h**, far above the maximum possible
  in-flight run (`job_timeout` ceiling 5400 s ≈ 1.5 h), so a live run's graph
  can never be swept mid-pipeline.
- **Dry-run semantics (one rule):** deletion is enabled **only** when
  `ZEP_SWEEP_DRY_RUN` is the exact literal `false` (case-insensitive, after
  trim). Absent, empty, invalid, or any other value ⇒ dry run. The first
  production sweep therefore runs dry by default.
- Unknown-age graphs that persist (e.g. pre-ledger orphans with unparseable
  vendor timestamps) are deleted only through the operator-approved runbook
  path (TDD §8), never automatically.

### FR-4 — Lifecycle ledger (scratch registry)
At graph creation, record `{graph_id, graph_kind=scratch, merchant_id,
created_at}`; at deletion, record `{deleted_at, delete_source: inline|sweep}`.
The ledger is Redis-backed (SCHEMA.md) and serves **observability,
corroboration (FR-3.2), and offboarding enumeration** — sweep correctness
never depends on it (FR-2 reads Zep, not the ledger).

**Registry separation (revision 2):** the scratch registry
(`zep:scratch:active`) holds throwaway graphs only. #61's long-lived
`merchant_*` graphs use their own registry keys and are **forbidden** from the
scratch registry — structurally (distinct writer functions asserting
`graph_kind`), not by convention — so a healthy store graph can never trip the
orphan alert.

### FR-5 — Orphan-age metric, alert, and liveness heartbeat
Each sweep emits `zep_sweep` structured log lines (scanned, matched, deleted,
skipped_dry_run, skipped_unknown_age, failed, oldest_scratch_age_seconds) and
reports to Sentry when (a) the sweep itself fails, (b) any per-graph deletion
failed, (c) `skipped_unknown_age > 0`, or (d) `oldest_scratch_age_seconds >
2 × TTL`. **`oldest_scratch_age_seconds` is computed from the Zep listing in
the same pass** (minimum parseable `created_at` among matched graphs), so the
alert survives Redis loss; the Redis registry is a secondary diagnostic the
sweeper reconciles.

**Liveness (revision 3):** the alerts above are emitted *by* the sweep, so
they cannot signal the sweep's own death. Every occurrence therefore checks
in with a **Sentry Cron Monitor** (schedule = the sweep interval + grace);
a missed check-in alert fires from Sentry's infrastructure — independent of
the worker, the chain, and Redis — covering dead chains, starvation, and
wedged workers with one signal. No sweep property is allowed to be attested
only by the sweep itself.

## 5. Retention policy (G5 documentation requirement)

| Data | Location | Retention |
|---|---|---|
| Cart-derived graph (entities, episodes) | Zep Cloud | Deleted at end of analysis; hard ceiling = sweep TTL (24 h default), except unknown-age graphs which are retained-and-alerted until the runbook disposes of them |
| Local project/sim/report scratch | Railway container disk | Deleted at end of analysis (existing #24 CP2b behavior) |
| Lifecycle ledger entry | Redis | 30 days after deletion, then expires |
| Insight (the paid output) | Returned to engine; Wakaru keeps job result in Redis | 24 h (existing `RESULT_TTL_SECONDS`) |

Vendor data-processing note: with this spec live, Zep holds shopper-derived
data for at most `ZEP_GRAPH_TTL_HOURS` in the normal path. The operator item
"confirm Zep DPA / data-deletion SLA covers this window" is tracked in the
rollout checklist (TDD §8) — a documentation task, not a code dependency.

## 6. Acceptance criteria (mapped to #72)

| #72 criterion | Met by |
|---|---|
| Successful or failed analysis eventually deletes its graph | FR-1 (fast) + FR-2 (guarantee); unknown-age exception is alerted, not silent |
| Retries survive worker restarts; orphan-age metric/alert | FR-2 (state in Zep) + FR-5 (metric from Zep scan) |
| Historical orphans inventoried and deleted under approved runbook | FR-2 + FR-3 dry-run + TDD §8 runbook |
| Offboarding can enumerate and delete every merchant graph | FR-4 ledger index — **narrowed (revision 2): graphs created after the ledger deploys.** Pre-ledger orphans carry no merchant attribution anywhere (the graph name embeds none) and are handled by the global age sweep instead; per-merchant enumeration for them is impossible retroactively, and the ≤ TTL lifetime makes the live set near-empty |
| Retention period and vendor controls documented | §5 |

## 7. Success metrics

- Zep account graph `total_count` (verified field on `GraphListResponse`)
  trends to ≈ number of in-flight analyses within 48 h of the historical
  sweep, and stays flat.
- `oldest_scratch_age_seconds` (Zep-derived) stays < TTL in steady state.
- `skipped_unknown_age` is 0 in steady state; any nonzero value alerts.
- Zero sweeps of non-`mirofish_` graphs (assert-level invariant, tested).

## 8. Rollout

Single deploy of web + worker (same image), no new Railway service, no new
operator-provisioned infrastructure. Feature order: ledger writes → inline
delete → sweeper in dry-run → operator reviews one dry sweep's counts
(including the unknown-age inventory) → set `ZEP_SWEEP_DRY_RUN=false`.
Scheduler-correctness tests run against real Redis with the pinned RQ version
before merge (TDD §6). Detail in TDD §8.
