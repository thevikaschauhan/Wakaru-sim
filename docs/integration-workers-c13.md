# C13: worker orchestration and integration health

Status: offline implementation on C12, draft PRs only. Live ingestion, deployment,
production mounts, email sends and production worker startup remain disabled.
This extends `integration-invalidation-c12.md`; existing privacy, support and
bounded-context authority remain unchanged.

## Explicit worker hosts

Engine and Inkwell share byte-identical `internal/integrationworker` source.
Construction starts no work. `Enabled` defaults to false, and disabled runners
allocate no timer, contact no service and require no credentials. A host must
explicitly construct a runner and call `Run(ctx)` or `Tick(ctx)`.

Enabled configuration requires an explicit merchant/store allowlist of 1 to 1,000
unique scopes, a 1 to 100 scope budget per tick, an interval between 10 ms and one
minute, and a step deadline between 1 ms and 30 seconds. The runner copies the
allowlist. It discovers no merchants or credentials. Each selected scope gets at
most one step per tick; a round-robin cursor advances past failures and scopes
in cooldown. Concurrent ticks and overlapping Run calls on one runner are fenced.
No detached goroutine is created by the runner. Cancellation joins the active
step; C12's acknowledgement/failure persistence may use a further bounded five
seconds to retain a committed receiver result after cancellation.

Engine's `NewInvalidationWorker` injects fixed-origin C11 clients for both
destinations per scope. Each step visits Inkwell and Wakaru independently, with a
five-second deadline per destination, through `RelayInvalidationOnce`. No provider,
model, enrollment, analysis, composition or send operation is reachable from this
worker. C12 database leases prevent duplicate active delivery across processes;
HTTP stays outside the lease transaction. A slow destination cannot block the
other indefinitely. Source epochs, receipts, backoff and expired-lease recovery
retain their C12 semantics.

Inkwell's `NewInvalidationWorker` processes one page of 1 to 100 decisions per
selected scope through `DrainInvalidation`. The C5 scope lock serializes pages
with erasure, registration, composition and newer fences. Page progress and cursor
commit together. Failure rolls back the page, then stores only the fixed code
`cleanup_unconfirmed`, attempt count and next retry time, with exponential backoff
capped at 300 seconds. A newer epoch resets its cursor/count/backoff while keeping
the age of an already pending withdrawal. Duplicate/older events preserve progress.

Both workers also apply a local per-scope cooldown after a failed step, capped at
one minute. This prevents a database outage from producing a hot loop when durable
backoff cannot be written. The scheduler cursor/cooldown are process-local; the
source of recovery truth is the durable lease, SQL backoff and cleanup cursor.
Restart may make one bounded probe before those durable gates are checked. No
worker discovers or processes scopes outside the host's fixed allowlist. Fairness
is bounded round-robin progress, not an availability or latency SLA under overload.

## Metadata and health contract

Engine migration 077 adds oldest-pending time, attempt/success timestamps and a
worker heartbeat to existing C12 delivery rows. Triggers update these in the same
transactions as source epochs, leases and receipts. A new event cannot reset the
age of earlier unacknowledged work. Acknowledging an older epoch cannot clear the
latest pending time. No new runtime grants or customer payload ledger is created.

Inkwell migration 0061 adds pending age, processed count, attempts, retry time and
worker/progress timestamps to the existing private invalidation scope. All seven
private tables retain zero ordinary-app access. The generated sqlc model is checked
in. Cleanup completion clears pending age; it retains the monotonic withdrawal
fence. A failed page never advances the cursor or processed count.

`integration_worker_health_c13_v1` is a read-only projection scoped to one
merchant/store/service, with a 30-second observation lifetime. It reports delivery
pending/retrying/leased/idle, expired leases, cleanup pending/retrying/complete,
retry counts, oldest pending age, progress and heartbeat timestamps. A heartbeat
is recent only within one minute and never proves provider coverage, completed
delivery, current consent, erasure or permission to send. Missing activity remains
unobserved; timestamps in the future or over one minute old are stale.

The shared fixture is `contracts/integrations/c13/health.json`; qualification checks
fixture/design/matrix parity and both shared Go packages for drift. The health
payload contains no customer identifiers, source keys, provider text, credentials,
lease tokens or document content. Error values are fixed codes. Healthy transport
and cleanup do not prove that new context exists or that outreach is authorized.

