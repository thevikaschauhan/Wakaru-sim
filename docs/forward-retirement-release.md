# Forward retirement candidate

Local qualification completed on 2026-10-29. Production deployment, production quarantine, and deletion are not authorized or performed.

The previous forward image admitted a signed request for an unapproved merchant with HTTP 202 because its default enabled direct analysis globally. The candidate now uses a shared admission policy in startup, readiness, and both submission routes:

- `RECOVERY_ANALYSIS_MODE=legacy_simulation` is the deny default. It never executes simulation.
- `RECOVERY_DIRECT_MERCHANT_IDS` admits only explicit canonical non-zero UUIDs. Empty means deny everyone.
- Global direct mode and malformed allowlists fail web and worker startup. Configuration drift returns 503 before queue or provider access.
- Authenticated unapproved or missing merchants receive the exact terminal `410 legacy_simulation_retired` contract. Request-body merchant claims cannot override the authenticated owner.
- Previously accepted direct jobs keep their captured mode during worker handoff. Legacy, missing-mode, and unknown-mode jobs fail without retries and retain their payloads.

The rollback artifact is unchanged. The forward overlay changes nine packaged files and adds no dependencies. The existing lean runtime excludes simulation providers, the legacy adapter, sweep entrypoint, and maintenance worker module. Inventory and the reviewed quarantine utility are packaged but never run at startup.

## Evidence

Evidence directory: `/Users/VikasChauhan/Desktop/vakaru-engine/evaluations/wakaru-forward-release-20261029`.

| Evidence | Result |
| --- | --- |
| Backend regression suite | 394 passed; 2 opt-in tests skipped |
| New authenticated admission tests | 10 passed |
| Real web/worker configuration matrix | 17 passed |
| Forward -> rollback -> forward container rehearsal | 20 phases passed |
| Engine real HTTP 410 and durable PostgreSQL handling | Passed against both images |
| Redis restart, pending direct handoff both directions, replay, legacy retention, maintenance quarantine, stale-plan rejection, backup restore | Passed using disposable fixtures |
| Dependency versions | All 55 unchanged |
| Privacy inventory | 23 writers, 18 existing blockers retained, no new families |
| Bug-class lint and whitespace check | Passed |
| Forward execution platform | Linux AMD64 under local ARM64 emulation |
| Native AMD64 forward qualification | Passed: 20 phases and 17 configuration checks on native AMD EPYC |
| Production Engine full-process rehearsal | 14 checks passed locally under AMD64 emulation; real startup, merchant authentication, signed HTTP, durable 410, two restarts and forward/rollback/forward |
| Production Engine native AMD64 qualification | 14 full-process checks passed in run 40858073058 |
| Production / provider / full integration-stack qualification | Not performed |

The two opt-in backend skips require the TypeSafe completion request fixture and a dedicated real Redis reconnect URL. They are not counted as passes. This slice separately exercised actual Redis/RQ and PostgreSQL through the container rehearsal. TypeSafe modes remain off in the image qualification; paid-provider behavior is not asserted.

The privacy addendum changes only the reviewed HTTP admission file's hash. It does not waive any retention, erasure, restore, or cross-repository gate.

## Pinned artifacts

- Wakaru source base: `b079ad453775886af26267fa8db967ae34abe7ef`, plus the inherited source manifest and this slice's manifest. Work remains uncommitted on `codex/wakaru-forward-release`.
- Forward local tag: `wakaru-retirement-forward:amd64-20261029`.
- Forward image ID: `sha256:6ba1a020755f197bc17ccfde2bfcc727a768acb7d7cfb1575954825dddbae4c1`.
- Forward runtime SHA-256: `e000e87934fb2f2746d302aaef0b2ca8e3029b7cd642b37c56be8a03bc32f2be`.
- Forward archive SHA-256: `2af4f4ca6b34a170112d273a59be35f46b00a7d08638dd098ff2bbb96c942cbf`.
- Rollback image ID: `sha256:2b53878860384b5654a9c29b56820064585b4d33d0f9199b2904238612eb2970`.
- Rollback archive SHA-256: `e7cb4b92ec800e2832be38c920fa89ee325d4213fc7c26d5d06abc56ff9aaec5`.
- Engine source base: `107fde1778a0332d8cdaf6e3bf8f1f29e5e919dd`. The three retirement production sources match the prior qualified binding. The exact prebuilt AMD64 Engine test binary and migrations were reused and checksummed. A production Engine image was subsequently built and packaging-verified locally in `evaluations/engine-retirement-artifact-20261029`; its full-process integration rehearsal now passes locally under AMD64 emulation in `evaluations/engine-retirement-process-20261029` (14 checks). Native execution of the exact production Engine archive and all 14 full-process checks passed in run 40858073058; evidence is in `evaluations/engine-retirement-native-20261029`.

