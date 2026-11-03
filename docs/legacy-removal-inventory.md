# Legacy simulation queue and artifact inventory

Observed 29 October 2026 IST (initial live reads at 28 October 19:40-19:42 UTC;
refreshed queue/provider collection began at 19:50:51 UTC; container inventory
completed at 20:04-20:05 UTC after explicit SSH-key authorization).
Scope: read-only discovery and removal preparation. **Simulation removal remains blocked.**
No jobs, retained files or graphs were deleted, retried or executed. No
deployments, application configuration changes or sweeper runs were initiated.
The only authorized control-plane change was registering a dedicated SSH key.

## Current deployment and findings

Railway project `mirofish`, production environment, has web, worker, sweep and
Redis services. The three application deployments identify commit
`94375258e6b7da542d975c72df28c44d76c9035f` (PR #83). The local combined runtime
candidate is a separate, undeployed tree based on `b079ad4`.

| Surface | Observed evidence | Removal implication |
| --- | --- | --- |
| Admission | Web `RECOVERY_ANALYSIS_MODE` unset; deployed code defaults to `legacy_simulation`; direct merchant allowlist contains one merchant | New simulation work can still enter. Empty queue is not a producer fence. |
| `analyze` | Registered queue, zero queue/intermediate/started/deferred/scheduled/failed/finished/canceled memberships; no analysis job hashes in two matching reads | No retained analysis jobs observed at this instant. Keep the stable job callable until admission and drain are proven. |
| `maintenance` | Registered queue with one scheduled job and matching job hash | An obsolete scheduler artifact remains. Reconcile explicitly before removing queue compatibility. |
| Old scheduler markers | No `zep:sweep:next`, `zep:sweep:lock` or other sweep keys observed in the refreshed scan | No marker cleanup indicated by this snapshot; revalidate at any later cleanup. |
| Worker | One worker hash reporting `analyze`, idle, RQ 1.16.2, recent heartbeat | No maintenance consumer observed. Three worker-related Redis keys are not three workers. |
| Redis scope | All three apps bind to the same Redis service, DB 0; keyspace discovery found no other populated DB | Shared infrastructure: do not flush Redis or remove the analysis queue wholesale. |
| Graph ledger | No graph records, active scratch members or merchant graph memberships | Diagnostic ledger is empty, not an authoritative provider deletion receipt. |
| Zep | One shared credential across the three services; visible graph listing empty on page 1 | No visible graphs observed. Historical provider copies and erasure remain unverified. |
| App filesystems | Both running replicas inspected over SSH; configured `/app/backend/uploads` absent; no simulation directories, SQLite or sidecars under `/app`; no matching simulation subprocesses | Current configured artifact roots are accounted for. Historical containers, external paths and future writes are not covered. |
| Redis persistence | Redis 8.2.9, 5 GB `/data` volume; one regular file, `/data/dump.rdb`, 1,248 bytes; no append-only directory, symlinks or special files | Snapshot content was not opened; backup contents and restore behavior remain unverified. |
| Sweep container | Completed cron, zero running replicas, no volume; unchanged deployment | No live container to inspect. Historical scratch is not proven absent; the cron was not started to gain access. |
| Existing sweep | Hourly cron; `ZEP_SWEEP_DRY_RUN=false` | Background mutation can occur independently. No freeze or quiescence is claimed. |

The retained job's callable is
`app.services.maintenance_queue.run_sweep_occurrence`, status `scheduled`,
origin `maintenance`, scheduled for **2026-07-28 21:06:47 UTC**. No `retries_left`
field was present. Its Redis-key SHA-256 reference is
`642519ca84168a24784573376fc6395b8f02178e2be76b422bcedcbdd3f4a1f0`.
Job arguments and customer identifiers are not included in the evidence.

Commit `a0055ec` (PR #79) deleted `maintenance_queue.py` and replaced the
self-perpetuating RQ scheduler with `backend/sweep.py` and Railway cron. The
module is absent from deployed commit `9437525`. This is therefore a stranded
old sweep occurrence, not a pending shopper analysis. Do not revive the old
scheduler or restore its module just to run this job. Any later cancellation
must revalidate the callable, status, key reference, memberships and scheduler
ownership immediately before an explicitly authorized, narrowly scoped cleanup.

## Retention surfaces and ownership

| Family | Producer / storage | Reader or cleanup | Required disposition |
| --- | --- | --- | --- |
| Analysis jobs | API -> `job_queue.py`; `rq:queue:analyze`, intermediate list, RQ registries and `rq:job:*` | `cart_recovery_jobs.run_analysis_job`; worker | Classify direct, explicit legacy, missing-mode legacy, unknown and unrelated jobs. Preserve direct work; drain or cancel legacy work only after admission is fenced. |
| Old maintenance occurrence | Historical `maintenance_queue.py`; `rq:scheduled:maintenance`, job hash, `zep:sweep:next` and `zep:sweep:lock` | Removed scheduler; current cron is separate | Account for the single retained occurrence and any related dependencies/results/markers. Never execute it as a cleanup shortcut. |
| Job results / failures / retries | RQ job hashes, registries, result/dependency keys; analysis result/failure TTL is 24 hours | Polling API and RQ lifecycle | TTL is not subject erasure. Inspect delayed, failed, finished, canceled, orphan and retry state before removing compatibility. Unknown payloads remain unresolved. |
| Idempotency | `idem:*`, including `__pending__` markers | `idempotency.py`; normal TTL | No keys observed. Preserve direct request deduplication. Do not delete all idempotency keys to clear simulation state. |
| Projects | `backend/uploads/projects/<project-id>`; project input and metadata | `ProjectManager`, legacy workflow cleanup | Establish path and ownership inventory on every relevant deployment before scoped removal. |
| Simulations | `backend/uploads/simulations/<simulation-id>`; state/config/profiles, actions, IPC and subprocess logs, OASIS SQLite and sidecars | Simulation manager/runner/IPC; legacy cleanup | Verify no running subprocess; inventory SQLite/WAL/SHM, logs and nested files. A crashed worker may bypass `finally`. |
| Reports | `backend/uploads/reports/<report-id>` and historical flat JSON/Markdown reports | Report manager and legacy cleanup | Verify report-to-simulation mapping; failed report lookup can leave files behind. |
| Scratch graphs | Provider `mirofish_<16 lowercase hex>`; `zep:graph:*`, `zep:scratch:active`, `zep:merchant:*:graphs` | Workflow cleanup and scratch sweeper | Reconcile provider and ledger independently. Delete only verified scratch graphs after approval; retain evidence for failures and unknown outcomes. |
| Ledger tombstones | Deleted graph records with 30-day TTL | Graph lifecycle and sweeper | Keep deletion evidence through its policy window. No ledger row is not proof that a graph never existed. |
| Persistent intelligence | Merchant store graphs (`merchant_*`), TypeSafe SQLite, plan journals, learning registry/data, integration and adaptive stores | Shopper/merchant intelligence and privacy paths | Preserve. C14 `legacy_blocked` describes unqualified privacy lifecycle, not disposable simulation code. |
| Historical copies | Redis RDB/volume backups, old container/image copies, exported datasets/reports, local operator copies, platform/application logs and provider copies | Infrastructure/provider/retention owners | Separate inventory and retention/restore decisions. This scan does not establish their absence or erasure. |

The stable queued function is shared by direct recovery. Missing `analysis_mode`
on an old job defaults to legacy simulation. The synchronous `/analyze` path can
also enter legacy execution, so draining only RQ does not fence all producers.
Persistent merchant graphs must never be swept based solely on being unused by
the simulation runtime.

## Repeatable read-only collection

`ops/runtime/inventory_legacy.py` is standalone and does not import the app or
RQ. Run selectors separately so an unavailable source cannot look like a full
pass:

```sh
python ops/runtime/inventory_legacy.py --redis
python ops/runtime/inventory_legacy.py --uploads /app/backend/uploads
python ops/runtime/inventory_legacy.py --zep
```

Redis and Zep credentials come from existing `REDIS_URL` and `ZEP_API_KEY`
environment variables. Do not put credentials in arguments, reports or logs.
Run the file selector **inside each inspected deployment**, with its actual
resolved uploads root. Running it on a developer checkout is not production
evidence. Missing roots, symlinks, special files, limits and read failures return
an incomplete result (exit 2), not an empty-success result.

The Redis collector uses raw bounded reads, including expired registry entries.
Do not replace it with `RQ Registry.get_job_ids()`: RQ 1.16 invokes cleanup from
that method. Payload classification rejects pickle globals/reducers/custom
objects and bounds decompression; unknown data is reported, never executed.
Only aggregate metadata is emitted. Two matching scans compare selected
metadata and job data, not every field, TTL, score, result body or heartbeat.
They cannot establish an atomic snapshot or exclude a transient intervening job.

The filesystem collector reads metadata and does not follow encountered
symlinks. It is not an atomic filesystem snapshot; it must be rerun after
producer/subprocess fencing, with mounts and resolved roots independently
verified. It does not claim ownership, content erasure, or backup coverage.
The provider collector lists graph metadata with bounded pagination and never
requests graph content or performs deletes. Listing completion is not a
provider retention or erasure receipt.

The user subsequently authorized a dedicated key. The Ed25519 key named
`wakaru-readonly-inventory-20261029` was created and registered, then verified
by listing registered keys and connecting with its explicit identity path.
Fingerprint: `SHA256:lV3ihFeLg8hBMwpLC0Oboka0yl1ARKjIFFZisFR9iqA`.
Its private file remains at
`~/.ssh/id_ed25519_railway_wakaru_inventory_20261029`, mode `0600`; key material
is not stored in this repository or the evidence folder. The key remains
registered. Its name describes this task; it does not impose read-only SSH
permissions. All remote commands used here were read-only.

The local evidence runner `collect-containers.py` passed the reviewed collector
in memory to `python -B`, without uploading files, importing the application,
executing jobs or writing bytecode. It checked the exact deployment and
environment IDs and matched the deployed configuration hash to commit
`94375258e6b7da542d975c72df28c44d76c9035f` before accepting the uploads root.

Web and worker each contained 200 regular files under `/app` (8,815,281 and
8,815,616 bytes respectively), with no symlinks or special files. Both expected
uploads roots were absent, with readable, non-symlink parent directories.
The standalone uploads selector still correctly returns incomplete for a
missing root; the separate deployed-config and parent-tree checks establish
the narrower observation that the configured root was absent in these replicas.
Neither container had `.db`, `.sqlite`, `.sqlite3`, WAL, SHM or journal files
under `/app`, or visible processes matching the known simulation scripts.
These checks are bounded observations, not proof of quiescence or a search of
every external path. No file payloads or process arguments are in the report.

Redis volume inspection used only `stat` and `find` metadata, never opened the
RDB, and found one 1,248-byte snapshot. The completed sweep had no running
replica and was not started. Environment checks before and after inspection
showed unchanged deployment IDs, no staged/applying work and no new failures.

## Ordered gates before code deletion

1. **Fence legacy producers.** Prepare and verify a release/configuration that
   prevents legacy admission across synchronous and queued routes. Include
   retries, old callers and deployment rollback. Production changes need their
   own approval; they were not performed here.
2. **Prove drain and ownership.** Inspect all registries, hashes, dependencies,
   worker execution and simulation subprocesses after the fence. Resolve the
   obsolete maintenance job separately. Retain the stable analysis callable for
   direct jobs; fail closed on unknown or missing-mode jobs.
3. **Close artifact evidence.** Current web/worker roots are now inventoried;
   rerun after the producer fence and reconcile any new artifacts. Resolve
   historical container and backup disposition. Reconcile provider graphs with ledger
   and deletion receipts where relevant. Document backups, exports and logs.
4. **Authorize exact cleanup.** Review a bounded deletion manifest and retention
   policy, preserving intelligence stores and privacy/withdrawal fences. No
   wildcard Redis purge, automatic unknown-job retry or arbitrary payload path
   deletion. Rerun collection after any approved cleanup.
5. **Delete simulation code and qualify.** Remove isolated simulation imports,
   APIs, scripts, dependencies and image assets only when the preceding evidence
   permits it. Reverify signed direct HTTP -> Redis -> real worker behavior,
   historical-job refusal and restore/rollback safety before deployment.

## Evidence and validation

Local evidence directory:
`evaluations/wakaru-legacy-inventory-20261029` in the outer Engine workspace.
It contains `inputs.json`, `deployment-metadata.json`, `live-inventory.json`,
`retained-job-detail.json`, `local-filesystems.json`, `container-web.json`,
`container-worker.json`, `container-redis.json`, environment before/after
snapshots and `verification.json`. Earlier `live-inventory*.json` files retain
their historical pre-key SSH error; the container reports supersede that gap
for the running replicas.
The five known local worktrees do not provide a readable uploads root; their
results are incomplete, not evidence that production files are absent.
The refreshed Redis scan found no unclassified keys. These are observations,
not a release approval or a whole-customer erasure claim.

Seven local tests pass with real disposable Redis and real RQ serialization,
including byte-preserving reads of expired registries, malformed/malicious
pickle refusal, scan races/limits, filesystem metadata handling, bounded
provider listing, and refusal to clean an occupied fixture database. Workflow
syntax and the existing privacy inventory checks pass. CI now runs this suite
against an isolated Redis service; hosted CI has not run for these uncommitted
changes. All 81 previously qualified runtime files match their recorded hashes,
and the original qualification worktree is unchanged. GitNexus's full diff
includes the inherited, uncommitted runtime work and reports high impact for
that larger stack; its bounded graph coverage is not deletion proof. This
slice's source comparison contains only the collector, tests, this runbook and
the CI workflow update.
