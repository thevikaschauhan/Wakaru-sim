# C11: projected Gorgias coverage, explicit enrollment and service authentication

C11 is an offline implementation on the C10 draft heads. It adds opt-in private
handler factories and proves their connected behavior with synthetic provider
responses, loopback HTTP and disposable databases. It installs no provider
transport, live worker, deployment, merchant enrollment or email send.

## Ownership and interfaces

| Owner | Added boundary |
| --- | --- |
| Engine | `customers` and `customer_tickets` scan modes; projection-derived support binding; sticky policy enrollment; queue-to-dispatch invalidation; private enrollment/binding handlers; signed fixed-origin client |
| Wakaru | `create_service_app`, default off; real bounded interpretation and owned privacy store behind tenant/store service authentication |
| Inkwell | `PrivateHandler`, default off; real bounded composition with a mandatory server-owned current-fact resolver and store-scoped authentication |
| Web | Shared contract/reference documentation and explicit branch deployment exclusion; no browser service credential, enrollment action or send control |

Private operations are `POST /internal/integrations/support/enroll`,
`POST /internal/integrations/support/bind`, `POST /internal/integrations/analyze`
and `POST /internal/integrations/compose`. Each destination exposes only its own
operations. Async analysis jobs and merchant UI APIs are outside these grants.
There is no disable-enforcement, provider-write, approval or send operation.

Factories require explicit dependencies and an enable argument. Their default
disabled state serves no routes. Existing production bootstraps do not invoke
them or read new activation environment variables. The offline hosts invoke the
same authenticated handlers; live bootstrap configuration, merchant identity
resolution and operational grant provisioning remain release work.

## Trusted coverage construction

`BindProjectedSupport` accepts only snapshot/cart/recipient/window binding fields.
Merchant and store come from the authenticated service grant, and subject comes
from the stored frozen context. No caller can supply `complete`, case status,
identity resolution or a verification timestamp. The trusted host remains
responsible for resolving the cart/recipient/subject association; this private
service interface must not be exposed as a merchant-authenticated API.

The builder runs under the same connection/privacy/support locks as persistence.
It requires current store/account/native-integration binding, one unambiguous
native Shopify customer mapping, a fresh completed customer inventory, a fresh
completed customer-filtered ticket inventory and a completed overlapping event
reconciliation covering both scans. The selected customer revision must have
been observed in that full inventory. A direct customer lookup cannot prove
uniqueness. Missing, remapped, conflicting or duplicate identities remain held.

The policy is `gorgias_account_customer_v1`: all tickets belonging to the explicitly
mapped Gorgias customer can hold the selected store's outreach, even when the
ticket's own store is unknown. This is deliberately conservative account-customer
enforcement, not ticket-to-Shopify-store attribution or permission to quote a
ticket. C4 account-wide ticket scans remain uncertain and cannot clear a hold.

Each selected ticket must occur in the completed filtered scan at its exact latest
revision. Previously known tickets cannot disappear because a newer list omits
them or changes their customer. Open cases hold; unknown/deleted/trashed/snoozed
or conflicting observations cannot grant clearance. Pending/failed/cancelled
jobs, unresolved non-message-list hints, uncertain event observations, source
expiry and stale scans withhold completeness. Work is bounded and over-limit
inventories fail without truncating into a clear result. Exact source keys join
the existing C5 support-state and binding inventories; no new untracked coverage
payload store is introduced.

Freshness remains bounded to one minute. Provider pagination consistency,
event retention, native mapping correctness and actual alias/merge behavior
still need authorized sandbox proof. A fixture pass is not that proof. Legacy
account-scope observations conflicting at the same source timestamp remain held;
the adapter does not discard conflicting history to force clearance.