Wakaru's new `create_worker_service_app` factory extends the default-off C12 factory
with `POST /internal/integrations/worker-health`. Its response reads the persisted
SQLite epoch without creating a missing scope or reading customer artifacts.
Wakaru needs no physical fanout worker because its transactional read/write fence
already rejects withdrawn inputs and late results. Restart preserves that fence.
Inkwell offers the same authenticated service-health path. Both require a separate
exact C11 operation grant and an empty object request. Query/body tenant overrides
are rejected. There is no public worker-control endpoint.

Engine and Inkwell also expose unmounted `MerchantWorkerHealthHandler` factories
for GET `/integrations/worker-health`. Engine resolves the merchant's current
active store in its read snapshot. Inkwell requires its injected authenticator to
resolve the authorized merchant/store. Browser scope IDs have no authority. All
responses are no-store; auth/DB failures return unavailable rather than synthetic
healthy output. No production router or OpenAPI activation is included.

## Merchant experience

The existing integrations page adds a Context updates section. Delivery receipts
and draft cleanup are separate states. It shows unavailable or expired status
explicitly, hides data on tenant changes, and does not call a service with browser
service credentials. The browser uses its existing merchant credential for reads.
A completed Inkwell cleanup cannot be presented as current if its epoch is behind
Engine or Engine health is unavailable.

A change in delivery acknowledgement or cleanup state clears inspected copy and
reloads the older decision projection. This prevents fresh health from appearing
beside a stale review-ready decision. Reloading health never approves, sends,
enrolls, connects, retries work or changes provider state. Retry and lifecycle
actions remain unavailable in the merchant experience.

## Recovery and operation boundaries

An operator can call `RetryInvalidation` to advance only durable delivery backoff;
it cannot clear a live lease or advance an acknowledgement. `RetryCleanup` advances
only cleanup retry time; it cannot clear a fence, reset a cursor or approve work.
Neither capability has a public or merchant HTTP mount. Local worker cooldown is
still respected after an operator advances SQL backoff.

For a failed/stale worker: verify tenant/store configuration and grants, inspect
pending age/lease expiry/last confirmed progress, restore the dependency, then
resume the explicit host. A duplicate delivery or process restart must converge
through existing C12 state. Do not delete/reseed fences or mark receipts complete
to quiet an alert. Replacing the worker/verifier configuration is required for key
rotation or revocation; no automatic secret fetch or distribution is introduced.
Operator alert thresholds and workload sizing require a separately reviewed rollout
policy. An unavailable health endpoint must remain unknown.

No startup scripts, process managers, cron jobs, production variables or runtime
role grants are changed. Deployment previews for the new Web branch are explicitly
disabled before its first push; Inkwell CI excludes optional staging credentials.
Tests mount synthetic loopback hosts and start fixture workers explicitly.

## Required qualification and next gates

Run Engine's `scripts/qualify-integrations.py --include-workers` with all four
sibling checkouts, locked dependencies and separate disposable loopback databases.
It requires C10 through C13 named tests, full schema through Engine 077 and Inkwell
0061, fresh strict reports, source/evidence hashes, shared package/contract/design
parity, the existing six browser scenarios and six additional C13 browser scenarios
using actual authenticated receiver health and worker orchestration. Required tests
cannot skip. The C13 browser confirms zero browser API mutations, keyboard access,
mobile layout, immediate fencing before cleanup and withdrawal of stale status.

Additional tests cover disabled workers, fair scopes, shutdown without detached
work, database-outage cooldown, destination outage/backoff, expired lease recovery,
cleanup timeout/rollback, resumed 1,101-decision cleanup, duplicate/new epochs,
tenant/auth/freshness boundaries and restart. These are synthetic fixture results.

Release still requires legacy privacy/erasure inventory closure, target-role
migration/restore rehearsal, production host/auth/grant review, secret operations,
provider-authorized Shopify/Gorgias coverage, shadow traffic, pilot approval and
controlled delivery. C13 does not authorize any of those activities. All PRs remain
draft and live ingestion, deployment and email sends remain disabled.
