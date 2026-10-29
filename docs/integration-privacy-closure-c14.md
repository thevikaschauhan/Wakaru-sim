# C14: privacy inventory and migration/restore rehearsal

Status: offline fixture implementation stacked on C13. All PRs remain draft.
Live ingestion, production mounts/worker startup, deployment and email sends stay
disabled. No production database, provider, blob store, merchant or mailbox is
accessed by this slice. There are no new runtime grants or schema migrations.

## What closure means

`contracts/integrations/c14/inventory.json` is the shared reviewed register of
108 Engine, 55 Inkwell and 10 Wakaru adaptive SQL relations, including views and intelligence/private
schemas. Each exact relation has its column names, RLS/FORCE RLS state and one
owner family. The 29 families specify subject selectors, export, erasure,
retention, restore handling, source evidence and the remaining blocker. The same
manifest is checked into all four repositories. Migration hashes detect changed,
added or removed historical files. Reviewed persistence-source hashes currently
cover 139 files across all four repositories.

Inventory closure means these stores have a recorded disposition. It does not
mean whole-customer erasure, resolved legacy lineage or approved retention policy.
Eighteen families remain explicitly `legacy_blocked` or `external_blocked`.
A complete `integration_c5_v1` receipt still certifies only the C5 inventory.
C14 does not relabel it, replace it with a broader receipt or weaken immutable
assignment, approval, learning or delivery evidence to make deletion pass.

The catalog verifier fails on new/missing relations, changed columns or RLS,
missing family/strategy/evidence, migration drift and reviewed persistence-source
drift. The source scan includes new untracked writers and excludes tests/tooling.
This static review tripwire is a lower bound on dynamic sinks and remote copies;
it never establishes absence of customer data or vendor deletion. The explicit
external and legacy blockers remain authoritative.

## Inventory disposition and next implementation

| Family | Owner | Disposition | Required next work |
| --- | --- | --- | --- |
| `engine.projections` | Engine | scoped_fenced | C5 scoped completion only; historical order tables excluded |
| `engine.integration_control` | Engine | scoped_fenced | Provider-authorized credentials and retention policy remain activation gates |
| `engine.privacy_ledger` | Engine | retained_fence | Backup provenance and restricted ledger retention require operator signoff |
| `engine.support` | Engine | scoped_fenced | Authenticated host/grants and independent source reconciliation remain blocked |
| `engine.identity_legacy` | Engine | legacy_blocked | Missing independently verified subject/source cleanup and export evidence |
| `engine.behavior_legacy` | Engine | legacy_blocked | Missing independently verified subject/source cleanup and export evidence |
| `engine.orders_legacy` | Engine | legacy_blocked | Missing independently verified subject/source cleanup and export evidence |
| `engine.recovery_legacy` | Engine | legacy_blocked | Missing independently verified subject/source cleanup and export evidence |
| `engine.message_legacy` | Engine | legacy_blocked | Missing independently verified subject/source cleanup and export evidence |
| `engine.merchant_records` | Engine | legacy_blocked | Missing independently verified subject/source cleanup and export evidence |
| `engine.typesafe_legacy` | Engine | legacy_blocked | Missing independently verified subject/source cleanup and export evidence |
| `engine.system` | Engine | system_only | No customer fulfillment claim |
| `inkwell.artifacts` | Inkwell | scoped_fenced | Legacy documents, provider data and blobs require separate evidence |
| `inkwell.privacy_ledger` | Inkwell | retained_fence | Target restore ownership and ledger provenance need separate qualification |
| `inkwell.documents_legacy` | Inkwell | legacy_blocked | Missing subject/source coverage or exact external deletion receipt |
| `inkwell.merchant_records` | Inkwell | legacy_blocked | Missing subject/source coverage or exact external deletion receipt |
| `inkwell.typesafe_legacy` | Inkwell | legacy_blocked | Missing subject/source coverage or exact external deletion receipt |
| `inkwell.system` | Inkwell | system_only | No customer fulfillment claim |
| `wakaru.integration_sqlite` | Wakaru | scoped_fenced | Storage media and old exports need separate evidence |
| `wakaru.typesafe_sqlite` | Wakaru | legacy_blocked | No verified subject/source cleanup and export closure |
| `wakaru.plan_journal` | Wakaru | legacy_blocked | No verified subject/source cleanup and export closure |
| `wakaru.learning_registry` | Wakaru | legacy_blocked | No verified subject/source cleanup and export closure |
| `wakaru.redis_queue` | Wakaru | legacy_blocked | No verified subject/source cleanup and export closure |
| `wakaru.workflow_files` | Wakaru | legacy_blocked | No verified subject/source cleanup and export closure |
| `wakaru.zep` | Wakaru + provider operator | external_blocked | Provider access disabled; no vendor receipt claimed |
| `web.context_memory` | Web | scoped_fenced | Browser is not an erasure participant or a send authority |
| `web.credentials` | Web | merchant_only | Active session revocation requires authorized operations |
| `external.downloads_blobs` | Engine + Inkwell + Web + operator | external_blocked | No live provider/object store access or customer-wide completion evidence |

