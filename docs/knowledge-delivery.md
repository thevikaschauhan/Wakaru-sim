# M1b: durable delivery receipts and scope transitions

This is a local, opt-in protocol boundary. `create_delivery_app` is disabled by
default and is not mounted in production startup or included in the runtime
allowlist. Migration `002_delivery_receipts.sql` is explicit. Nothing migrates
existing subjects or activates Engine producers automatically.

## Commands and receipts

The separate `wakaru-knowledge-delivery` audience grants exact POST operations
under `/internal/integrations/knowledge-delivery/`. Supported mutations are
`scope_sync`, `source`, `withdraw_source` and `erase_subject`. There is no remote
bootstrap, quarantine release, merchant-wide operation, arbitrary SQL, knowledge
save or downstream execution. Existing service-v1 remains unchanged.

A command binds an immutable UUID, full subject scope, operation, arguments and
source-admission deadline. The receipt binds SHA-256 of the exact request bytes,
not a subsequently reserialized equivalent. Rebinding the same scoped command ID
to different bytes, operation or scope revision fails. Subject/store/merchant
authority comes from the authenticated grant, never the payload alone.

The mutation and its payload-free retained receipt commit in one PostgreSQL
transaction. Existing store operations execute through a request-local wrapper
using that same connection. The original store transaction implementation is
unchanged. The new outer transaction takes the control row exclusively, serializing
with existing mutations, erasure and quarantine. This is a correctness baseline;
multi-tenant throughput has not been qualified and finer locking needs evaluation
before activation. Lock and statement timeouts remain bounded by the store.

`receipt` lookup requires both the lookup grant and permission for the original
operation. It binds command ID, hash, operation and scope and returns either
`applied` or `unknown`. An applied receipt is historical evidence of a committed
operation, not current data authority or whole-system privacy completion. It can
be looked up after identity changes or erasure without reapplying the operation.
An absent receipt stays unknown; it does not authorize retry or replacement.

Receipts contain HMAC scope/command keys, exact request and scope hashes and the
operation. They retain no raw command, source content or response payload.
They survive erasure of `knowledge_data` and have immutable SQL guards against
update, delete and truncate. They remain pseudonymous metadata with unresolved
export, retention and restore-control obligations.

## Scope transition policy

Engine's retained identity revision maps to Wakaru identity revision. Engine's
retained identity registry **version** maps to this protocol's Wakaru privacy
epoch. It is deliberately not Engine's admitted source privacy generation.
Registry version increases on identity changes, privacy-floor advancement and
fresh admission after a floor change, giving each change a monotonic wire epoch.

`scope_sync` can provision an unseen scoped subject or advance a live subject.
Identity may stay equal while privacy advances. Identity cannot decrease, privacy
cannot decrease, and an identity increase also requires a privacy increase.
Skipping intermediate epochs is allowed; old delayed commands then fail the
current retained fence. An equal scope is a no-op. Existing data/fence consistency
is checked before an advance so a restore gap is not silently repaired.

An advance clears the previous active knowledge aggregate, sources and catalog,
and increments its data version. This conservative policy requires re-admission
of knowledge under the new scope. A store-wide privacy generation currently
advances other registered shopper scopes too; retention and retrieval quality
must be evaluated before production use. This is not a graph-memory migration
or an instruction to discard production history.

Erasure can establish a stop before any provision arrives and can advance an
existing stop monotonically. A late provision/source cannot revive it. Replaying
an already-applied old command returns its historical receipt without executing
it or changing newer scope. A different old command is refused.

Source commands additionally validate the existing source receipt contract and
content hash. Their mandatory deadline must be live and no more than 30 seconds
away on admission, and is checked again before recording the receipt. Source
content is not retained by this boundary; existing `capture_source` stores its
receipt/claims. Acknowledgment is not a freshness guarantee for later consumption.
Control/withdrawal commands have no expiry so privacy work can finish after source
cleanup. `source` and `withdraw_source` require an already provisioned exact scope.

## Qualification and remaining gates

`tests/knowledge/test_delivery.py` uses real disposable PostgreSQL for concurrent
duplicates, immutable binding, transactional rollback on receipt-write failure,
privacy-only transition, correction/withdrawal, erasure before arrival, retained
lookup after erase/restart, wrong scope/grant, expired source and quarantine.
The Engine fixture launches `ops/knowledge/delivery_fixture.py` on literal loopback
HTTP with synthetic credentials and two separate disposable databases. It commits
mutations then deliberately replaces their acknowledgments with HTTP 503.

Engine persists `started` before sending once. A receipt lookup confirms `applied`
after restart; a missing receipt cannot move the command back to `reserved`.
The bridge also delivers erasure before an older withheld command. The old command
is refused and remains unresolved locally; no C5 completion is fabricated.

The new Engine outbox handles scope-control commands only. Its Shopify customer
projection is identity/consent evidence and cannot be converted into the currently
supported event/statement claim types by inventing observations. Typed event and
statement adapters, exact source-wire outbox storage/disposal, source correction
and withdrawal producers, and complete knowledge-specific privacy fulfillment
remain next work. No source ingestion, shopper communication or execution is enabled.

Existing restore checkpoints do not include this new receipt ledger. Quarantine
denial is tested; independent current checkpoint authority and full backup/restore
qualification remain blocked. C14 inventory, retained export/retention, database
roles, packaged artifacts, hosted CI, native AMD64 and staging/pilot acceptance
remain separate gates. Do not enable this protocol until those gates are closed.

The [delivery privacy addendum](knowledge-delivery-privacy-addendum.json) inventories the new retained relation and explicitly records the remaining export, retention, role and restore obligations. It does not close the base C14 inventory or whole-system privacy gate.