Protocol references checked on 25 October 2026:
[Gorgias list tickets](https://developers.gorgias.com/reference/list-tickets)
documents `customer_id`, cursors, ordering and inclusion of trashed tickets;
[list customers](https://developers.gorgias.com/reference/list-customers)
documents cursor pagination. C11 does not infer snapshot-consistent pagination
from those references and introduces no network-capable Gorgias adapter.

## Enrollment and immediate invalidation

Enrollment requires a separate exact-operation grant and the literal supported
policy. Engine verifies active merchant/store ownership and the privacy stop.
It records policy, grant key ID and time, inserts the durable invalidation and
does not require healthy coverage before enforcing holds. Replay preserves the
original audit fields and generation. Promoting an old synthetic enrollment
advances its generation; migration alone never promotes it. There is no public
unenrollment operation. Disconnect, erasure and disabled service routes do not
remove enrollment or bypass the existing C7 dispatch evaluator.

A new regression reproduced an old binding returning `support_clear` immediately
after a refresh was queued. Migration 074 adds Gorgias job mutation triggers using
the existing dispatch advisory lock, generation and outbox. Acceptance, retry,
failure, cancellation and removal now fence old bindings before rebinding or a
worker run. Migration 075 adds explicit policy/actor/time metadata without seeds
or new runtime grants. Preserve both migrations and privacy/revision fences on
rollback; disable service mounts instead of restoring old authority.

## Authentication contract

`integration_service_request_v1` uses HMAC-SHA256 over a newline-delimited domain
separator, key ID, audience, merchant, store, method, exact request URI, integer
Unix timestamp and SHA256 of the exact request bytes. Authorization is
`Integration v1.<key-id>.<timestamp>.<hex-mac>`. The key configuration supplies all
tenant/store/audience authority and exact allowed operations; request bodies and
merchant JWTs cannot expand it. Keys are separate from merchant JWT/provider
secrets, at least 32 bytes, and provisioned per tenant/store/destination. The
server rejects unknown/duplicate keys at configuration time, duplicate HTTP
Authorization headers, wrong operation/audience/store, query tampering, body
tampering, future timestamps and requests more than 30 seconds old. Bodies are
bounded to 200,000 bytes; errors never echo source text or credentials.

Identical requests may replay within the short window because enrollment,
interpretation and composition use durable idempotency/revision fences. This
contract must not be reused for a non-idempotent operation without replay
protection. Rotation/revocation means replacing the configured verifier/key set;
there is no background secret fetch or automatic key distribution. Provisioning,
rotation, rate limiting and revocation across running instances remain rollout
requirements. The client pins a configured origin, rejects redirects and permits
HTTP only for literal loopback fixtures; other origins require HTTPS.

Go and Python consume the same checked-in synthetic signing vector. Go verifier
source is identical in Engine and Inkwell and qualification rejects drift. Web
only carries the reference vector; no real service credential reaches the UI.

## Qualification and limits

Run the existing qualification with `--include-wiring`, using the C10 disposable
loopback database rules and sibling checkouts. Apply the complete Engine schema
through 075. C11 adds strict named requirements to the C10 matrix and retains
source hashes, log/XML hashes, required-test skip rejection, real browser reads
and blocked release gates. All companion revisions are pinned by full SHA in CI.

The connected C11 journey uses the actual projection worker to build coverage,
then actual signed Engine-to-Wakaru and Engine-to-Inkwell HTTP. Reopening through
the projection worker prevents further analysis/composition. Separate database
HTTP tests cover missing tickets, duplicate identities, unreconciled work, stale
scans, revocation, unauthorized requests, current approved fact withdrawal,
support invalidation, erasure and replay. Wakaru restart preserves its frozen
result and later erasure rejects it. Inkwell's authenticated positive test composes
one approved paragraph and still returns review-only, email-disabled output.

The connected C11 journey, like C10, uses an empty approved fact set. The positive
paragraph proof is a separate Inkwell test, not a full provider-to-personalized-copy
journey. Inkwell adoption and invalidation delivery remain explicit fixture setup;
C11 does not finish the durable outbox/large-batch transport or the legacy privacy
inventory. Current fact resolution is an injected server dependency, not a newly
enabled production resolver. No production router, provider, model or sender was
contacted to establish this evidence.

Keep all PRs draft. The new branch is excluded from Web preview deployments before
its first push, and Inkwell CI excludes optional staging credentials. Remaining
gates include actual provider coverage, production configuration/auth review,
durable invalidation/retention and legacy privacy closure, target-role migration
and restore rehearsal, authorized shadow traffic, pilot deployment and controlled
delivery. None is satisfied by the C11 fixture report.