Detailed selectors, code hashes and per-family export/erasure/retention/restore
strategies are in the manifest. Notable boundaries:

- Engine identity helpers delete only their enumerated proof/link rows. Email
  similarity cannot extend the subject closure. Events, raw order/webhook JSON,
  customer snapshots, historical recovery and learning data need verified aliases
  and lineage before scoped cleanup can be claimed.
- TypeSafe `DeleteTenant`/`delete_tenant` and learning `delete_merchant` are tenant
  operations. They are not safe substitutes for a customer-level erasure. Add
  source/subject registration and late-write stops before admitting integration
  data to those paths. Existing new C8 context remains in C5-owned stores.
- Inkwell build plans, legacy documents, slots, embeddings, immutable review and
  delivery history, rendered-object keys and caches need separate scoped adapters
  and an approved treatment of retained evidence. Never issue a blanket tenant
  delete or disable immutable triggers for a subject request.
- Wakaru's plan journal, workflow/upload/OASIS/report files, RQ queued/running/
  deferred/failed/completed jobs and Redis graph registry need trusted ownership
  indexes and independently tested cleanup. A TTL or scratch sweep is insufficient.
- Zep graphs, object versions/CDNs, telemetry, delivered provider/recipient copies,
  independently retained backups and downloaded exports require exact operator or
  vendor evidence. None is contacted or certified by C14. Physical database/WAL/
  storage-media retention is separate from SQL row deletion.
- Browser integration context stays in session memory and has generation/source
  invalidation. Merchant credentials/OAuth state are a distinct inventory. Browser
  downloads and other active sessions cannot be recalled by a server C5 receipt.

For the next privacy implementation, first register trusted subject/source aliases
and immutable-record redaction/retention decisions, then implement bounded fenced
cleanup/export adapters with exact receipts and vendor/object ownership. Until
those proofs exist, the 18 blocked family IDs remain release/fulfillment gates.
This is an engineering inventory, not a legal retention-policy decision.

## Executable migration and recovery design

Run from Engine with sibling `engine`, `inkwell`, `wakaru` and `web` checkouts:

```sh
python scripts/test_integration_privacy_inventory.py
python scripts/rehearse-integration-privacy.py \
  --admin-dsn "$C14_ADMIN_DATABASE_URL" \
  --workspace .. --output /private/tmp/c14-evidence
```

Preinstall Go, matching PostgreSQL client tools, pgvector and the C13 locked Python
fixture dependencies. The cluster must be fresh and disposable. The script accepts
only an explicit `127.0.0.1/c14_admin_test` URL with no connection overrides. It
refuses existing fixed Inkwell roles, creates only fresh `c14_*` databases and
fixture roles, never drops existing data, and inherits only compiler/runtime
variables. Outputs must be outside source checkouts. Heads, dirty state, source
hashes and archive/ledger hashes accompany the report; source changes during a
run fail qualification. CI pins every companion checkout to an exact commit.

1. Apply the complete Engine migration chain with its real UUID backfill at 026.
   Seed a synthetic pre-067 Judge.me installation and verify its ciphertext and
   legacy AAD survive migration to shared credentials, with the old table removed.
   Remove bootstrap database CREATE before 067 through 077. The runner has no
   superuser, CREATEDB, CREATEROLE or BYPASSRLS privilege.
2. Use Inkwell's real Goose command, historical 0001-0007 bootstrap and declared
   capability-role fixtures. Revoke database/public-schema CREATE before modern
   migrations. Independently prove 0047 and 0058 stop at versions 46 and 57 without
   owner-provisioned schemas, then provision just those schemas and finish 0061.
   The private C5 tables have zero ordinary `inkwell_app` access. The dedicated
   historical SLI owner capability remains explicit; it is not granted to C14's
   runtime probe role.
3. Seed only synthetic Shopify projections, support enrollment, and six kinds of
   Inkwell artifacts. Take actual custom-format PostgreSQL backups before erasure,
   excluding mutable privacy/withdrawal ledger data. Execute an actual Engine C5
   erase, deliver its exact task to Inkwell and retain the newer ledger separately.
   Keep C6 request fingerprints, enrollment, epoch/cutoff and pending destination
   state, not only subject tombstones. A content-free checkpoint verifies Engine's
   exact current generation/epoch/cutoff before any replay mutation.
