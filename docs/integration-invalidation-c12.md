# C12: durable cross-service invalidation

Status: offline implementation on C11, draft PRs only. Live ingestion, production
route mounts, automatic workers, deployment and email sends remain disabled.
This design extends `integration-wiring-c11.md` and preserves C5 privacy, C7
support dispatch, C8 bounded context and C9 merchant review contracts.

## Ownership and transport

Engine owns source authority. Its existing transactional support outbox is the
publication point for coverage, support, enrollment and privacy changes. Migration
076 adds a trigger that commits a monotonically increasing store epoch and a
microsecond cutoff in the same transaction as each outbox insert/update. It also
seeds a pending fence for already enrolled stores. It does not enroll new stores.
One store watermark and two destination delivery rows coalesce pending work;
no customer identifiers, source keys, provider text or context payload are copied.
Existing C7 acknowledgements cannot remove the C12 ledger.

`integration_invalidation_c12_v1` contains merchant UUID, numeric store string,
epoch and cutoff_us. Integers are positive and bounded to the JavaScript safe
integer range. Unknown fields, duplicates, fractions, trailing JSON and oversized
bodies fail validation. The reference vector and named acceptance matrix are in
`contracts/integrations/c12/`; the Go codec is identical in Engine and Inkwell.

Engine's explicit `RelayInvalidationOnce` claims one merchant/store/destination
with a 60-second database lease, releases locks, then calls
`POST /internal/integrations/invalidate` through the C11 authenticated fixed-origin
client. The grant permits this exact operation for one tenant/store/audience.
Receivers commit their fence before returning an exact `fenced` receipt. This
receipt means withdrawal is durable, not that physical fanout has finished.
Only the current unexpired lease and matching receipt advance that destination's
acknowledgement. A newer source epoch cannot be cleared by an older receipt.

Lost replies, crashes and expired leases are retryable. Retries use persisted
exponential delays capped at 300 seconds, independently per destination. Concurrent
claimers use row locks and SKIP LOCKED. Receivers accept duplicate/older events
without regressing; contradictory epoch/cutoff pairs fail closed. No response body
or transport error text is retained. There is no automatic retry loop, discovery
scheduler, production key distribution or production router change in this slice.

## Immediate fences and bounded cleanup

The withdrawal is deliberately store-wide. A frozen Engine context whose `as_of`
is at or before the cutoff is unusable. This may invalidate unaffected customers;
correctness takes priority over unnecessary recomputation in this initial design.
Engine assigns both the source cutoff and authenticated context time. Consumers
must never accept caller-invented capture times. Clock rollback is conservative:
old contexts remain blocked until a new authorized capture exceeds the watermark.
A future subject-level protocol must retain equivalent completeness and fencing
proof before relaxing this scope.

Wakaru persists the watermark in SQLite. Its bounded service checks the original
input at submit, run, result read and final plan write, including a fence inside
the final write transaction. Cached plans and work that finishes after withdrawal
are rejected. Restarting the process/store cannot restore old context. Fresh
Engine recapture can produce new work, subject to existing expiry/privacy checks.
Generic privacy export remains historical retrieval, not current analysis.

Inkwell commits a watermark and durable fanout cursor under the C5 scope lock.
Registration, reevaluation, composition and merchant-copy reads reject withdrawn
input immediately. Merchant status hides the draft and reports awaiting_context
before fanout. A database trigger rejects new approval, validation and entry into
dispatch for linked invalidated documents. It preserves terminal settlement of
already-started or unknown delivery attempts; invalidation cannot retract an email
already handed to a provider. Engine's current support and privacy checks remain
authoritative at dispatch. This asynchronous transport is not a claim of globally
atomic provider dispatch.

`DrainInvalidation` processes 1 to 100 decisions per transaction, stores a UUID
cursor and resumes after restart. It clears old review authority and marks eligible
documents support_superseded/held. Completed, in-flight and ambiguous delivery
records retain their existing outcomes. Duplicate delivery does not reset progress;
a newer epoch resets the cursor. The fence remains after fanout completes, so late
writes and old approvals cannot revive withdrawn context. A missing input still
fails the existing privacy checks. No background fanout worker is started.

Web carries the shared reference design and acceptance contracts. Existing C9
reads show the fenced status without a new mutation or service credential in the
browser. Its branch is explicitly excluded from Vercel deployment.

## Privacy, operational gates and qualification

Invalidation is not erasure. C5 retention, tombstones and erasure still govern
payload deletion. Minimal merchant/store watermarks persist to prevent replay;
subject erasure does not delete them. Store erasure remains fenced. Scope/merchant
teardown cascades the metadata only after the owning lifecycle has made reuse
impossible. Do not prune/reseed epochs while old signed requests or artifacts can
reappear. Restore rehearsal, retention inventory and legacy artifact closure remain
required before production activation.

Run `scripts/qualify-integrations.py --include-invalidation` with all four sibling
checkouts, locked dependencies and separate disposable loopback databases. It
requires C10, C11 and C12 named tests, full schema through Engine 076 and Inkwell
0060, strict test reports with no required skips, byte-identical contracts/design,
source hashes, actual authenticated cross-service fixtures and connected browser
reads. The CI workflow pins companion commits by full SHA.

C12 tests cover duplicate/late receipt, newer epoch during delivery, concurrent
claims, lease expiry, lost HTTP response, tenant/auth failures, receiver restart,
1,101-decision fanout beyond the legacy limit, immediate copy/approval fencing,
dispatch settlement, monotonic replay, erasure, and invalidation during plan
creation. Fixtures use synthetic credentials and local PostgreSQL/SQLite only.

Release still requires provider-authorized coverage proof, complete legacy privacy
inventory, target-role migration/restore checks, production grants and route
review, durable scheduling/monitoring, key rotation/revocation, shadow traffic,
pilot approval and controlled delivery. A passed fixture report enables none of
these. All PRs stay draft and the live boundaries remain disabled.
