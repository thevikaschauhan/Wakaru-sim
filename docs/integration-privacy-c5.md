# C5 integration privacy, retention and erasure

C5 is a dormant, fixture-tested implementation for the C1-C4 integration inventory. Engine is based on C4 (`c39dfe8`); Wakaru, Inkwell and Web are based on their C1/C2 draft heads because C3/C4 changed only Engine. All four branches are `codex/integration-privacy` in isolated worktrees. Live ingestion, deployments and email sends remain disabled.

## Scope and ownership

| Inventory | Owner | C5 treatment |
| --- | --- | --- |
| Shopify customer projection revisions and observations | Engine | Backfilled reference index; closure erasure; observations cascade; read and insert fences; 90-day maximum metadata lifetime |
| Gorgias ticket/customer/message/event metadata and observations | Engine | Explicit provider references form a connected component; erase the component across generations; no message bodies or attachments exist in C4 |
| C2/C4 pending/running jobs, checkpoints, scans, hints, fixture records, outbox | Engine | Cancel work and clear cursors under connection locks; remove scan/hint/checkpoint payloads during cleanup; retain content-free request fingerprints and audit identifiers |
| Integration credentials and OAuth attempts | Engine | Store-wide erasure disconnects every integration, advances connection generations and removes credentials/attempts; ordinary disconnect continues C2 immediate invalidation |
| New integration frozen inputs, journals, results, plans, judgments, pending jobs | Wakaru | SQLite participant with atomic reference registration, immutable writes, generation/read fences, physical erasure, bounded export and explicit retention purge |
| New integration frozen inputs, plans, documents, previews, judgments, pending jobs | Inkwell | PostgreSQL participant with equivalent fences, tenant RLS, erasure/export and retention; no production grants |
| Future integration context cache and cleanup presentation | Web | Memory-only cache, late-response rejection, expiry and source/subject invalidation; completion requires Engine, Wakaru and Inkwell receipts |
| C1 contracts | All four | Byte-identical schemas, fixtures and manifest for `integration_privacy_task_v1` and `integration_privacy_receipt_v1` |

The receipt inventory is exactly `integration_c5_v1`. It is deliberately not called whole-customer or whole-application erasure. C1-C4 had no downstream integration ingestion, so there are no historical integration derivatives to migrate in Wakaru/Inkwell/Web. C8 must route every new integration-derived write through these stores or extend the inventory with an equivalent fenced adapter. A derivative's source keys must identify exact source revisions; its expiry must be no later than its earliest supporting evidence expiry. A cache or legacy writer must not bypass these boundaries.

Existing legacy Engine order/outcome ledgers, identity links, recovery requests and email history; Wakaru TypeSafe journals, learning registry exports, workflow files and Zep graphs; and Inkwell legacy build plans, email documents, immutable recovery/approval history, rendition blobs and external caches remain a separate migration inventory. Their existing deletion helpers do not establish subject coverage. C5 does not silently invoke tenant-wide legacy deletion or weaken their immutable audit triggers. They must not receive new integration evidence until C7/C8 supplies reference registration, audited scoped cleanup and dispatch fencing. Whole-application privacy fulfillment remains blocked until that inventory is covered. Provider/vendor copies, backups, exports delivered outside the process and browser sessions require their own completion evidence.

## Durable protocol

1. A trusted, tenant-bound caller requests erase or export. The request key and subject identifier are hashed; source IDs are represented by restricted SHA-256 keys over `provider + newline + account + newline + kind + newline + id`. Inputs reject ambiguous separator characters. These keys are pseudonymous, not anonymous, and require restricted access.
2. Engine locks the affected store's connections in UUID order, then its privacy scope. Normal page commits take a connection lock first. A privacy mutation cannot race a late page commit.
3. Erasure increments the store privacy generation, writes persistent tombstones for the explicit reference closure, cancels applicable jobs and invalidates capability coverage. This transaction commits **before** physical cleanup. Store-wide erasure also revokes credentials. A failing cleanup cannot roll back the stop.
4. Engine removes matching revisions and their observations, clears replay-bearing cursors/hints and emits its content-free receipt. Cleanup failure leaves the request pending. `ProcessPrivacyEngine` retries after restart.
5. `PrivacyTask` emits a strictly validated task with the immutable request ID, tenant/store, subject key, generation, operation, scope, source keys and inventory revision. `PendingPrivacy` returns bounded pending work. C5 mounts no delivery endpoint or scheduler and performs no service network calls.
6. Each participant commits its local stop before cleanup and rejects changed request content on replay. All writes require the current generation and unblocked subject/source references. Export contents are returned only to the trusted caller, never stored in the task/receipt. A later erasure invalidates earlier export work.
7. Engine binds receipts to the authenticated service identity and every stored task field. Missing, stale, cross-tenant, cross-request or wrong-operation receipts cannot complete the job. Duplicate matching receipts are idempotent. Completion requires all three server participants. Web neither acknowledges cleanup nor grants send authority.

