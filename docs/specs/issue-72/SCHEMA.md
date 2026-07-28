# Backend schema — graph lifecycle ledger (issue #72)

**Issue:** [#72](https://github.com/thevikaschauhan/Wakaru-sim/issues/72)
**Companion docs:** [PRD](./PRD.md), [TDD](./TDD.md)

> **Revision 2 (2026-07-21):** the active-graph registry is renamed
> `zep:scratch:active` and is structurally scratch-only (`graph_kind` field +
> writer-side assertion), so #61's long-lived `merchant_*` graphs can never
> enter it or trip its alert. The registry is demoted from metric source to
> secondary diagnostic (the orphan-age metric is Zep-scan-derived, TDD §3.2),
> and the ledger's `created_at` gains a corroboration role for fail-closed
> deletion eligibility. Scheduling keys for the revision-2 scheduler added
> (§2.4).

## 1. Why Redis, and the durability boundary

Wakaru has no SQL database (verified: `backend/requirements.txt` /
`backend/pyproject.toml` carry no DB driver; state = Redis + local scratch).
Adding Postgres for one stopgap table contradicts the stopgap's purpose, and
SQLite on Railway is ephemeral across deploys.

The ledger therefore lives in the **existing Redis** (same instance as RQ +
idempotency, `job_queue.get_redis_connection()`), under one explicit
invariant, stated here and enforced in code:

> **Correctness never depends on the ledger.** Sweep decisions read Zep's own
> `graph.list_all`; the orphan-age metric is computed from that same scan.
> The ledger's roles are: merchant attribution/enumeration, age
> **corroboration** for graphs whose vendor timestamp is unparseable
> (fail-closed rule, TDD §3.2 — the ledger can only make deletion *possible*
> for an already-prefix-matched graph, never *prevent* a wrongly-scoped one),
> and diagnostics. If Redis is flushed, the only losses are attribution
> history and the corroboration source — deletions of parseable-age graphs
> continue, unknown-age graphs are retained-and-alerted, and the scheduler
> chain is re-seeded by the boot reconciler. Every ledger write is wrapped so
> Redis unavailability degrades to a warning, never a failed analysis or a
> failed sweep.

All keys use the `zep:` prefix to stay clear of the existing `idem:` and
RQ (`rq:*`) namespaces.

## 2. Key schema

### 2.1 `zep:graph:<graph_id>` — Hash (one per graph, the record of truth for attribution)

| Field | Type/format | Written by | Semantics |
|---|---|---|---|
| `graph_kind` | literal `scratch` (this spec) or `store` (#61) | `record_created` / #61's `store_record_created` | Lifecycle class. **Revision 2:** #72's `record_created` writes `scratch` and asserts the id matches `^mirofish_[0-9a-f]{16}$`; #61's writer writes `store` and asserts `merchant_*`. Mixed writes are a programming error, not a data state. |
| `merchant_id` | canonical lowercase UUID string (per `validate_merchant_id`, `paths.py:52`); sentinel `00000000-0000-0000-0000-000000000000` for legacy engine calls | `record_created` | Tenant attribution for offboarding/audit |
| `created_at` | Unix epoch seconds, integer string | `record_created` | Graph creation time (local clock). Roles: corroboration source for fail-closed deletion eligibility when the vendor timestamp is unparseable (TDD §3.2); diagnostics. Zep's `created_at` remains the primary age source. **Corroboration reads (`created_at_for`) return None when the record is tombstoned (`deleted_at` set) — a dead incarnation must not age a re-minted id (revision-6 F10) — or when the value is ≤0 / in the future (impossible; revision-5 F11). `record_created` clears `deleted_at`/`delete_source` on re-incarnation.** |
| `deleted_at` | Unix epoch seconds, integer string; absent while alive | `record_deleted` | Deletion time |
| `delete_source` | `inline` \| `sweep`; absent while alive | `record_deleted` | Which path deleted it (metric: sweep-share ≈ inline-failure rate) |

TTL: none while alive; `EXPIRE 2592000` (30 days) set by `record_deleted`.
A graph deleted by the sweeper that never had a `record_created` (pre-#72
historical orphan, or created during a Redis outage) gets a hash written at
deletion time with `merchant_id` absent — explicitly distinguishable from the
sentinel.

### 2.2 `zep:scratch:active` — Sorted set (scratch registry; secondary diagnostic)

- **Member:** `graph_id` (scratch graphs only, enforced by the writer's
  `graph_kind` assertion); **Score:** creation epoch seconds.
- `record_created` → `ZADD`; `record_deleted` → `ZREM`.
- **Revision 2 — demoted from metric source:** the alertable
  `oldest_scratch_age_seconds` comes from the sweeper's Zep scan (TDD §3.2),
  which survives Redis loss. This zset is a diagnostic (e.g. "what does the
  ledger *think* is alive") and a **bidirectional** drift detector (revision-6
  F7): each sweep reports drift in both directions. `ledger_drift` counts
  ledger-only entries (in the zset, absent from the listing) — but each is
  **closed only after an authoritative per-graph `graph.get` 404 confirms it is
  genuinely gone (revision-7 F1)**, then closed lifecycle-aware (`record_deleted`:
  stamp `deleted_at` + 30-day TTL, `ZREM`, `SREM` — not a bare `ZREM` that would
  strand the hash and merchant membership, revision-6 F9). A stale-looking entry
  that `graph.get` shows still EXISTS (the listing dropped a page / Zep
  under-reported `total_count`) or cannot confirm is KEPT and counted
  `reconcile_unconfirmed` — the r6 completeness gate (`distinct_ids_seen >=
  total_count`) that could close a live entry under an under-count is removed
  (revision-7 F1), which also drops the O(all-listed-ids) set (revision-7 F6 —
  scan memory is now O(distinct scratch graphs)). `zep_only_unattributed` counts
  matched scratch graphs present in Zep but absent from the ledger snapshot.
  Under **dry-run** the sweep confirms/reconciles nothing (pure inventory,
  revision-6 F9).
- #61's `merchant_*` graphs never appear here (writer assertion + sweeper
  regex are independent guards).

### 2.3 `zep:merchant:<merchant_id>:graphs` — Set (offboarding enumeration, PRD FR-4)

- **Members:** `graph_id`s created for this merchant and not yet deleted
  (scratch graphs from this spec; #61's store graphs join the same set so
  offboarding remains one enumeration).
- `record_created` → `SADD`; `record_deleted` → `SREM`.
- Offboarding procedure: `SMEMBERS` → `graph.delete` each → confirm empty.
  In the throwaway model this set is empty in steady state (graphs live
  minutes). Scope note (PRD §6): this index enumerates graphs created after
  the ledger deploys; pre-ledger orphans have no merchant attribution
  anywhere and are handled by the global age sweep.

### 2.4 Scheduling keys (REMOVED in revision 9)

> **These keys are no longer used.** Revision 9 replaced the in-worker
> self-perpetuating scheduler with a Railway cron one-shot (`sweep.py`, TDD §3.4
> revision-9 note), which needs no marker or lock — Railway's cron is the
> scheduler. `zep:sweep:lock` and `zep:sweep:next` are neither written nor read
> anymore; the table below is retained only as the historical record of the
> retired scheduler.

| Key | Type | Semantics |
|---|---|---|
| `zep:sweep:lock` | String, `SET NX EX 360` | **Secondary concurrency guard, not the chain-collapse mechanism.** Value = occurrence job id; taken by the canonical occurrence AFTER the generation check to stop two *concurrent* replicas sweeping the same generation. Released via atomic Lua compare-and-delete on the owning occurrence id (revision 3), from BOTH the normal `finally` (best-effort — revision-6 F2, a release error must not abort successor scheduling) AND the `on_failure`/killed-horse re-seed, which **actively frees a dead occurrence's lease keyed to its id (revision-7)** so a hard SIGKILL/OOM does not leave the lease blocking the successor. **`LOCK_TTL`(360s) OUTLIVES the 300s interval floor**, so a canonical occurrence can find a stale lease still held; it does NOT exit empty (revision-7 — that killed the chain at the floor) but reschedules the successor (the holder is provably dead, since a live holder would still own the marker). TTL is a backstop; correctness no longer depends on it being shorter than the floor. Chain collapse is done by generation disposition on `zep:sweep:next`, not this lock. |
| `zep:sweep:next` | String, `EX = 3 × interval` | Chain-liveness marker **and the single canonical generation**; value = the next occurrence's job id. On dequeue an occurrence is classified **canonical** (marker == its id → sweep + schedule successor), **duplicate** (marker names another occurrence that is still **live** → exit, collapse), or **dead** (marker absent past TTL, names a job that no longer exists, OR names a job in a **terminal** state → re-seed one fresh chain in-band). **Liveness is an RQ runnable-state check (`_job_is_live`: status ∈ {queued, started, deferred, scheduled}), NOT hash presence (revision-7 F3)** — RQ retains a FAILED/CANCELED job's hash, so a terminal occurrence must classify as dead (re-seed), not a live duplicate. While an occurrence runs the marker holds *that occurrence's own id*. Every re-seed/advance path **enqueues the fresh occurrence FIRST, then CAS-sets the marker to it** (revision-7 F4 — so a process death between the two can never leave a phantom marker; a CAS-losing re-seeder's orphan job self-collapses via generation ownership when it runs). The `on_failure`/killed-horse re-seed CAS-claims when the marker is absent OR equals the failed/killed id (a bare `SET NX` cannot, since the running id is already present — revision 4). The boot reconciler re-seeds unless the marker names a **live** job (revision-7 — absent, phantom, OR terminal-hash markers are all re-seeded). Liveness *alerting* does not depend on this key — the Sentry Cron Monitor is the external watcher (TDD §3.4). |