Image IDs above identify the local exported artifacts. No registry digest or hosted release publication is claimed. See `artifact/build.json`, `artifact/export.json`, `engine-binding.json`, and `slice-files.json` for full bindings.

## Read-only Railway refresh

Web, worker, and sweep still reference deployed commit `94375258e6b7da542d975c72df28c44d76c9035f`; there are no staged service configuration changes in this project. Web and worker are native x86_64. Both completed a stable two-pass Redis inventory: analysis queue empty, one scheduled obsolete maintenance job, no missing job-hash references. Quiescence is not proven by a snapshot.

Web has one valid approved merchant; worker has no direct allowlist. The candidate rollout must preserve that exact web-approved merchant scope and explicitly configure the same policy on both new roles. Evidence records only the count and normalized-list hash, not the merchant ID. The hourly sweep remains configured (`0 * * * *`). No service was stopped, started, deployed, or reconfigured by this slice.

## Rollout checklist, not executed

1. Completed: this exact forward archive passed native AMD64 validation paired with the existing rollback archive (run 40852506614). Repeat qualification if artifact bytes change. Keep the Engine binary/source bindings, package versions, and archive hashes exact. Do not silently rebuild from floating base tags.
2. The exact Engine deployment artifact passed native AMD64 full-process qualification (run 40858073058). Review the accumulated Engine/Wakaru source stack and reconcile the separate Engine attempt-token branch before combined-stack acceptance. The aggregate GitNexus review is high risk because it includes inherited app startup, workflow, and worker changes; local tests do not replace release review.
3. Obtain deployment authorization and an observation window. Recheck live source revisions, merchant-list hash, queues, active jobs, scheduler state, retained artifacts, and backups. Preserve the current approved merchant list; do not broaden it.
4. Pause intake and all legacy producers, including the hourly sweep. Stop old consumers and reconcile any in-flight work before handing queue ownership to the candidate. Do not mix old maintenance consumers with the retired runtime.
5. Roll out the qualified Engine 410 handler before reopening intake. Install the pinned Wakaru web and worker artifacts with the deny default and the same approved merchant scope. Keep TypeSafe and integration activation controls unchanged unless separately qualified.
6. Once producer/consumer fences and a fresh inventory are proven, preview the exact obsolete maintenance job again. Any authorized quarantine must use a fresh, reviewed plan for the current Redis instance and preserve the job, payload, and receipt. No deletion is part of this release.
7. Reopen approved intake only after startup, readiness, owner isolation, direct completion, exact terminal 410 behavior, and queue ownership checks pass. Monitor retries, failed-job retention, scheduler resurrection, and memory continuity.
8. For rollback, pause intake and transfer ownership to the pinned retirement-preserving rollback, keeping the same merchant scope and producer fences. Never revert to the old globally permissive image or an Engine version without terminal 410 handling. Reconcile durable state before any separately authorized restore; Redis restart invalidates old maintenance plans.

Native AMD64 forward/rollback validation passed in GitHub run 40852506614. The Engine production image full-process integration rehearsal passed locally under AMD64 emulation (14 checks). Native AMD64 qualification of the exact production Engine image and rehearsal also passed (run 40858073058, 14 checks). The next slice is integration-stack source reconciliation and combined qualification. Full integration-stack acceptance and later source/data cleanup remain separate slices.
