# Backend schema — graph lifecycle ledger (issue #72)

**Issue:** [#72](https://github.com/thevikaschauhan/Wakaru-sim/issues/72)
**Companion docs:** [PRD](./PRD.md), [TDD](./TDD.md)

## 1. Why Redis, and the durability boundary

Wakaru has no SQL database (verified: `backend/requirements.txt` /
`backend/pyproject.toml` carry no DB driver; state = Redis + local scratch).
Adding Postgres for one stopgap table contradicts the stopgap's purpose, and
SQLite on Railway is ephemeral across deploys.

The ledger therefore lives in the **existing Redis** (same instance as RQ +
idempotency, `job_queue.get_redis_connection()`), under one explicit
invariant, stated here and enforced in code:

> **Correctness never depends on the ledger.** Sweep decisions read Zep's own
> `graph.list_all`. If Redis is flushed, the only losses are metric history
> and the offboarding index for graphs that are all ≤ 24 h from deletion
> anyway. Every ledger write is wrapped so Redis unavailability degrades to a
> warning, never a failed analysis or failed sweep.

All keys use the `zep:` prefix to stay clear of the existing `idem:` and
RQ (`rq:*`) namespaces.

## 2. Key schema

### 2.1 `zep:graph:<graph_id>` — Hash (one per graph, the record of truth for attribution)

| Field | Type/format | Written by | Semantics |
|---|---|---|---|
| `merchant_id` | canonical lowercase UUID string (per `validate_merchant_id`, `paths.py:52`); sentinel `00000000-0000-0000-0000-000000000000` for legacy engine calls | `record_created` | Tenant attribution for offboarding/audit |
| `created_at` | Unix epoch seconds, integer string | `record_created` | Graph creation time (local clock; Zep's `created_at` remains authoritative for sweep age) |
| `deleted_at` | Unix epoch seconds, integer string; absent while alive | `record_deleted` | Deletion time |
| `delete_source` | `inline` \| `sweep`; absent while alive | `record_deleted` | Which path deleted it (metric: sweep-share ≈ inline-failure rate) |

TTL: none while alive; `EXPIRE 2592000` (30 days) set by `record_deleted`.
A graph deleted by the sweeper that never had a `record_created` (pre-#72
historical orphan, or created during a Redis outage) gets a hash written at
deletion time with `merchant_id` absent — explicitly distinguishable from the
sentinel.

### 2.2 `zep:graphs:active` — Sorted set (the orphan-age index)

- **Member:** `graph_id`; **Score:** creation epoch seconds.
- `record_created` → `ZADD`; `record_deleted` → `ZREM`.
- `oldest_active_age_seconds()` = `now − score(ZRANGE zep:graphs:active 0 0 WITHSCORES)`.
- Feeds the `oldest_active_age_s` gauge and the 2×TTL Sentry alert (TDD §3.2).
- Drift tolerance: an entry whose graph the sweeper no longer finds in Zep
  (inline-delete + Redis blip ordering) is `ZREM`-ed by the sweeper during its
  pass (reconciliation, not correctness).

### 2.3 `zep:merchant:<merchant_id>:graphs` — Set (offboarding enumeration, PRD FR-4)

- **Members:** `graph_id`s created for this merchant and not yet deleted.
- `record_created` → `SADD`; `record_deleted` → `SREM`.
- Offboarding procedure: `SMEMBERS` → `graph.delete` each → confirm empty.
  In the throwaway model this set is empty in steady state (graphs live
  minutes); the index exists so that *proving* emptiness is one command, and
  so #61's long-lived `merchant_*` graphs can reuse the identical mechanism.

### 2.4 RQ-owned keys (no new schema, listed for the namespace map)

- `rq:queue:maintenance` — new queue (TDD §3.4).
- `rq:scheduled:maintenance` (`ScheduledJobRegistry`) — holds the
  deterministic `zep-graph-sweep` job between runs.

## 3. Write-path summary

| Event | Ledger effect |
|---|---|
| `on_graph_created` fires mid-build (`cart_recovery_workflow.py:140`) | `HSET zep:graph:<id>` (merchant_id, created_at) + `ZADD zep:graphs:active` + `SADD zep:merchant:<m>:graphs` — one `MULTI` pipeline, guarded |
| Inline delete succeeds (`_cleanup_artifacts`) | `HSET deleted_at, delete_source=inline` + `EXPIRE 30d` + `ZREM` + `SREM` — one pipeline, guarded |
| Sweep delete succeeds | Same, `delete_source=sweep` |
| Sweep finds active-set entry with no Zep graph | `ZREM` + `SREM` (reconcile) |

## 4. Sizing

Steady state: ≤ a few dozen live hashes (in-flight runs) + 30-day tail of
deleted-graph hashes ≈ `runs/day × 30` small hashes (4 short fields each).
At even 1,000 analyses/day this is ~30k hashes of <200 bytes ≈ 6 MB — noise
for the existing instance. No unbounded growth: every key path has a TTL or
an explicit removal.
