# TDD — Delete per-cart Zep graphs and sweep historical orphans

**Issue:** [#72](https://github.com/thevikaschauhan/Wakaru-sim/issues/72)
**Companion docs:** [PRD](./PRD.md), [SCHEMA](./SCHEMA.md)
**Verified against:** `origin/main` @ `a3d7a1a`; `zep-cloud==3.13.0` SDK source at tag `v3.13.0`; `rq>=1.16,<2`

> **Revision 2 (2026-07-21), after external design review:** the scheduler no
> longer reuses a deterministic job id for self-rescheduling (on pinned RQ,
> re-enqueueing the id of a *running* job overwrites its record, and
> `result_ttl=0` cleanup can then delete the newly scheduled job — silently
> ending the chain). Replaced with unique occurrence ids + an atomic singleton
> lock + a chain-liveness marker (§3.4), proven against real Redis and the
> pinned RQ version. Deletion eligibility is now fail-closed on unknown age
> (§3.2). The orphan-age metric derives from the Zep scan (§3.2). Per-run
> deletion is capped. The dry-run parser has one rule. Queue-interference
> bounds are stated honestly (§3.4).
>
> **Revision 3 (2026-07-21), after review-round 2 — the r2 liveness story was
> self-referential:** a work-horse SIGKILL/OOM skips the job's `finally`
> while the worker process survives, so the chain dies with no boot to
> reconcile it, and the 2×TTL alert was emitted by the very sweep that was no
> longer running. Fixed with an **independent watcher** (Sentry Cron Monitor
> check-in per occurrence — missed-check-in alerts come from Sentry's
> infrastructure, not the worker) plus an RQ failure callback that re-seeds
> the chain when a horse dies (§3.4). Lock release is Lua compare-and-delete
> **only** (the r2 GET+DELETE fallback had a lease-expiry race). The sweep
> keeps a bounded candidate heap with streamed metrics instead of collecting
> every match (§3.2).

## 1. Verified API surface this design depends on

From `zep-cloud` 3.13.0 (`src/zep_cloud/graph/client.py`,
`src/zep_cloud/types/graph.py`, `graph_list_response.py` at tag `v3.13.0`):

- `client.graph.list_all(page_number: int | None, page_size: int | None) -> GraphListResponse`
  — pagination starts at page **1**; response fields `graphs: list[Graph] | None`,
  `row_count: int | None`, `total_count: int | None`.
- `Graph.graph_id: str | None`, `Graph.created_at: str | None` (string timestamp;
  format defensively parsed, see §3.2 and V-3).
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

RQ semantics this design must respect (the revision-2 driver): a worker
executes **one job at a time**; enqueueing with the job id of a currently
executing job **overwrites** that job's Redis hash; with `result_ttl=0`, the
finishing worker's cleanup deletes the job key even if a scheduled entry still
references it. Therefore a running job must never schedule its own id.

## 2. Architecture

```
                       ┌──────────────────────────────────────────────┐
 analysis run          │  Zep Cloud (source of truth for orphans      │
 ──────────────        │  AND for the orphan-age metric)              │
 create graph ────────▶│  mirofish_ab12…  (created_at)                │
 …pipeline…            │                                              │
 finally:              │                                              │
   inline delete ─────▶│  DELETE (fast path, best-effort)             │
                       │                                              │
 worker (maintenance)  │                                              │
   sweep every 60m ───▶│  list_all → filter ^mirofish_[0-9a-f]{16}$   │
                       │  AND proven age > TTL → DELETE (≤ cap/run)   │
                       └──────────────────────────────────────────────┘
 Redis: scratch registry (corroboration + offboarding index + diagnostics)
        sweep singleton lock + chain-liveness marker (scheduling only)
```

Two independent deletion paths; the sweeper needs no memory of what the inline
path did, because Zep's own listing is the work queue. There is no state whose
loss can orphan a graph past one TTL window — and no state whose loss can
*delete* a live graph, because eligibility is fail-closed (§3.2).

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

def sweep_orphan_graphs(*, dry_run: bool, ttl_hours: int, page_size: int,
                        max_deletes: int) -> SweepStats:
```

Algorithm (single listing pass; **genuinely bounded memory — revision 3**:
the r2 text claimed bounded memory while collecting every match; now only a
`max_deletes`-sized heap and running aggregates are retained):

1. `page = 1`; loop `client.graph.list_all(page_number=page, page_size=page_size)`
   until `graphs` is empty/None. **No deletion during pagination** — deleting
   while paginating shifts pages and skips entries. Per page, each matched
   graph is folded into streaming state and the page is discarded.
2. For every `SWEEPABLE_RE.fullmatch(graph_id)` graph, establish **proven
   age**:
   - Parse vendor `created_at` (ISO-8601 via `datetime.fromisoformat` after
     `Z → +00:00` normalization). Parseable ⇒ that is the age.
   - Unparseable/absent ⇒ consult the ledger's `created_at` for the id
     (SCHEMA §2.1). Present ⇒ corroborated age.
   - Neither ⇒ **unknown age: skip, increment `skipped_unknown_age`, log the
     id at WARNING**. Never deleted automatically (revision 2, fail-closed —
     a vendor timestamp-format regression must not become a mass deletion of
     in-flight graphs; disposal of persistent unknown-age orphans is the §8
     runbook's operator-approved path).
3. Streaming state (revision 3): a **bounded max-heap of the oldest
   ≤ `max_deletes` eligible candidates** (eligible = proven age >
   `ttl_hours`); running counters `scanned`, `matched`, `eligible_total`,
   `skipped_unknown_age`; running `oldest_scratch_age_seconds` over **all**
   matched graphs (aggregate, no ids retained). `eligible_total −
   len(heap)` is reported as `truncated_backlog` — the cap bounds deletion
   calls per cycle, and the backlog it defers is visible, not silent.
   Dry-run inventories via these counters + WARNING-level per-id log lines
   as they stream — it does not require storing all ids either.
4. For each heap candidate (oldest first): if `dry_run`, log intent; else
   `client.graph.delete`, `record_deleted(graph_id, source="sweep")`.
   Per-graph try/except: one failure never aborts the sweep; failures
   counted and logged by id.
5. **Metrics from this scan, not Redis:** `oldest_scratch_age_seconds` as
   accumulated in step 3 (adjusted for deletions of the oldest candidates).
   Reconcile the Redis registry against the listing (remove entries whose
   graphs no longer exist; flag ledger drift) — membership checks stream per
   page against the registry, no full listing copy needed.
6. Emit one summary log line (`zep_sweep scanned=… matched=…
   eligible_total=… deleted=… failed=… skipped_dry_run=…
   skipped_unknown_age=… truncated_backlog=… oldest_scratch_age_s=…
   total_count=…`) and `sentry_sdk.capture_message` on: sweep-level
   exception, per-graph failure count > 0, `skipped_unknown_age > 0`, or
   `oldest_scratch_age_s > 2 × ttl_hours`.
7. **Liveness check-in (revision 3):** the occurrence wraps its body in a
   Sentry Cron Monitor check-in (`monitor_slug="zep-graph-sweep"`,
   schedule = interval, grace = interval; `sentry_sdk.crons` — the SDK is
   already a dependency). In-progress → ok/error statuses are reported; a
   **missed** check-in alert is raised by Sentry's own infrastructure, so
   sweep death is detected by something the sweep cannot take down with it.

Config (module-level, read live from `os.environ` per the `Config.validate`
convention):

- `ZEP_GRAPH_TTL_HOURS` — default 24, **floor 6** (below-floor values fall
  back to default with a warning, mirroring `job_queue.analyze_job_timeout`'s
  footgun guard).
- `ZEP_SWEEP_INTERVAL_MINUTES` — default 60, floor 5.
- `ZEP_SWEEP_DRY_RUN` — **one parser rule (revision 2): deletion is enabled
  only when the value, after `.strip().lower()`, equals `"false"`. Absent,
  empty, or anything else ⇒ dry run.** (The rollout in §8 relies on
  ships-unset ⇒ dry.)
- `ZEP_SWEEP_PAGE_SIZE` — default 100.
- `ZEP_SWEEP_MAX_DELETES` — default 200, floor 1.

### 3.3 `backend/app/services/graph_lifecycle.py` (new)

Thin Redis writer/reader for the **scratch registry** in SCHEMA.md:
`record_created(graph_id, merchant_id)` (stamps `graph_kind=scratch` and
asserts the id matches `SWEEPABLE_RE` — store graphs are structurally
rejected, revision 2), `record_deleted(graph_id, source)`,
`created_at_for(graph_id)` (the §3.2 corroboration read),
`graphs_for_merchant(merchant_id)`. Every function takes the connection from
`job_queue.get_redis_connection()` and **no-ops with a warning when Redis is
unavailable** — the ledger corroborates and enumerates; it never gates
correctness (PRD FR-4).

### 3.4 Scheduling: `backend/app/services/maintenance_queue.py` (new) + `backend/worker.py`

**Queue.** New queue name `maintenance`, drained by the existing worker:
`Worker([ANALYZE_QUEUE_NAME, MAINTENANCE_QUEUE_NAME], ...)` with
`.work(with_scheduler=True)` (RQ ≥ 1.2 built-in scheduler; in-range for the
pinned `rq>=1.16,<2`).

**Interference bound, stated honestly (revision 2; alerting corrected in
revision 3):** an RQ worker executes one job at a time, so queues do not
isolate workloads — they set priority. With `analyze` listed first, a queued
analysis always dequeues ahead of a queued sweep; the worst case for a paid
analysis is waiting out one **in-flight** sweep, bounded by the sweep
`job_timeout` of **300 s**. The worst case for the sweep is starvation under
sustained back-to-back analyses — detected by the **missed Sentry Cron
check-in** (§3.2 step 7), which fires from outside the worker (revision 3:
the r2 text claimed the orphan-age alert covers this, but a starved sweep
emits no metrics at all — starvation must be watched externally). This trade
(≤ 5 min added tail latency on an 8-17 min job, vs a new
operator-provisioned Railway service) is an explicit product acceptance in
PRD §3; a dedicated maintenance worker service remains the documented
scale-out path if either bound is hit in practice.

**Occurrence scheduling (revision 2 — replaces same-id self-reschedule;
revision 3 — liveness made independent of the chain):**

- Every sweep occurrence is enqueued with a **unique** job id
  (`zep-graph-sweep-<uuid4hex>`), `job_timeout=300`, `result_ttl=0`,
  `failure_ttl=86400`. No id is ever reused, so no running job's record can
  be overwritten and `result_ttl=0` cleanup can only ever delete the
  *finished* occurrence's own record.
- **Chain-liveness marker:** `zep:sweep:next` (SCHEMA §2.4) is set to the
  scheduled occurrence's id with `EX = 3 × interval` every time an occurrence
  is scheduled. Its absence means the chain is dead.
- **Singleton execution lock:** the job body's first action is
  `SET zep:sweep:lock <occurrence_id> NX EX 360` (lease > job_timeout). If
  the lock is held by another occurrence, this occurrence exits immediately
  **without sweeping and without rescheduling** — it is a duplicate chain,
  and declining to reschedule is what collapses duplicate chains back to one.
  The winner releases the lock in a `finally` via **atomic Lua
  compare-and-delete only** (revision 3: the r2 GET+DELETE fallback is
  removed — the lease can expire between GET and DELETE and the DELETE would
  then kill a successor's lock; non-atomic release is not an allowed
  implementation).
- **Self-perpetuation:** the lock-holding occurrence schedules the next
  occurrence in the same `finally` (after lock release logic, before exit):
  `maintenance_queue.enqueue_in(timedelta(minutes=interval), sweep_job,
  job_id=new_unique_id)` + refresh `zep:sweep:next`.
- **Chain-death healing (revision 3 — the r2 story was incomplete: a
  work-horse SIGKILL/OOM skips `finally` while the worker survives, and the
  boot reconciler only runs at boot):**
  - **RQ failure callback:** occurrences are enqueued with an `on_failure`
    callback (runs in the *worker* process, which survives horse death) that
    re-seeds the chain — `SET NX` claim on `zep:sweep:next`, then enqueue a
    fresh occurrence `interval` out. Covers job exceptions, timeouts, and
    horse kills that RQ detects.
  - **Boot reconciler:** unchanged (worker start + marker absent ⇒ enqueue,
    `SET NX` claim; racing replicas seed once) — now the second line of
    defense, not the only one.
  - **Independent watcher (the actual guarantee):** the Sentry Cron Monitor
    check-in (§3.2 step 7) alerts on a missed occurrence from outside the
    worker entirely. Whatever kills the chain — including failure modes the
    two heals above cannot see (worker wedged, scheduler thread dead,
    sustained queue starvation) — surfaces as a missed check-in within one
    interval + grace. **No liveness property is attested solely by the
    mechanism whose death it must detect.**

**Proof obligation (revision 2, extended by revision 3):** these mechanics
are covered by integration tests running against **real Redis and the pinned
RQ version** (§6, tests 12-16) — not asserted from documentation. Pinned
regressions: scheduling a unique-id occurrence while another runs must never
mutate the running job's record (r1 failure); a horse-killed occurrence must
still yield a rescheduled chain via `on_failure` (r2 failure); lock release
must be atomic under lease expiry (r2 failure).

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
| Vendor `created_at` format changes | Ages unparseable ⇒ ledger corroboration; if both unavailable, graphs are **retained** and `skipped_unknown_age` alerts | Operator runbook; no mass deletion (revision 2) |
| Redis flushed / unavailable | Ledger writes no-op with warning; marker + lock lost | Boot reconciler restores the chain on worker start; sweep correctness and the orphan-age metric are Zep-derived and unaffected |
| Occurrence raises / times out | RQ marks it failed | `on_failure` callback (worker process) re-seeds the chain immediately (revision 3) |
| Work horse SIGKILL/OOM (`finally` skipped, worker survives) | Chain would die silently in r2 | `on_failure` re-seeds when RQ detects horse death; regardless, the **missed Sentry Cron check-in alerts within interval + grace** (revision 3) |
| Worker wedged / scheduler thread dead / sustained starvation | No occurrences run; nothing in-process can notice | Missed Sentry Cron check-in — the watcher is external to the worker (revision 3) |
| Duplicate chains (replica race, manual enqueue) | Loser fails the singleton lock, exits without rescheduling | Chains collapse to one within one interval |
| Lock lease expires mid-sweep, successor acquires | Lua compare-and-delete release no-ops on the successor's lock (revision 3 — GET+DELETE removed for exactly this race) | Successor proceeds; overlap bounded by lease design |
| Zep outage during sweep | Sweep fails, Sentry error status on the check-in; `finally` still schedules next occurrence | Next cycle retries |
| Backlog larger than per-run cap | Oldest-first deletion from the bounded heap, `truncated_backlog` logged | Drains across cycles; visible, not silent |
| Operator sets TTL below in-flight run length | Floor of 6 h refuses the value, warns, uses default | — |
| #61 later adds `merchant_*` graphs | Regex cannot match them; `record_created` asserts scratch ids only | Structural, tested |

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
3. `test_sweeper_filters` — table-driven: `mirofish_<16hex>` proven-old ⇒
   deleted; young ⇒ kept; `merchant_abc…` old ⇒ untouched;
   `mirofish_SHOUTING` ⇒ untouched.
4. `test_sweeper_fail_closed_unknown_age` — unparseable vendor `created_at`
   with no ledger entry ⇒ **kept** + counted + would-alert; with a ledger
   entry older than TTL ⇒ deleted (corroboration path). (Revision 2.)
5. `test_sweeper_dry_run_parser` — matrix: absent, `""`, `true`, `TRUE`,
   `false`, `False`, ` false `, `0`, `no`, garbage ⇒ only the `false`
   variants delete. (Revision 2.)
6. `test_sweeper_pagination` — multi-page fake listing; delete-after-list
   ordering asserted.
7. `test_sweeper_partial_failure` — one delete raises; sweep continues;
   failure counted.
8. `test_sweeper_delete_cap_bounded_heap` — backlog > cap ⇒ oldest-first
   from the heap, cap respected, `truncated_backlog` = eligible_total − cap;
   heap never holds more than `max_deletes` entries during a multi-page
   listing (revision 3 asserts the memory bound, not just the call bound).
9. `test_metrics_from_scan` — oldest age computed from the streamed listing,
   not Redis; registry reconciliation removes stale entries. (Revision 2.)
10. `test_ttl_floor` — config guard.
11. `test_cron_checkin_emitted` — occurrence body wraps in the Sentry cron
    check-in (fake transport records in_progress → ok on success, error on
    raise). (Revision 3.)

Integration (real Redis + pinned RQ; new `tests/test_sweep_scheduling.py`,
env-gated on a Redis URL like the repo's other gated tests — revision 2's
proof obligation, extended by revision 3):

12. `test_unique_occurrence_ids_never_touch_running_job` — start occurrence A
    (long-running stub), schedule occurrence B; assert A's job hash is
    unmodified and A completes + cleans only its own record.
13. `test_on_failure_reseeds_chain` — occurrence raises (and separately: its
    horse is killed); the `on_failure` callback enqueues exactly one fresh
    occurrence and refreshes the marker. (Revision 3.)
14. `test_boot_reconciler` — marker expired, no scheduled occurrence; worker
    boot re-seeds exactly one (two reconcilers racing seed exactly one —
    `SET NX` claim).
15. `test_duplicate_chains_collapse` — two live occurrences; loser exits
    without sweeping or rescheduling; exactly one chain remains after one
    interval.
16. `test_lock_release_atomic` — occurrence A's lease expires mid-run;
    occurrence B acquires; A's release no-ops (Lua compare-and-delete) and
    B's lock survives. (Revision 3 — pins the removed GET+DELETE race.)

Regression: existing `test_cart_recovery_cleanup.py` suite stays green
(signature change is defaulted).

## 7. Open items verified-in-staging (explicitly not assumed)

- **V-1**: exact exception class Zep raises on `delete` of an already-deleted
  graph (expected 404 → treated as success; code catches `ApiError` and
  inspects status).
- **V-2**: `list_all` behavior at exactly `total_count % page_size == 0`
  (empty last page vs absent `graphs`) — loop handles both, staging confirms.
- **V-3**: `Graph.created_at` wire format (assumed ISO-8601). Revision 2 note:
  this is no longer a deletion-safety dependency — an unexpected format
  degrades to fail-closed retention + alert, not deletion.

## 8. Rollout + historical-orphan runbook (maps to #72 AC-3)

1. Merge + deploy web and worker (same image). Sweeper ships with
   `ZEP_SWEEP_DRY_RUN` unset ⇒ **dry** (single parser rule, §3.2).
2. First dry sweep logs the full inventory: `total_count`, matched count,
   oldest age, and the **unknown-age list** (expected: pre-ledger orphans
   whose vendor timestamps parse fine will show proven ages; the unknown-age
   list should be empty or tiny). Operator reviews the summary (this is the
   "inventory" artifact; paste it into #72).
3. Operator sets `ZEP_SWEEP_DRY_RUN=false` on the **worker** service, restarts.
4. Next sweeps drain the backlog (≤ `ZEP_SWEEP_MAX_DELETES` per cycle);
   #72 AC-3 closes with the before/after `total_count` pasted into the issue.
5. **Unknown-age disposal (operator-approved path):** if any unknown-age
   graphs persist, the operator reviews the logged ids and disposes of them
   explicitly — `ZEP_SWEEP_FORCE_DELETE_IDS` (comma-separated exact ids,
   consumed by the next sweep, logged loudly, then unset). Automatic deletion
   of unknown-age graphs is never enabled.
6. Confirm steady state over 48 h: `oldest_scratch_age_s < TTL`,
   `skipped_unknown_age = 0`, no Sentry sweep alerts.
7. Operator documentation task: record Zep DPA / deletion-SLA reference in the
   repo's `docs/integration.md` (PRD §5).
