# TDD — Delete per-cart Zep graphs and sweep historical orphans

**Issue:** [#72](https://github.com/thevikaschauhan/Wakaru-sim/issues/72)
**Companion docs:** [PRD](./PRD.md), [SCHEMA](./SCHEMA.md)
**Verified against:** `origin/main` @ `a3d7a1a`; `zep-cloud==3.13.0` SDK source at tag `v3.13.0`; `rq>=1.16,<2`

## 1. Verified API surface this design depends on

From `zep-cloud` 3.13.0 (`src/zep_cloud/graph/client.py`,
`src/zep_cloud/types/graph.py`, `graph_list_response.py` at tag `v3.13.0`):

- `client.graph.list_all(page_number: int | None, page_size: int | None) -> GraphListResponse`
  — pagination starts at page **1**; response fields `graphs: list[Graph] | None`,
  `row_count: int | None`, `total_count: int | None`.
- `Graph.graph_id: str | None`, `Graph.created_at: str | None` (string timestamp;
  format defensively parsed, see §4.3 and V-3).
- `client.graph.delete(graph_id: str) -> SuccessResponse`.
- `client.graph.get(graph_id: str) -> Graph`.

From the repo:

- Graph id format: `f"mirofish_{uuid.uuid4().hex[:16]}"` — `graph_builder.py:126`.
- Cleanup seam: `run_cart_recovery`'s `try/finally` with the `captured` dict
  (`cart_recovery_workflow.py:92-96`) feeding `_cleanup_artifacts` (`:241-275`).
- `graph_id` is already surfaced to the caller mid-build via the
  `on_graph_created` callback (`graph_builder.py:60,89-90`;
  `cart_recovery_workflow.py:140-151`).
- Worker entry: `backend/worker.py` — `Worker([ANALYZE_QUEUE_NAME], connection=...,
  log_job_description=False).work(with_scheduler=False)`.
- `merchant_id` availability: sync route has `g.merchant_id`
  (`app/__init__.py:236-252`); job path recovers it from `job.meta`
  (`cart_recovery_jobs.py`, `run_analysis_job`). `run_cart_recovery` itself does
  **not** receive it today.
- No SQL database exists; Redis backs idempotency (`idempotency.py`) and RQ.

## 2. Architecture

```
                       ┌──────────────────────────────────────────────┐
 analysis run          │  Zep Cloud (source of truth for orphans)     │
 ──────────────        │                                              │
 create graph ────────▶│  mirofish_ab12…  (created_at)                │
 …pipeline…            │                                              │
 finally:              │                                              │
   inline delete ─────▶│  DELETE (fast path, best-effort)             │
                       │                                              │
 worker (RQ scheduler) │                                              │
   sweep every 60m ───▶│  list_all → filter ^mirofish_[0-9a-f]{16}$   │
                       │  AND age > TTL → DELETE (guarantee path)     │
                       └──────────────────────────────────────────────┘
 Redis: lifecycle ledger (observability + offboarding index only)
```

Two independent deletion paths; the sweeper needs no memory of what the inline
path did, because Zep's own listing is the work queue. There is no state whose
loss can orphan a graph past one TTL window.

## 3. Code changes, file by file

### 3.1 `backend/app/services/cart_recovery_workflow.py`

**(a)** Thread `merchant_id` through (ledger attribution, FR-4):

```python
def run_cart_recovery(
    cart: ShopifyCartData,
    on_progress: ProgressCallback = None,
    merchant_id: str = SENTINEL_MERCHANT_ID,
) -> AbandonmentInsight:
```

Callers updated (the only two, verified by grep):
- `backend/app/api/cart_recovery.py` `/analyze` handler:
  `run_cart_recovery(cart, on_progress=..., merchant_id=g.merchant_id)`.
- `backend/app/services/cart_recovery_jobs.py` `run_analysis_job`: pass the
  `merchant_id` it already extracts from `job.meta`.

Default = sentinel keeps the signature backward-compatible for tests.

**(b)** Capture `graph_id` exactly like `simulation_id`
(`captured = {"simulation_id": None, "graph_id": None}`). Set it inside the
existing `_persist_graph_id` callback (`:140-144`), which fires the moment the
graph exists — so a failure at any later stage still has the id. Also write the
ledger "created" record there (`graph_lifecycle.record_created`, §3.3), guarded
so a Redis outage cannot fail the run.

**(c)** Extend `_cleanup_artifacts(project_id, simulation_id, graph_id, merchant_id)`
with a fourth guarded block, same contract as the existing three (never mask
the real result/exception):

```python
if graph_id is not None:
    try:
        GraphBuilderService(api_key=Config.ZEP_API_KEY).delete_graph(graph_id)
        graph_lifecycle.record_deleted(graph_id, source="inline")
    except Exception:
        logger.warning("scratch cleanup: zep graph delete failed for %s", graph_id)
```

`graph_id` is random hex, not PII — logging it is safe (same reasoning as the
existing docstring `:250-251`). Ordering note: the graph is deleted **after**
`_run_analysis` has returned (the insight is already built; `ReportAgent` is
the last graph reader), so the fast path can never race its own run.

### 3.2 `backend/app/services/zep_graph_sweeper.py` (new)

```python
SWEEPABLE_RE = re.compile(r"^mirofish_[0-9a-f]{16}$")

def sweep_orphan_graphs(*, dry_run: bool, ttl_hours: int, page_size: int) -> SweepStats:
```

Algorithm (single pass, bounded memory):

1. `page = 1`; loop `client.graph.list_all(page_number=page, page_size=page_size)`
   until `graphs` is empty/None. **Collect matching candidates first, delete
   after listing completes** — deleting while paginating shifts pages and
   skips entries.
2. Candidate = `SWEEPABLE_RE.fullmatch(graph_id)` **and**
   `age(created_at) > ttl_hours`. `created_at` parse: ISO-8601 via
   `datetime.fromisoformat` after `Z → +00:00` normalization; an absent or
   unparseable `created_at` on a `mirofish_`-matching graph counts as
   **older than TTL** (delete) and increments `unparseable_created_at` in
   stats — rationale: the prefix is exclusively produced by this service's
   throwaway path, and indefinite retention is the harm this issue fixes.
   (V-3 confirms the format in staging before prod enablement.)
3. For each candidate: if `dry_run`, log intent; else `client.graph.delete`,
   `record_deleted(graph_id, source="sweep")`. Per-graph try/except: one
   failure never aborts the sweep; failures counted and logged by id.
4. Emit one summary log line (`zep_sweep scanned=… matched=… deleted=…
   failed=… skipped_dry_run=… unparseable_created_at=… oldest_active_age_s=…
   total_count=…`) and `sentry_sdk.capture_message` on: sweep-level exception,
   any per-graph failure count > 0, or `oldest_active_age_s > 2 × ttl_hours`.

Config (module-level, read live from `os.environ` per the `Config.validate`
convention): `ZEP_GRAPH_TTL_HOURS` (default 24, **floor 6** — below-floor
values fall back to default with a warning, mirroring
`job_queue.analyze_job_timeout`'s footgun guard), `ZEP_SWEEP_INTERVAL_MINUTES`
(default 60, floor 5), `ZEP_SWEEP_DRY_RUN` (default `false`; only the literal
`"false"` disables it — any other value stays dry, fail-safe),
`ZEP_SWEEP_PAGE_SIZE` (default 100).

### 3.3 `backend/app/services/graph_lifecycle.py` (new)

Thin Redis writer/reader for the ledger keys in SCHEMA.md:
`record_created(graph_id, merchant_id)`, `record_deleted(graph_id, source)`,
`oldest_active_age_seconds()`, `graphs_for_merchant(merchant_id)`.
Every function takes the connection from `job_queue.get_redis_connection()`
and **no-ops with a warning when Redis is unavailable** — the ledger is
observability, never correctness (PRD FR-4).

### 3.4 `backend/app/services/maintenance_queue.py` (new) + `backend/worker.py`

- New queue name `maintenance`, **separate from `analyze`**: a sweep enqueued
  on `analyze` would sit behind 8-17-minute analysis jobs and, worse, an
  analysis behind a sweep would add latency to a paid path.
- `worker.py` changes:
  - `Worker([ANALYZE_QUEUE_NAME, MAINTENANCE_QUEUE_NAME], ...)`
  - `.work(with_scheduler=True)` — RQ ≥ 1.2 built-in scheduler; in-range for
    the pinned `rq>=1.16,<2`. No new package, no new Railway service.
  - Boot reconciler `ensure_sweep_scheduled(connection)`: with deterministic
    `job_id="zep-graph-sweep"`, if the job id is in neither the maintenance
    queue, its `ScheduledJobRegistry`, nor `StartedJobRegistry`, enqueue it
    immediately. Combined with self-rescheduling (below), this heals a broken
    chain on every worker (re)start — the same boot-reconciler pattern engine
    #156 uses.
- The sweep job body runs `sweep_orphan_graphs(...)` and **re-schedules itself
  in a `finally`** via `queue.enqueue_in(timedelta(minutes=interval), ...,
  job_id="zep-graph-sweep")`, so one failed sweep never ends the cycle. The
  deterministic job id makes double-scheduling collapse to one entry.
- Job timeout for sweeps: 600 s (a full page-through + deletes is minutes, not
  hours), `result_ttl=0` (stats live in logs, not RQ results).

### 3.5 Not changed

- `graph_builder.py` — `delete_graph` is already correct; it gains callers,
  not edits.
- Web tier request path — zero behavioral change other than passing
  `merchant_id` down.
- `railway.toml`, Dockerfile, service topology — none.

## 4. Failure-mode table

| Failure | Behavior | Recovery |
|---|---|---|
| Inline delete fails (Zep 5xx, network) | Warning logged; run result unaffected | Sweeper deletes at ≤ TTL + interval |
| Worker killed mid-run (SIGKILL, deploy) | No inline delete happens | Same — graph is listed, aged, swept |
| Redis flushed / unavailable | Ledger writes no-op with warning; scheduler entry lost | Boot reconciler re-schedules on worker start; sweep correctness unaffected (reads Zep) |
| Zep outage during sweep | Sweep fails, Sentry message; self-reschedule still runs (finally) | Next cycle retries; orphan-age alert fires at 2×TTL if prolonged |
| Sweep job crashes before self-reschedule | Chain broken | Boot reconciler restores it at next worker restart; Sentry captured the crash |
| Operator sets TTL below in-flight run length | Floor of 6 h refuses the value, warns, uses default | — |
| #61 later adds `merchant_*` graphs | Regex cannot match them | Structural, tested |

## 5. Security and PII

- No new endpoints, no new inbound surface.
- Log lines carry only graph ids / merchant UUIDs (non-PII per the existing
  `#7/#17` discipline and the `_cleanup_artifacts` docstring).
- The sweeper uses the existing `ZEP_API_KEY`; no new secrets.

## 6. Testing plan

Unit (pytest, `backend/tests/`, fake Zep client per existing patterns in
`test_cart_recovery_workflow.py` / `test_cart_recovery_cleanup.py`):

1. `test_cleanup_deletes_graph_on_success` / `_on_pipeline_failure` — captured
   graph_id flows to `delete_graph` in both outcomes; assert the analysis
   result/exception is preserved when delete raises.
2. `test_cleanup_without_graph` — failure before graph creation ⇒ no delete call.
3. `test_sweeper_filters` — table-driven: `mirofish_<16hex>` old ⇒ deleted;
   young ⇒ kept; `merchant_abc…` old ⇒ untouched; `mirofish_SHOUTING` ⇒
   untouched; missing `created_at` ⇒ deleted + counted.
4. `test_sweeper_dry_run` — zero delete calls, correct counts.
5. `test_sweeper_pagination` — multi-page fake listing; delete-after-list
   ordering asserted.
6. `test_sweeper_partial_failure` — one delete raises; sweep continues;
   failure counted.
7. `test_ensure_sweep_scheduled_idempotent` — two boots, one scheduled job
   (fakeredis or the repo's existing Redis test approach).
8. `test_ttl_floor` / `test_dry_run_default_safe` — config guards.
9. Regression: existing `test_cart_recovery_cleanup.py` suite stays green
   (signature change is defaulted).

Staging verification (pre-prod flip): V-1..V-3 in §7.

## 7. Open items verified-in-staging (explicitly not assumed)

- **V-1**: exact exception class Zep raises on `delete` of an already-deleted
  graph (expected 404 → treated as success; code catches `ApiError` and
  inspects status).
- **V-2**: `list_all` behavior at exactly `total_count % page_size == 0`
  (empty last page vs absent `graphs`) — loop handles both, staging confirms.
- **V-3**: `Graph.created_at` wire format (assumed ISO-8601; parser is
  defensive either way).

## 8. Rollout + historical-orphan runbook (maps to #72 AC-3)

1. Merge + deploy web and worker (same image). Sweeper ships with
   `ZEP_SWEEP_DRY_RUN` unset ⇒ **dry**.
2. First dry sweep logs the full inventory: `total_count`, matched count,
   oldest age. Operator reviews the summary line (this is the "inventory"
   artifact; paste it into #72).
3. Operator sets `ZEP_SWEEP_DRY_RUN=false` on the **worker** service, restarts.
4. Next sweep deletes the backlog; #72 AC-3 closes with the before/after
   `total_count` pasted into the issue.
5. Confirm steady state over 48 h: `oldest_active_age_s < TTL`, no Sentry
   sweep alerts.
6. Operator documentation task: record Zep DPA / deletion-SLA reference in the
   repo's `docs/integration.md` (PRD §5).
