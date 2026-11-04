# Legacy simulation admission fence and maintenance quarantine

29 October 2026. **Prepared and locally tested; not deployed or applied to production.**
This slice continues the [read-only inventory](legacy-removal-inventory.md).
It does not remove simulation source, delete retained payloads, enable new
merchants, restart workers or run the sweep service.

## Candidate behavior

| Boundary | Behavior after this candidate is deployed |
| --- | --- |
| Signed `POST /api/cart-recovery/jobs` and `/analyze` | Resolve mode from trusted server configuration and authenticated merchant. If it remains `legacy_simulation`, return HTTP 410 with `error: legacy_simulation_retired`. Reject before idempotency claims, queue writes, enrichment or inference. Normal auth and rate limiting still apply. |
| Direct recovery | Explicit `RECOVERY_ANALYSIS_MODE=direct_v1` or existing authenticated direct allowlist membership continues to work. No automatic promotion of other merchants. Caller-supplied `analysis_mode` or merchant fields cannot bypass the server decision. |
| Existing direct jobs | The stable `app.services.cart_recovery_jobs.run_analysis_job` callable and captured mode remain compatible across a worker restart. Polling and tenant isolation remain unchanged. |
| Existing legacy / missing-mode / unknown-mode jobs | Refuse before constructing the cart or importing simulation providers. Record a payload-free failure marker, disable automatic retries and retain the job indefinitely for explicit reconciliation, even if its old `failure_ttl` was zero. Never reinterpret it as direct work. |
| In-process simulation dispatch | `_run_legacy` raises `LegacySimulationRetired` before importing the adapter or starting OASIS. The existing deterministic unknown-abandonment helper still does no simulation. |
| Old maintenance occurrence | Remains untouched until a separately authorized operator operation. The prepared tool previews by default and can transfer this exact scheduled job to canceled retention without running its callable or callbacks. |

The default configuration is intentionally unchanged: an unset mode still
selects legacy and now receives 410 unless the merchant is already on the direct
allowlist. This closes admission without silently expanding the direct pilot.
The retained full image still has legacy configuration/provider readiness
checks; the lean image requires direct mode. Their health endpoints alone are
not proof that every producer has been fenced.

Already-running old workers are outside this code change's control. A rollout
must account for their in-flight work and subprocesses; neither a new web
replica nor an empty queue proves the old execution has stopped. There is no
new environment switch that re-enables simulation in this candidate.

## Prepared operator tool

`ops/runtime/quarantine_maintenance.py` is an offline operator tool, excluded
from the lean runtime allowlist. It does not import RQ, the application,
historical callbacks or provider clients. Credentials are read from an existing
`REDIS_URL` environment variable and never written into a plan or report.

Its default reference is the SHA-256 of the Redis job key observed during the
inventory:
`642519ca84168a24784573376fc6395b8f02178e2be76b422bcedcbdd3f4a1f0`.
It accepts only a scheduled, overdue, non-expiring maintenance job whose safe
primitive payload is exactly
`app.services.maintenance_queue.run_sweep_occurrence` with no arguments.
It refuses unreadable/custom-object payloads, retries, active scheduler markers,
maintenance workers, dependencies, additional queue membership, unexpected
registry types, future/non-finite schedule scores and ambiguous state.

Read-only preview, for a later authorized operator session:

```sh
python ops/runtime/quarantine_maintenance.py > reviewed-maintenance-plan.json
```

The plan contains a job reference, retained-field digest, exact scheduled score,
Redis instance ID and database, and a preparation timestamp. It contains no
cart payload, job arguments, private key or Redis credentials. It expires after
15 minutes; a Redis restart or restored instance requires a new preview.

**Do not run the following in production under this slice's authorization.**
After a separate approval and independently verified producer/consumer fence,
the explicit apply form is:

```sh
python ops/runtime/quarantine_maintenance.py \
  --apply-reviewed-plan reviewed-maintenance-plan.json \
  --confirm-producers-stopped
```

The confirmation flag records the operator's prerequisite decision; it is not
itself proof that producers stopped. The tool rechecks the exact reviewed state
and uses Redis WATCH/MULTI to abort on concurrent changes. It changes only:

