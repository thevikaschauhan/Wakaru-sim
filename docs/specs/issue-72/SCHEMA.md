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
| `created_at` | Unix epoch seconds, integer string | `record_created` | Graph creation time (local clock). Roles: corroboration source for fail-closed deletion eligibility when the vendor timestamp is unparseable (TDD §3.2); diagnostics. Zep's `created_at` remains the primary age source. |
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
  ledger *think* is alive") and a drift detector: each sweep reconciles it
  against the listing and flags disagreements (`ledger_drift` count in the
  sweep stats).
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

### 2.4 Scheduling keys (revision-2 scheduler, TDD §3.4)

| Key | Type | Semantics |
|---|---|---|
| `zep:sweep:lock` | String, `SET NX EX 360` | Singleton execution lease; value = occurrence job id. Held for the duration of one sweep occurrence; released **only** via atomic Lua compare-and-delete on the owning occurrence id (revision 3 — GET+DELETE is not an allowed release: the lease can expire between the two commands and the DELETE would kill a successor's lock). A duplicate occurrence that fails to acquire exits without sweeping or rescheduling (chain collapse). |
| `zep:sweep:next` | String, `EX = 3 × interval` | Chain-liveness marker; value = the next scheduled occurrence's job id. Refreshed on every successful schedule (including by the `on_failure` re-seed). Absent ⇒ chain dead ⇒ boot reconciler enqueues (with `SET NX` as the claim so racing replicas seed once). Liveness *alerting* does not depend on this key — the Sentry Cron Monitor is the external watcher (TDD §3.4). |

RQ-owned keys (no new schema, listed for the namespace map):
`rq:queue:maintenance`, `rq:scheduled:maintenance` (ScheduledJobRegistry
holding unique `zep-graph-sweep-<hex>` occurrence ids — never a reused id).

## 3. Write-path summary

| Event | Ledger effect |
|---|---|
| `on_graph_created` fires mid-build (`cart_recovery_workflow.py:140`) | `HSET zep:graph:<id>` (graph_kind=scratch, merchant_id, created_at) + `ZADD zep:scratch:active` + `SADD zep:merchant:<m>:graphs` — one `MULTI` pipeline, guarded |
| Inline delete succeeds (`_cleanup_artifacts`) | `HSET deleted_at, delete_source=inline` + `EXPIRE 30d` + `ZREM` + `SREM` — one pipeline, guarded |
| Sweep delete succeeds | Same, `delete_source=sweep` |
| Sweep finds registry entry with no matching Zep graph (or vice versa) | Reconcile (`ZREM`/`SREM` stale entries) + `ledger_drift` counted in sweep stats |

## 4. Sizing

Steady state: ≤ a few dozen live hashes (in-flight runs) + 30-day tail of
deleted-graph hashes ≈ `runs/day × 30` small hashes (5 short fields each).
At even 1,000 analyses/day this is ~30k hashes of <220 bytes ≈ 7 MB — noise
for the existing instance. No unbounded growth: every key path has a TTL or
an explicit removal.