Reconnection does not remove tombstones. New Gorgias objects that explicitly reference an erased identity inherit its stop, preventing replay through newly discovered ticket/message IDs. Uncertain or absent identity links are not email-matched. Database insertion triggers reject old binaries/direct writes that bypass the projector guard. A store tombstone also rejects reconnection to `connected` state.

Privacy ledger rows do not cascade from ordinary merchant/store deletion. Their deliberate removal is an operator privacy-policy action, not ordinary cleanup. Retain the ledger across schema rollback and backup restoration.

## Retention

No raw webhook bodies, support-message bodies or attachments are persisted by these adapters. The design's proposed 30-day excerpt limit is not activated: excerpt ingestion does not exist. Metadata/derivative retention is capped at 90 days. Replaying an artifact cannot extend its original expiry. Normal reads reject expired evidence even before a purge runs.

`ExpirePrivacy` processes at most 100 expired Engine revisions per invocation. It tombstones their exact revision hashes, removes their indexed records and propagates withdrawal to participants. It does not erase the customer identity, so a genuinely newer source revision can be admitted. This distinction has a regression test. Expiration makes coverage unavailable; C7 must preserve an unresolved hold rather than treating missing evidence as clearance.

Wakaru/Inkwell `purge_expired`/`PurgeExpired` physically remove expired artifacts and local derivatives sharing retired revision keys. Source keys are retained as minimal replay fences. These methods are explicit local calls, with no enabled scheduler. Export batches are bounded; oversized or unresolved work fails without issuing a completion receipt.

These are conservative fixture defaults. Merchant retention policy, backup retention, active-case hold policy and restricted tombstone retention require acceptance before live use.

## Signed intake and live gates

`AcceptShopifyPrivacyFixture` verifies a bounded raw-body HMAC, duplicate-free JSON, domain/store/merchant binding and stable delivery ID, then exercises durable intake for `customers/data_request`, `customers/redact` and `shop/redact`. Its topic must be selected by a fixed endpoint, not an untrusted header. It is not mounted. Email-only identities return `privacy_identity_resolution_required`; they must not be acknowledged as fulfilled. Orders listed in a provider payload remain covered by the explicit legacy-ledger gate above, not by a C5 integration receipt.

The topic/payload reference is [Shopify's official compliance webhook documentation](https://shopify.dev/docs/apps/build/compliance/privacy-law-compliance), checked 2026-10-23. No subscriptions, live callbacks or merchant requests were created. Gorgias source deletion can enter the same explicit-reference erase API after provider deletion/merge semantics are authenticated and verified; an uncertain 301/404 observation is not silently promoted to an erasure authorization. Revocation/disconnect immediately invalidates use through C2; retained history is withdrawn through the explicit C5 retention/erase APIs.

Before activation: complete the legacy inventory, map email-only requests through an authorized identity workflow, add authenticated service transport/retries, mount reviewed intake, grant separate least-privilege roles, adopt C7 dispatch and C8 writes, and verify provider-authorized sandbox behavior. No C5 receipt satisfies these gates by itself.

## Restore and rollback runbook

Keep ingestion, serving of restored integration context and sends disabled during restore. Restore the most recent independently retained privacy ledger before restoring artifact availability. Reapply pending erase/expire tasks and participant tombstones, run retention purges, verify zero matching derivatives and collect exact receipts. Repeat a backfill fixture and a stale-artifact replay against the restored ledger. Only then consider separately authorized serving. Restoring artifact rows without the newer ledger is not a valid restore.

For a restricted Inkwell migration runner without database CREATE privilege, an owner must first provision the `integration_privacy` schema owned by that runner. Migration 0058 fails closed without it. Runtime roles receive no automatic grants, and participant tables enforce tenant RLS even for their owner.

Do not roll migration 071 or Inkwell 0058 down while newer artifacts or deletion records exist. Older binaries are incompatible with the new reference and read fences; keep their consumers disabled. No general-purpose delete of immutable legacy audit tables is introduced.

## Validation

- Full historical Engine schema migration in fresh loopback PostgreSQL; non-owner runtime-role tests for tenant isolation, erase/export, partial cleanup failure, replay, exact-revision retention, signed intake, Gorgias in-flight page cancellation and reconnect.
- Real cross-repository local flow: Engine emits a task; Python Wakaru and Go Inkwell seed and erase synthetic derivatives, replay completion, and return receipts; Engine verifies them and completes the durable job.
- Inkwell PostgreSQL tests cover plans/documents/previews, restart/replay, cross-tenant rejection, expired reads, dependent artifact purging and cleanup permission failure.
- Wakaru SQLite tests cover physical deletion, restart, stale artifact restoration against a preserved ledger, export invalidation, failure/retry and retention.
- Web tests cover exact receipt aggregation, cache invalidation, late reads, cross-tenant rejection and fresh-revision admission after expiry. Full TypeScript and component suites remain required.

All evidence is local/hosted synthetic validation. It is not deployed health, provider delivery, merchant acceptance, full privacy compliance or permission to send email.
