# Combined Wakaru runtime qualification

Local source and container qualification completed on 28 October 2026. This is
the second lean-runtime slice, in branch `codex/wakaru-combined-runtime`. Changes
are uncommitted. The original lean, integration and adaptive worktrees are preserved.

## Inputs and reconciliation

| Input | Pinned revision |
| --- | --- |
| PR #84 baseline | `b079ad453775886af26267fa8db967ae34abe7ef` |
| Integration stack through PR #92 | `23fa11b0153b34a2e6097b982cb1f801f434b6b6` |
| Adaptive PR #93 | `acc704663b774943e0e9219dacd53cf1bfd524ea` |
| First lean slice | Local patch and new-file hashes in `verification/combined-runtime/inputs.json` |

The integration and adaptive changes were applied locally from their shared
`94375258e6b7da542d975c72df28c44d76c9035f` base. The requirements conflict was
resolved by keeping the core/legacy split, jsonschema 4.26.0 with format extras,
rfc8785 and psycopg. PR #84 accounting and retention fixes remain in the baseline.
No remote branch was merged or changed.

## Fixed combined-runtime failures

1. The first lean allowlist omitted the newly mounted adaptive package. Starting
   the packaged app reproduced `ModuleNotFoundError: cart_recovery.adaptive`.
   The allowlist now includes the adaptive/integration runtime, policies and
   contract files required at import and execution time. Offline fixtures,
   simulation, tests and migration tooling remain outside the runtime image.
2. Adaptive release hashing assumed the old source layout and failed on the
   packaged artifact's absent `backend/pyproject.toml`. It now hashes every
   allowlisted file plus the manifest itself, including HTTP/auth/config and
   split requirements. Source and image hashes match. Changing runtime config,
   core requirements or the judgment catalog invalidates the binding.

This changes the release artifact identity. Any target release must be qualified
and registered against the new hash. Existing authority, policy, lease and send
checks remain required. The hash covers shipped source and requirement declarations;
it is not a container digest or a resolved dependency lock.

Adaptive HTTP routes remain disabled by default. The integration service remains
a separate opt-in factory, with no automatic grants or production app mount.
The default lean image runs direct analysis and excludes torch, camel, oasis,
OpenAI SDK, Zep SDK and the legacy adapter. Compatibility source/images remain
available for existing queued jobs and cleanup.

## Privacy inventory boundary

`contracts/integrations/c14/wakaru-runtime-addendum.json` records 23 current
Wakaru writer files, their family mappings, both adaptive migrations and lineage
controls. It retains all 17 blockers from the unchanged canonical C14 inventory
and adds one blocked adaptive persistence family. Mutation tests reject new or
changed writers, a new migration, and removal of a retained blocker.

`python ops/runtime/verify_privacy.py` verifies this Wakaru-only addendum. Its
pattern scanner is a conservative source inventory, not proof of complete data
lineage. It does not widen the C5 erasure receipt, reconcile companion inventories,
or establish whole-application erasure. Cross-repository parity remains false.

## Verification

- 601 tests passed with zero skips, using real local Redis and disposable
  PostgreSQL 18 for adaptive journal/migration tests. Provider transports were
  fixtures. The 235 warnings are dependency deprecations, not skipped tests.
- Intelligence and integration contract manifests passed; recovery v2 passed
  70 conformance fixtures and verified 19 pinned artifacts.
- Built `wakaru-combined-runtime:audit-20261028` and verified signed HTTP intake,
  real Redis, a separate worker and result polling. The container also checked
  direct readiness, disabled adaptive routes and source/image release binding.
- Within that image, a synthetic authenticated integration request passed using
  SQLite. After scoped erasure and store/app recreation, stale context was
  rejected. This integration check uses Flask's test client inside the image;
  the direct queue check uses actual HTTP and separate processes.
- Image size: 328,723,373 bytes, approximately 313.5 MiB, on local Linux ARM64.
  No startup latency, memory, production capacity or legacy-image reduction claim.
- Ruff bug-class lint and Git diff whitespace checks passed. CI now includes
  PostgreSQL-backed adaptive tests, contracts, privacy verification and lean-image
  verification. Hosted CI has not run for this candidate.
- GitNexus reports 386 changed symbols and 67 affected flows across 113 tracked
  changed files, with critical aggregate impact for the combined stack. App
  startup, readiness, queue execution, integration auth/erasure and adaptive
  judgment/authority paths are covered by the checks above. The index explicitly
  drops some flows, and diff analysis omits new untracked files; graph results
  are lower-bound evidence, supplemented by direct review and executable tests.

Input provenance, runtime hashes and test output are saved under
`docs/verification/combined-runtime/`.

## Remaining gates and next slice

1. Reconcile the addendum with the companion C14 inventories and run the full
   cross-repository integration/restore qualification against this combined head.
   Existing integration-only results do not qualify the new combination.
2. Inventory and disposition legacy jobs across all queues, registries and retry
   stores. Reconcile retained graph/scratch/provider artifacts. Keep compatibility
   workers and sweepers until their owners confirm completion.
3. Qualify the target runtime, restricted database roles, retention/backup policy,
   release authority bindings, rollback and operational capacity before cutover.
4. Delete the remaining legacy source only after the queue/caller/artifact gates.
   Then continue the shopper/merchant intelligence refactor: explicit memory,
   context, preference and relationship ownership. This slice adds no new personal
   intelligence object model and does not claim that broader architecture complete.

No deployment, live merchant mutation, provider invocation or email delivery was
performed. Source validation, hosted CI, deployment and live acceptance remain
separate evidence levels.