1. Removes the exact job's scheduling eligibility from `rq:scheduled:maintenance`.
2. Adds it to `rq:canceled:maintenance` with an infinite retention score.
3. Changes its existing job hash's status to `canceled`, preserving every other
   field, including payload, callbacks, timestamps and original no-expiry state.
4. Writes an immutable-by-tool receipt at
   `wakaru:retired-maintenance:<job-reference>` with the reviewed plan digest.

It does not issue DEL, FLUSH, EXPIRE, retry or execute the job, restore its
removed module, clear other queues, or touch graphs. The historical occurrence
had a success TTL of zero and a failure callback that could reseed the chain;
executing it to make it disappear is therefore specifically avoided.

Repeating the same successful, unexpired review checks the retained state and
returns `already_quarantined` without writes. A changed record is refused.
If a transport or execution error could follow a committed transaction, the
CLI reports **`outcome_unknown`**, not "nothing happened". Inspect the receipt,
job status and both registries on the same Redis instance before deciding a
next action. Do not blindly retry or restore scheduling eligibility.

## Retention and restore

Blocked analysis jobs, the canceled maintenance job and the new receipt belong
to the existing **`wakaru.redis_queue`** privacy family, which remains blocked
for complete subject-level lifecycle qualification. No new customer payload
copy is created by quarantine. The receipt contains metadata/digests only and
has no automatic expiry. Retention cleanup needs a separate approved policy.
Current Redis persistence/backup copies are not erased by either fence.

Keep an independently retained approved reconciliation manifest when performing
the future operation. On restore, keep producers/consumers paused, restore the
current privacy/withdrawal fences, and reconcile canceled/scheduled state before
opening the queues. A backup from before quarantine can reintroduce the old
scheduled occurrence. Do not restore the historical scheduler module or run
the occurrence to reconcile it.

## Future release order and rollback gates

1. Approve the exact candidate and target environment. Confirm the Engine
   treats 410 as a terminal retirement refusal under its existing attempt and
   eligibility rules; do not add automatic retry or silently rewrite payloads.
2. Fence old producer ingress and account for in-flight work and simulation
   subprocesses. Record which web and worker replicas own the queues.
3. Replace the relevant web and worker replicas with the reviewed candidate;
   verify no old producer/consumer or maintenance scheduler remains. Keep the
   existing direct merchant scope unless a separate expansion is approved.
4. Verify signed legacy-mode requests return 410 without new RQ/idempotency
   records, while an authorized direct fixture completes through the real
   worker. Check old payloads are retained as failed, without retries.
5. Collect a fresh read-only queue/artifact inventory. Review the exact
   maintenance plan; seek separate authorization before applying quarantine.
6. Record the post-operation receipt and inventory, then close historical
   container/backup/provider disposition before deleting simulation source.

If rollback is required, pause ingress and consumers first. Rolling back to an
older binary can re-enable the legacy default; do not reopen traffic merely
because deployment or health checks pass. Preserve canceled/failed records and
receipts, reconcile effects, and verify one tested owner before resuming.

## Validation evidence

The dedicated tests use signed Flask requests, real Redis 8.2.9, real RQ
serialization and forked workers. They cover both HTTP entrypoints, trusted
merchant scope, direct sync/async replay, old/unknown modes, retry suppression,
payload retention, malformed or changed maintenance state, transaction races
and lost commit replies. The existing privacy addendum was refreshed for the
two reviewed queue writers; all 18 upstream blockers remain.

The packaged lean-image check exercises actual HTTP, a separate Redis/worker,
historical-job refusal, immutable release binding and the existing integration
SQLite erasure path. It verifies absence of simulation provider dependencies.
Workflow syntax, bug-class lint and privacy checks pass locally. Hosted CI and
production behavior are not claimed.

Exact final counts, source hashes, image identity and logs are recorded under
`evaluations/wakaru-legacy-admission-fence-20261029` in the outer Engine
workspace, with a portable summary in `docs/verification/legacy-admission/`.
The earlier inventory worktree is preserved unchanged.
