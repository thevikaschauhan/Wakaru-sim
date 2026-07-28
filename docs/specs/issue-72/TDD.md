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
>
> **Revision 4 (2026-07-24), after the W2 implementation review - the r3
> healing text overstated the in-band recovery:** two corrections, both
> verified against the RQ 1.16.2 source in the pinned venv. (1) The
> `on_failure` re-seed cannot be a bare `SET NX`: while an occurrence runs,
> `zep:sweep:next` already holds *that occurrence's own id* (the prior
> occurrence set it), so `SET NX` finds the key present and declines, silently
> killing the chain. It is now an atomic compare-and-set that claims the marker
> when it is absent OR still equals the failed occurrence's id (§3.4). (2) RQ
> 1.16.2 does NOT invoke the failure callback on a hard work-horse SIGKILL/OOM
> (`monitor_work_horse -> handle_work_horse_killed -> handle_job_failure` moves
> the job to the failed registry without calling `execute_failure_callback`);
> the callback fires only for a caught exception or timeout inside the horse.
> A hard kill is therefore healed by the boot reconciler (after the stale
> marker's TTL lapses) and, definitively, by the external Sentry Cron miss -
> which was already the design's stated guarantee. The in-band SIGKILL auto-heal
> is weaker than r3 implied; the deployable invariant and the ultimate liveness
> guarantee are unchanged.
>
> **Revision 5 (2026-07-25), after an external multi-agent review of PR #78
> (11 findings, all confirmed against the code):** the load-bearing correction
> is **F1** — the r2/r3 claim that the singleton lock collapses duplicate chains
> was wrong. On a single worker the lock is free between *serial* occurrences,
> so two queued duplicates both acquire it, both sweep, and both schedule
> successors → permanent duplicate chains. Collapse is now **generation
> ownership**: an occurrence sweeps + reschedules ONLY if `zep:sweep:next` names
> it (single atomic read, checked before the lock); the lock is demoted to a
> secondary guard against *concurrent* replicas (§3.4). **F3:** the scheduler no
> longer publishes a marker before the job it names is enqueued (enqueue-first +
> CAS-advance; compare-delete rollback on enqueue failure), and a failed
> successor-enqueue is *raised* not swallowed so `on_failure` heals instead of
> the chain dying silently. **F4:** the Sentry Cron check-ins now carry a
> `monitor_config` (interval schedule + grace) so a *missed* occurrence actually
> alerts — the r3/r4 external-watcher guarantee was inert without it. Sweeper
> hardening in the same review: force-delete is regex- + observed-scan- + cap-
> bounded (F2, never deletes `merchant_*`/tombstone); reconciliation is skipped
> on an incomplete listing (F6) and closes hashes + merchant sets lifecycle-aware
> (F9); paginated ids are de-duplicated (F7); a ledger-bookkeeping error can no
> longer abort a successful vendor delete (F8); impossible ledger timestamps
> (≤0/future) fail closed to unknown-age (F11). CI now runs the real-Redis
> scheduler suite against a Redis service container (F5). Every fix is pinned by
> a revert-proofed test; the deployable invariant (ships dry-run) is unchanged.
>
> **Revision 6 (2026-07-26), after the second external review round of PR #78
> (11 further findings, all confirmed):** the r5 fixes closed the headline gaps
> but the reviewer found deeper liveness and deletion-safety holes, all now
> fixed and revert-proofed. **Scheduler:** (F2) a lock-release Redis error on
> the success path silently ended the chain — successor scheduling is now
> decoupled from the release result (schedule on sweep SUCCESS regardless; the
> 360s lock TTL backstops a missed release). (F3) generation ownership was too
> blunt — a *non-canonical* occurrence always exited, so a TTL-starved chain
> (marker expired) or a phantom marker died silently; the gate now classifies
> **canonical / duplicate / dead**, and a **dead** disposition (marker absent, or
> naming a job that no longer exists) RE-SEEDS one fresh chain in-band via the
> atomic claim. A **`work_horse_killed_handler`** on the Worker (rq 1.16.2
> supports it; runs in the surviving parent) re-seeds on a hard SIGKILL/OOM that
> skips `on_failure` — so the hard-kill heal is now in-band, not only the Sentry
> miss. (F4) the boot reconciler trusted marker *presence*, leaving a phantom
> marker (crash between claim and enqueue) unhealed forever; it now re-seeds
> unless the marker names a job that still **exists**. **Sweeper:** (F1)
> listing-completeness is judged over **distinct** graph ids covering a
> `total_count` that Zep actually reported (read before the empty-page break), so
> duplicate rows / an empty first page can no longer authorize destructive
> reconciliation; an absent `total_count` ⇒ incomplete (skip). (F5) the normal
> and force passes now share **one** delete budget so the per-occurrence cap
> cannot be doubled. (F6) dry-run logs the **full** eligible inventory (not the
> capped heap) and the "bounded memory" claim is restated honestly (the deletion
> heap is capped; per-scan id sets are bounded by the small scratch population).
> (F7) Zep-only (unattributed) drift is now counted (bidirectional contract).
> (F9) dry-run is a **pure inventory** — no ledger reconciliation, no force
> deletion, no mutation. (F10) a **tombstoned** ledger record (deleted_at set)
> no longer corroborates a re-minted id's age, and `record_created` clears the
> tombstone on re-incarnation. (F11) the 404-as-success delete branch is pinned
> by a focused test. Deployable invariant (ships dry-run; #72 closes at OP1)
> unchanged.
>
> **Revision 7 (2026-07-27), after the THIRD external review round (3 high + 3
> medium, all confirmed):** the r6 fixes still left three unsafe edges plus a
> false memory claim and stale docs. **Sweeper:** the r6 completeness gate
> (`distinct_ids_seen >= total_count`) trusted Zep's `total_count`, so an
> UNDER-reported count + a dropped page still closed a live ledger entry (F1),
> and `distinct_ids_seen` retained every listed id, scaling with the `merchant_*`
> store population (~13 MiB at 100k, F6). Both are replaced by **authoritative
> per-graph `graph.get()` confirmation** — a stale ledger id is closed only after
> Zep 404s it; an exists/error keeps it (`reconcile_unconfirmed`). No
> `total_count` dependence, no all-ids set (memory now O(scratch population)).
> **Scheduler:** (lock) `LOCK_TTL_SECONDS`(360) outlives the 300s interval floor,
> so a stale lease from a dead occurrence made the canonical successor exit
> empty; now the canonical-but-lock-held occurrence **reschedules** (chain never
> empties at any interval) and the `on_failure`/killed-horse re-seed **releases
> the dead occurrence's lease**. (liveness) `_job_exists` (hash presence) misread
> a FAILED/CANCELED job — whose hash RQ retains — as a live chain; replaced by
> `_job_is_live` (RQ status ∈ {queued,started,deferred,scheduled}). (phantom)
> the re-seed now **enqueues first, then CAS-sets** the marker, so a process
> death between the two can no longer leave a phantom marker (a CAS-losing
> re-seeder's orphan self-collapses via generation ownership). Docs (this file +
> SCHEMA §2.2/§2.4) corrected to match. Deployable invariant unchanged; no
> operator config change (the lock fix is self-healing at every supported
> interval, so the 5-min floor stands).
>
> **Revision 8 (2026-07-28), after the FOURTH external review round (4 medium,
> all confirmed):** the r7 fixes were sound but left three edges plus stale docs.
> **(F1) The 404 confirm and the ledger close were not generation-bound:** the
> confirm result carried a bare id, so a same-id `record_created` racing in
> between the `graph.get` 404 and `record_deleted` would tombstone the NEW
> generation (pull it from the active/merchant registries). Fixed by
> generation-binding the close — `_load_registry` snapshots each id's
> `created_at` (the zset score), and `record_deleted(..., expected_created_at=)`
> runs under a Redis WATCH that tombstones ONLY while the hash's `created_at`
> still equals the snapshot (a re-incarnation, new `created_at`, is left live).
> **(F2) A killed non-sweep job enqueued a maintenance orphan:** the killed-horse
> handler fired for every kill and (enqueue-first) enqueued a sweep occurrence
> before its CAS lost — churn under repeated paid-analysis failures. The re-seed
> now acts ONLY for a killed SWEEP occurrence (id-prefix gate). **(F3) The real
> killed-horse path had no committed regression:** the handler is now a
> module-level `on_work_horse_killed` (not a worker closure) and a forking-Worker
> self-SIGKILL test drives the genuine RQ dispatch (no fake Worker / direct call).
> **(F4) Docs:** TDD §3.4/§4 proof-obligation + failure-mode table and SCHEMA
> §2.2/§3 write-path corrected to the current (in-band killed-horse heal;
> per-graph-confirm + generation-bound reconciliation) behavior. Deployable
> invariant unchanged.
>
> **Revision 9 (2026-07-28), architecture change — the self-perpetuating
> scheduler is REPLACED by a dedicated Railway cron one-shot.** Revisions 2-8
> built (and repeatedly hardened, over four review rounds) an in-worker
> self-perpetuating RQ scheduler — unique-id occurrences, a `zep:sweep:next`
> generation marker, a `zep:sweep:lock` singleton lock, generation-ownership
> collapse, a killed-horse handler, a boot reconciler, enqueue-first CAS. That
> entire subsystem existed for ONE reason: PRD §3 accepted the complexity to
> AVOID making the operator provision a new Railway service. The operator has
> since opted to run a dedicated service, which removes that constraint — so the
> whole subsystem is deleted and the sweep now runs as a one-shot
> (`backend/sweep.py`) on a dedicated **Railway cron** service
> (`backend/railway.sweep.toml`, hourly). Railway's cron IS the scheduler; a
> missed run is visible in Railway's run history and alerts via the same Sentry
> Cron Monitor check-in (now wrapping a dead-simple one-shot, not a fragile
> chain). **Deleted:** `maintenance_queue.py` and `tests/test_sweep_scheduling.py`
> in full; the maintenance queue / `with_scheduler` / killed-horse wiring in
> `worker.py` (it drains only `analyze` again); the CI `redis:7` service (no
> real-Redis test remains). **Unchanged:** the sweeper core
> (`zep_graph_sweeper.py`) and the ledger (`graph_lifecycle.py`) — every
> revision 2-8 sweeper/ledger fix (fail-closed age, per-graph-confirm +
> generation-bound reconciliation, bounded memory, dry-run purity, tombstone,
> 404) stands. **§3.4 below is SUPERSEDED** (kept only as the historical record
> of the in-worker scheduler and why it was hard); SCHEMA §2.4's scheduling keys
> are removed. Still ships DRY-RUN; #72 still closes at OP1.

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

Algorithm (single listing pass; **memory bounded by the scratch population —
revision 7**: the deletion set is a `max_deletes`-sized heap; the only retained
id sets are the scratch-bounded `seen_matched`/`unknown_age_seen` + the
active-registry snapshot. Earlier revisions kept an all-listed-ids
`distinct_ids_seen` set for completeness — that scaled with the `merchant_*`
store population (F6) and is removed; reconciliation is now per-graph-confirm
(step 5), which needs no all-ids set):

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
   **Registry reconciliation — authoritative per-graph confirm (revision 7,
   F1/F6):** a ledger id in the snapshot but NOT seen in the listing is closed
   ONLY after `client.graph.get(id)` authoritatively 404s it. A `get` that
   returns a Graph (the listing dropped a page / under-reported `total_count`)
   or errors leaves the entry intact and increments `reconcile_unconfirmed`
   (surfaced + alerted). The sweep therefore never infers "gone" from listing
   completeness — so an under-reported `total_count` cannot authorize a
   destructive close — and it retains NO all-ids set (the earlier
   `distinct_ids_seen`/completeness machinery is gone). Only the scratch-bounded
   `seen_matched`/`unknown_age_seen` sets + the active-registry snapshot are
   retained, so scan memory is O(distinct scratch graphs), independent of the
   `merchant_*` store population. `total_count` is now a reported metric only.
   Reconciliation runs only on a real sweep (dry-run confirms/mutates nothing).
6. Emit one summary log line (`zep_sweep scanned=… matched=…
   eligible_total=… deleted=… failed=… skipped_dry_run=…
   skipped_unknown_age=… truncated_backlog=… oldest_scratch_age_s=…
   total_count=… ledger_drift=… zep_only_unattributed=…
   reconcile_unconfirmed=…`) and `sentry_sdk.capture_message` on: sweep-level
   exception, per-graph failure count > 0, `skipped_unknown_age > 0`,
   `reconcile_unconfirmed > 0`, or `oldest_scratch_age_s > 2 × ttl_hours`.
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
- ~~`ZEP_SWEEP_INTERVAL_MINUTES`~~ — **removed in revision 9.** The cadence is
  Railway's `cronSchedule` (`backend/railway.sweep.toml`), mirrored in code by
  `sweep.SWEEP_CRON_SCHEDULE` for the Sentry monitor and pinned to the toml by
  `tests/test_sweep.py`. An env knob that only *described* the cadence could
  silently disagree with it and make the monitor alert on healthy runs.
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

### 3.4 Scheduling

> **SUPERSEDED by revision 9.** The current design runs the sweep as a one-shot
> `backend/sweep.py` on a dedicated **Railway cron** service
> (`backend/railway.sweep.toml`, hourly `cronSchedule`, `restartPolicyType =
> "NEVER"`). Each cron fire calls `create_app()` then one
> `sweep_orphan_graphs(...)`, wrapped in the Sentry Cron Monitor check-in
> (`monitor_slug="zep-graph-sweep"`, kept so the existing monitor carries over;
> in_progress → ok/error, and a missed cron run raises a missed check-in). The
> monitor declares the SAME crontab string as the Railway cron
> (`sweep.SWEEP_CRON_SCHEDULE`, pinned to the toml by `tests/test_sweep.py`);
> `ZEP_SWEEP_INTERVAL_MINUTES` is removed, since a cadence env knob that only
> described the cron could silently disagree with it. A SIGALRM watchdog bounds a
> run to `SWEEP_MAX_RUNTIME_SECONDS` (300, the deleted RQ occurrence's
> `job_timeout`) because Railway never terminates a deployment and SKIPS a
> scheduled run while the previous one is still Active — one hang would otherwise
> silently end every later sweep. `worker.py` drains only `analyze`
> again; `maintenance_queue.py` and `tests/test_sweep_scheduling.py` are deleted.
> No `zep:sweep:*` keys, no marker/lock/generation/killed-horse/reconciler — a
> cron either fires or it does not, so there is no in-worker chain to die.
>
> The rest of §3.4 below is the RETIRED in-worker scheduler design, kept as the
> historical record of what it was and why (across revisions 2-8) it was hard —
> which is the motivation for the revision-9 move to a cron.

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
- **Generation disposition is the collapse mechanism (revision 5, refined in
  revision 6 — corrects the r2/r3 claim that the lock collapses chains):** the
  job body's FIRST action is a single atomic read of `zep:sweep:next`, which
  classifies the occurrence:
  - **canonical** (`marker == occurrence_id`) — the one live generation: sweep,
    then schedule the successor.
  - **duplicate** (marker names ANOTHER occurrence whose job is still **live** —
    RQ status ∈ {queued, started, deferred, scheduled}) — a replica race or
    manual enqueue, concurrent OR serial: exit **without sweeping or
    rescheduling**. This is the collapse. **Revision 7 (F3):** liveness is a
    RUNNABLE-STATE check (`_job_is_live`), not hash presence — RQ retains a
    FAILED/CANCELED job's hash, so the r6 `Job.exists` check misread a terminal
    occurrence as a live duplicate and blocked the re-seed for the hash's
    retained lifetime.
  - **dead** (marker absent — chain expired past its `3 × interval` TTL — OR
    naming a job that no longer exists (phantom from a crash) OR one that has
    reached a TERMINAL state, revision 7): **re-seed one fresh chain in-band**
    via the atomic claim, then exit without sweeping (the fresh successor sweeps
    next). Revision 6: r5 exited silently here, so a TTL-starved or phantom chain
    died with no in-band heal.
  - **declined** (Redis error on the read): fail closed — neither sweep nor
    re-seed; the Sentry monitor surfaces a persistent problem.
  Because the marker names exactly one occurrence (`reconcile_chain` `SET NX` +
  `_schedule_next` CAS-advance + the atomic re-seed claim all keep it single),
  duplicate chains collapse to one regardless of timing. **The r2/r3 text that
  the singleton lock collapses chains was wrong: on a single worker the lock is
  free between serial occurrences, so two queued duplicates would both sweep and
  both schedule successors — permanent duplicate chains (caught in review as
  the first-round F1).**
- **Singleton execution lock (secondary concurrency guard, not the collapse
  mechanism; self-healing under a stale lease — revision 7):** after the
  generation check, the canonical occurrence takes `SET zep:sweep:lock
  <occurrence_id> NX EX 360` to keep two *concurrent* replicas from sweeping the
  same generation at once. **`LOCK_TTL_SECONDS`(360) outlives the 300s interval
  floor**, so a lease left by a dead prior occurrence can still be held when the
  successor runs. A canonical occurrence that finds the lock held therefore does
  **NOT** exit empty (r6's bug — that killed the chain at the 5-min floor);
  because it is canonical the holder is provably a *dead* prior occurrence (a
  live holder would still own the marker, so no other occurrence could be
  canonical), so it skips the sweep this cycle but **schedules the successor +
  advances the marker** — the chain self-heals at every interval regardless of
  the TTL/floor relationship. The lease is also actively freed by the
  `on_failure`/killed-horse re-seed (below), so the successor usually need not
  wait it out. The winner releases the lock in a `finally` via **atomic Lua
  compare-and-delete only** (revision 3: no GET+DELETE — the lease can expire
  between the two and the DELETE would kill a successor's lock). `LOCK_TTL` is
  now a backstop; correctness no longer depends on it being shorter than the
  floor.
- **Self-perpetuation (revision 5 enqueue-first/CAS-advance; revision 6 F2 —
  decoupled from lock release):** in the `finally`, the canonical occurrence
  first releases its lock (**best-effort** — a Redis error on release is *not*
  fatal; the 360s lock TTL backstops a missed release and the successor runs an
  interval later). It then schedules the successor **whenever the sweep
  SUCCEEDED, regardless of the release result** (revision 6 F2 — r5 gated
  scheduling on a successful release, so a transient release error silently
  ended the chain). Scheduling **enqueues the successor FIRST**, then
  CAS-advances `zep:sweep:next` from its own id to the successor's
  (`_ADVANCE_MARKER_LUA`, only-if-still-mine) — never publishing a marker before
  the job it names exists. A failure to enqueue the successor is **raised, not
  swallowed** (F3): the occurrence fails so `on_failure` re-seeds; a swallowed
  schedule error would let the occurrence report success while the chain died.
  (On sweep FAILURE the successor is not scheduled here — the exception
  propagates and `on_failure` heals.)
- **Chain-death healing (revision 4 — corrected against RQ 1.16.2; the r3
  text overstated the in-band heals):**
  - **RQ failure callback:** occurrences are enqueued with an `on_failure`
    callback that re-seeds the chain when an occurrence *fails* (a caught
    exception or an RQ timeout) but its own `finally` could not advance the
    chain (its lock was lost, stolen, or lease-expired, so `_schedule_next`
    never ran). The re-seed is an **atomic compare-and-set** on
    `zep:sweep:next`, not a bare `SET NX`: while an occurrence runs the marker
    already holds *that occurrence's own id*, so `SET NX` would find the key
    present and decline, killing the chain (the revision-4 bug). The CAS
    claims when the marker is absent OR still equals the failed occurrence's
    id, then enqueues one fresh occurrence. **Scope, verified against the RQ
    1.16.2 source:** the callback runs inside the work-horse's `except` handler
    (in-process for a `SimpleWorker`); it does NOT fire on a hard work-horse
    SIGKILL/OOM (`monitor_work_horse -> handle_work_horse_killed ->
    handle_job_failure` never calls `execute_failure_callback`) — that case is
    the killed-horse handler's job (next bullet).
  - **Killed-horse handler (revision 6, F3 — the hard-kill in-band heal):** the
    Worker is constructed with a `work_horse_killed_handler` (rq 1.16.2 supports
    it; `handle_work_horse_killed` invokes it in the SURVIVING parent process).
    On a hard SIGKILL/OOM it delegates to the same `reseed_chain_on_failure` as
    `on_failure`, which **(revision 7) first releases the dead occurrence's
    singleton lease** (atomic compare-delete keyed to the killed id, so a live
    sibling's lock is never touched) and then re-seeds via the atomic CAS. So a
    hard kill IS healed in-band now (r4/r5 left it to the marker-TTL + next boot),
    and its stale lease is freed so the successor sweeps without waiting out
    `LOCK_TTL`. Firing for a non-sweep (analyze) killed job is a harmless no-op.
  - **Dead-chain heal on dequeue (revision 6, F3):** an occurrence that dequeues
    to find the chain **dead** (marker absent past its TTL, or a phantom marker)
    re-seeds one fresh chain itself (the `dead` disposition above), rather than
    exiting silently. Covers TTL-starvation the callbacks cannot see.
  - **Boot reconciler (revision 6 validates job existence; revision 7 validates
    LIVENESS):** worker start re-seeds unless the marker names a job that is
    still **live** (`_job_is_live` — RQ runnable status, not hash presence). r5
    trusted marker *presence*, so a phantom marker (a crash between the marker
    claim and the enqueue) was never healed by any later boot; r6 fixed that but
    still trusted a retained terminal hash; now an absent, phantom, OR
    terminal-hash marker is re-seeded via the atomic claim (racing replicas seed
    once).
  - **Independent watcher (the ultimate guarantee):** the Sentry Cron Monitor
    check-in (§3.2 step 7) alerts on a missed occurrence from outside the
    worker entirely. For anything the in-band heals cannot cover (worker wedged,
    scheduler thread dead, sustained queue starvation) this missed check-in is
    the operative heal, firing within one interval + grace (well inside the
    marker TTL). **No liveness property is attested solely by the mechanism
    whose death it must detect.**

**Proof obligation (revision 2, extended by revisions 3-4):** these mechanics
are covered by integration tests running against **real Redis and the pinned
RQ version** (§6, tests 12-16) — not asserted from documentation. Pinned
regressions: scheduling a unique-id occurrence while another runs must never
mutate the running job's record (r1); the `on_failure` re-seed must claim the
marker via CAS when it still holds the failed occurrence's own id (r4 — a bare
`SET NX` silently no-ops there and kills the chain), driven through a real RQ
worker; lock release must be atomic under lease expiry (r2). RQ 1.16.2 does not
run `on_failure` on a hard work-horse SIGKILL/OOM — **revisions 6-8 heal that
in-band via the `work_horse_killed_handler`** (`maintenance_queue.on_work_horse_killed`,
which runs in the surviving parent, re-seeds, and frees the dead lease); a
committed forking-worker self-SIGKILL test exercises the real dispatch
(revision-8 F3). The boot reconciler and the external Sentry Cron miss remain
the outer backstops.

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
| Occurrence raises / times out (caught by RQ in the horse) | RQ marks it failed and runs `on_failure` | `on_failure` re-seeds via CAS even when `finally` could not advance the marker because the lock was lost/stolen/expired (revision 4) |
| Work horse SIGKILL/OOM (`finally` skipped; RQ does NOT run `on_failure`) | The `work_horse_killed_handler` fires in the surviving parent (revisions 6-8) | `on_work_horse_killed` re-seeds the chain in-band AND frees the dead occurrence's stale lease (only for a killed SWEEP occurrence — a killed analysis enqueues nothing, revision-8 F2); the boot reconciler + missed Sentry Cron check-in remain outer backstops |
| Worker wedged / scheduler thread dead / sustained starvation | No occurrences run; nothing in-process can notice | Missed Sentry Cron check-in — the watcher is external to the worker (revision 3) |
| Duplicate chains (replica race, manual enqueue), concurrent OR serial | Only the marker-named generation sweeps + reschedules; every other occurrence exits at the generation check (revision 5, F1 — the lock alone did not collapse serial duplicates on a single worker) | Chains collapse to one within one interval |
| Successor enqueue fails after a sweep | `_schedule_next` raises (not swallowed); the marker is not left pointing at a non-existent job (revision 5, F3) | The occurrence fails ⇒ `on_failure` re-seeds the chain |
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
13. `test_on_failure_reseeds_chain` — occurrence raises; the `on_failure`
    callback enqueues exactly one fresh occurrence and refreshes the marker.
    A hard work-horse SIGKILL (which skips `on_failure`) is covered separately by
    `test_real_worker_sigkill_dispatches_handler_and_reseeds` — a real forking
    Worker whose horse SIGKILLs itself, asserting the wired
    `on_work_horse_killed` re-seeds + frees the dead lease — plus
    `test_killed_non_sweep_job_does_not_enqueue_orphan` (a killed analysis
    enqueues nothing, revision-8 F2). (Revisions 3/6/8.)
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

> **Revision 9 rewrote steps 1/2/3/6 for the cron service.** The sweep no longer
> runs in the worker, so the service the operator touches changed. Everything
> else in this runbook is unchanged.

1. Merge + deploy web and worker (same image), then **create the dedicated
   Railway cron service** in `mirofish` from the same repo/image with
   "Config-as-code file path" = `/backend/railway.sweep.toml`, and set the env
   that file lists (all five `Config.validate` vars — `SECRET_KEY`,
   `LLM_API_KEY`, `ZEP_API_KEY`, `WAKARU_API_KEY`, `WAKARU_INTERNAL_SECRET` —
   plus `REDIS_URL` and `SENTRY_DSN`; a missing one fails every run before it can
   check in). Sweeper ships with `ZEP_SWEEP_DRY_RUN` unset ⇒ **dry** (single
   parser rule, §3.2).
2. First dry sweep logs the full inventory: `total_count`, matched count,
   oldest age, and the **unknown-age list** (expected: pre-ledger orphans
   whose vendor timestamps parse fine will show proven ages; the unknown-age
   list should be empty or tiny). Operator reviews the summary (this is the
   "inventory" artifact; paste it into #72) from the cron service's run history,
   and confirms the `zep-graph-sweep` check-in landed in Sentry.
3. Operator sets `ZEP_SWEEP_DRY_RUN=false` on the **sweep cron** service (NOT the
   worker — it no longer sweeps, so setting it there changes nothing), then waits
   for the next cron fire. Also unset the retired `ZEP_SWEEP_INTERVAL_MINUTES`
   wherever it is still set (it is inert as of revision 9).
4. Next sweeps drain the backlog (≤ `ZEP_SWEEP_MAX_DELETES` per cycle);
   #72 AC-3 closes with the before/after `total_count` pasted into the issue.
5. **Unknown-age disposal (operator-approved path):** if any unknown-age
   graphs persist, the operator reviews the logged ids and disposes of them
   explicitly — `ZEP_SWEEP_FORCE_DELETE_IDS` (comma-separated exact ids,
   consumed by the next sweep, logged loudly, then unset). Automatic deletion
   of unknown-age graphs is never enabled.
6. Confirm steady state over 48 h: `oldest_scratch_age_s < TTL`,
   `skipped_unknown_age = 0`, no Sentry sweep alerts, and — revision 9 — the cron
   run history shows ~one run per hour with none skipped (a skipped run means a
   previous one was still Active; the SIGALRM bound should make that impossible).
7. Operator documentation task: record Zep DPA / deletion-SLA reference in the
   repo's `docs/integration.md` (PRD §5).