4. Restore pre-data and old payload data into distinct databases with `--no-acl`.
   CONNECT is revoked from runtime roles and PUBLIC. A runtime SQL access probe
   must fail with permission denied before it can invoke tables or functions. No application
   host, listener or worker starts. Verify the independently retained ledger hash,
   load it atomically, then restore post-data constraints/triggers. Restoring all
   constraints before their current ledger scope rows is invalid and was reproduced
   during implementation. Never disable production privacy/immutability triggers
   to admit old rows.
5. Give only the synthetic operator probe role explicit fixture grants. Before
   physical cleanup, actual service-store reads reject erased data even though old
   payload rows are present. Replay the exact erase task despite earlier completion,
   verify zero matching artifacts, reject old exports/late writes/projector backfill,
   preserve another subject/tenant, retain privacy and withdrawal fences, and check
   restored column/RLS shape. Engine emits no fabricated downstream receipt.
6. Wakaru uses real `sqlite3.backup` snapshots. Atomically install independently
   retained scopes, tombstones, requests and invalidation scopes while no host is
   serving. Restart `PrivacyStore`, deny stale reads, replay cleanup twice, verify
   physical deletion and unrelated-tenant preservation, then reject a late result
   and old context timestamp. Do not replace newer fences with old snapshot state.

The rehearsal uses a privileged fixture restore owner for schema/extension restore
and deliberately discards old ACLs. It does not qualify production restore-owner
attributes, security-definer ownership, PostgreSQL topology/version, extension
ownership, target grants or deployment. The report explicitly leaves
`target_environment_qualified` and `production_restore_owner_qualified` false.
Actual role and security-definer ownership must be mapped and rehearsed in the
separately authorized target environment before serving. No downgrade migration
or old-binary compatibility is asserted. Keep all consumers stopped on rollback.

## Web completion boundary and evidence

`integrationPrivacyClosure` is an unmounted read-only helper over the existing
validated C5 receipt reader. It reports the exact completion inventory and blocked
family IDs; whole-application erasure and restored-serving authorization always
remain false. A complete scoped receipt cannot remove legacy/vendor blockers,
authorize production restore, ingestion, enrollment or delivery. No new endpoint
or merchant action is exposed.

Required C14 evidence includes inventory mutation/strategy/source/hash failures,
remote/unrelated/connection-option URL rejection, stale/missing/tampered ledger
rejection, all three PostgreSQL fixture phases in each service with no skips,
Wakaru's real backup test, Web's receipt/cross-scope/cache tests, and the existing
strict C10-C13 qualification. Local results, hosted CI, target environment recovery,
provider deletion and actual customer fulfillment remain separate evidence levels.

Before any live release: close the 18 named fulfillment blockers with exact
proofs, accept retention/deletion policies, qualify actual roles/ownership and
backup provenance, review production mounts/auth/secrets, verify authorized provider
coverage, then obtain shadow/pilot and controlled-delivery authorization. C14
implements none of those activations.

## Combined Wakaru source reconciliation, 29 October 2026

The inventory now includes the combined lean, integration and adaptive Wakaru
candidate. The original 17 legacy/external blockers are retained, and
`wakaru.adaptive_semantic` is an eighteenth blocker. The canonical inventory is
byte-identical across four repositories, with 173 relations, 139 persistence
sources and 29 families. The Wakaru addendum is rebased onto this inventory;
its standalone verifier still does not certify cross-repository parity.

The rehearsal applies both adaptive SQL migrations under a restricted fixture
migration role in a separate database, verifies actual columns/RLS and denies
ordinary runtime grants. This proves schema inventory only. The PostgreSQL
backup/restore phases remain scoped to Engine and Inkwell C5 stores, and the
SQLite backup/restore phase remains scoped to Wakaru C5. Adaptive journal
backup/restore, source aliases, export and retained spend/fence handling still
require their own fulfillment qualification. No new erasure receipt is issued.

The source scanner excludes Wakaru's `ops/runtime` qualification/build tooling,
as it already excludes scripts, tests and contracts. Runtime writers under
backend and cart_recovery, including untracked files, remain in the scan.
The lean runtime bytes and its release artifact hash are unchanged by this slice.

The candidate is local and uncommitted. Hosted workflows still pin the published
integration-only companion commits. Publication and deliberate companion-pin
updates are required before hosted CI can qualify this combination.