RQ-owned keys (no new schema, listed for the namespace map):
`rq:queue:maintenance`, `rq:scheduled:maintenance` (ScheduledJobRegistry
holding unique `zep-graph-sweep-<hex>` occurrence ids — never a reused id).

## 3. Write-path summary

| Event | Ledger effect |
|---|---|
| `on_graph_created` fires mid-build (`cart_recovery_workflow.py:140`) | `HSET zep:graph:<id>` (graph_kind=scratch, merchant_id, created_at) + `ZADD zep:scratch:active` + `SADD zep:merchant:<m>:graphs` — one `MULTI` pipeline, guarded |
| Inline delete succeeds (`_cleanup_artifacts`) | `HSET deleted_at, delete_source=inline` + `EXPIRE 30d` + `ZREM` + `SREM` — one pipeline, guarded |
| Sweep delete succeeds | Same, `delete_source=sweep` |
| Sweep finds a ledger-only entry (in the active zset, absent from the listing), non-dry-run | Confirm it via a per-graph `graph.get` **404** before closing (revision-7 F1 — never inferred from listing completeness), then close it **generation-bound** to the snapshot `created_at` (revision-8 F1 — `record_deleted(..., expected_created_at=score)` under a WATCH; a same-id `record_created` racing in is left live) — lifecycle-aware (`deleted_at` + 30d TTL, `ZREM`, `SREM`) + `ledger_drift` counted |
| Sweep finds a stale-looking entry whose `graph.get` still returns / errors | Kept (fail-closed) + `reconcile_unconfirmed` counted — the listing dropped a page / under-reported (revision-7 F1) |
| Sweep finds a Zep-only matched scratch graph (in Zep, no ledger entry) | `zep_only_unattributed` counted (bidirectional drift, revision-6 F7); not "restored" (it is transient scratch, swept on age) |
| Dry-run listing | NO confirmation and NO reconciliation (revision-6 F9 — dry-run is a pure inventory, mutates nothing) |

## 4. Sizing

Steady state: ≤ a few dozen live hashes (in-flight runs) + 30-day tail of
deleted-graph hashes ≈ `runs/day × 30` small hashes (5 short fields each).
At even 1,000 analyses/day this is ~30k hashes of <220 bytes ≈ 7 MB — noise
for the existing instance. No unbounded growth: every key path has a TTL or
an explicit removal.
