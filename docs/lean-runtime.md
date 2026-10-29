# Lean Wakaru runtime: first removal slice

Implemented locally on 28 October 2026, based on PR #84 head
`b079ad453775886af26267fa8db967ae34abe7ef`.

For the subsequent combined integration/adaptive candidate and current evidence,
see [combined runtime qualification](combined-runtime.md). Counts below describe
the original first slice.

## Behavior

The public HTTP and persisted RQ function paths remain unchanged. The dispatcher
runs direct buyer-state/intelligence and unknown-abandonment handling without
loading simulation. Only an explicit `legacy_simulation` cart loads
`legacy_cart_recovery`; that process registers subprocess cleanup before running.
Old RQ payloads without a mode retain legacy semantics. No job is silently
reinterpreted as a new direct analysis.

The service and utility package initializers no longer import provider stacks.
Direct-mode readiness does not call Zep or the legacy LLM endpoint. An enabled,
merchant-scoped TypeSafe mode checks its local provider/model/key/path prerequisites;
the response key is `intelligence_configuration`, not a provider-health assertion.
Deployment-policy enforcement, journal operations and actual provider outcomes
remain checked per request. Pure direct projection reports `analysis: ok`.
Legacy readiness retains the bounded, sanitized provider probes.

## Artifacts

- `backend/requirements-core.txt`: direct HTTP/worker and bounded intelligence.
- `backend/requirements-legacy.txt`: simulation and legacy provider dependencies.
- `backend/requirements.txt`: compatibility aggregate used by existing deployments.
- `backend/Dockerfile.lean`: core-only image, defaults to `direct_v1`; the `hosted`
  target retains the existing reviewed-profile entrypoint.
- `ops/runtime/lean-files.txt`: explicit source allowlist used by the builder.
- `ops/runtime/verify_container.py`: disposable local HTTP/Redis/worker verification.

The lean image excludes simulation services/scripts, project/report storage,
sweepers, tests and fixture exports. It does not install torch, camel, oasis,
openai or zep_cloud. Minimal learning-serving dependencies are retained; training
policy extraction is a later cleanup. Config validation rejects legacy mode when
the adapter is physically absent. It does not migrate queued legacy jobs.

Build from the repository root:

```sh
docker build -f backend/Dockerfile.lean -t wakaru-lean:local .
python3 ops/runtime/verify_container.py wakaru-lean:local
# For the existing hosted profile contract:
docker build -f backend/Dockerfile.lean --target hosted -t wakaru-lean:hosted .
```

The verifier creates uniquely named local containers/network, uses synthetic
credentials and data, and removes its resources afterward. It does not contact
model, graph, merchant or email providers. It requires the Docker daemon and
`redis:8.2.9`. No host ports or persistent data volumes are created.

## Local evidence

- Both new architecture regressions reproduced before the extraction.
- 457 backend/intelligence/learning tests passed, zero skips, including real
  Redis reconnect and signed completion/restart/rollback fixtures.
- Existing simulation pipeline and scratch cleanup tests remain covered.
- Captured-mode and pre-mode queued payloads retain legacy dispatch after a
  process switches to direct mode.
- Fresh subprocesses execute signed HTTP/RQ projection with optional imports
  blocked, both from source and the physically reduced artifact.
- A built lean image passed signed HTTP intake, real Redis queue execution in a
  separate worker, polling, and direct readiness. The container also verifies
  that excluded dependencies and simulation source are absent.
- Ruff bug-class lint, actionlint and `git diff --check` passed.
- AST comparison confirms the five extracted simulation/cleanup helpers and
  observation-only projection are unchanged. GitNexus change analysis reports
  the expected HTTP, worker, startup and readiness paths. Its flow coverage is
  partial and its diff report omits new untracked files; those files were
  inspected directly and exercised by the artifact/container tests.
- CI gains a core-only test job and the container verifier. Hosted CI has not run
  for these uncommitted changes.

The final local Linux ARM64 image measured 280,381,238 bytes (about 280 MB,
267 MiB). There is no comparable legacy-image measurement in this change, so no
percentage reduction or latency/RSS improvement is claimed. The lean HTTP timeout
is 60 seconds; queued work retains the existing job timeout contract. Qualify
capacity and deadlines in the target environment before cutover.

## Cutover and final deletion gates

Existing Railway configuration and compatibility Dockerfiles continue to select
the full image. This change prepares an independently testable artifact; it does
not switch production or remove historical records.

1. Reconcile this slice with integration PRs #85-#92 and adaptive PR #93. Update
   the runtime allowlist for newly mounted capabilities and persistence source
   hashes in the C14 inventory. Rerun combined contract and privacy qualification.
2. Inventory every queue/registry and captured job mode, including queued,
   started, deferred, scheduled, failed and retained completed jobs. Missing-mode
   payloads count as legacy. Include persisted retry/requeue mechanisms.
3. Stop creating legacy jobs through an explicitly reviewed configuration
   change. Drain or explicitly disposition existing jobs using the compatibility
   workers. Do not attach a lean worker to a mixed queue: RQ cannot dispatch
   workers by the mode hidden inside a job payload.
4. Reconcile graph ledger entries, scratch directories and provider artifacts.
   Keep the compatibility sweeper and required credentials available until their
   owners have verified cleanup. A code deletion is not a data-erasure receipt.
5. Qualify the lean web/worker image against the combined stack, then switch
   the selected deployment artifact. Keep a tested rollback image and avoid
   restarting old jobs under changed semantics.
6. Once queue, caller and artifact evidence is complete, delete the legacy
   adapter, simulation services/scripts, obsolete requirements/configuration and
   their superseded tests. Preserve audit, replay, accounting and privacy tests.

Current state: the new runtime artifact is simulation-free and locally verified.
Legacy source remains isolated for compatibility and cleanup. Complete repository
removal and deployment depend on the operational evidence above.
