# M1c source cleanup and retained export

This document records the original local helper slice. Its subsequent [authenticated transport slice](knowledge-privacy-transport.md) adds a separate opt-in API and Engine client. Nothing is mounted at startup; workers, producers, deployment and whole-application privacy acknowledgment remain disabled.

## Physical cleanup

`cart_recovery.domain.privacy.purge_withdrawn(store, scope)` is an explicit trusted-operator boundary. Authorize the exact current Engine-issued scope before calling it. It locks the same retained subject fence as ordinary writes and erasure, deletes withdrawn source receipts, removes dependent objects and their links, and advances the data version atomically. An empty aggregate becomes SQL NULL. Repeating cleanup with nothing remaining does not advance the version.

The v1 aggregate requires complete assertion revision chains. If any revision depends on a withdrawn source, cleanup removes that assertion's entire chain and any dependent assertions. Unrelated assertions, source receipts, catalog bindings and command hashes remain. Remaining aggregates pass the normal provenance validator before commit. This conservative history removal is explicit; a replacement must use new object and assertion IDs with independently authorized evidence.

Object and assertion tombstones use the separate `purged-object` and `purged-assertion` HMAC namespaces in the existing `knowledge_retained.withdrawals` ledger. Its `source_key` column now carries opaque source, object and assertion fence keys. Keys are scoped by the row's `scope_key`. No object value, source ID or assertion ID is retained in plaintext. The unchanged checkpoint envelope carries all those opaque keys. Save and quarantine release refuse purged identifiers. Cleanup emits a normal `write` invalidation after changing payload/version.

A withdrawal receipt proves the historical withdrawal command was applied. It does not prove cleanup ran. Cleanup does not create or rewrite delivery receipts. Engine must continue withholding aggregate privacy completion until its own complete inventory protocol is qualified. Retention expiry still requires Engine to deliver withdrawal and an explicit operator to run cleanup; there is no automatic scheduler or new production endpoint.

Physical means removal from active PostgreSQL rows. PostgreSQL WAL, dead tuples, backups and external exports require their own retention/disposal controls. Catalog bindings and payload-free command hashes are not removed by source cleanup. This is not a whole-subject erasure operation.

## Retained metadata export

`export_retained_page(store, scope, family, limit=100, cursor=None, snapshot=None)` returns at most 100 rows from one family: `withdrawals`, `delivery_receipts` or `events`. It also returns current subject and tenant erasure state. It requires the exact current scope even after subject/merchant erasure, rejects quarantine, and requires migration 002 so a missing receipt ledger cannot silently produce an incomplete export.

Start one family without a cursor, then pass its returned `snapshot` to every subsequent family and page. Follow each signed `next_cursor` until null. Cursors bind scope, family, position and ledger snapshot. Changes to the subject fence or retained family counts invalidate the snapshot and require restarting the export. This relies on trusted application writes being append-only for those ledgers; it is not proof against privileged SQL tampering. Each page is serialized against normal writes, delivery, cleanup and erasure. Count queries may time out on large ledgers and fail closed; scale/latency remains unqualified.

Subject events include its merchant-wide erasure event but no other subject's events. Delivery exports include HMAC command/scope keys, request and scope digests and operation, with no raw command body. Existing active-data export covers payload, sources, catalog and save-command hashes separately. These helpers require upstream authorization and protected export storage. No export HTTP route or aggregate cross-service export protocol is added.

## Restore and rollout boundaries

A stale data backup fails the existing data-version gate after cleanup. Independently current checkpoints retain object/assertion tombstones, which additionally prevent manually relabeled stale aggregates from passing quarantine. A clean post-purge backup can be released with the matching current checkpoint. Tombstones remain pseudonymous personal metadata subject to governed access and disposition.

The v1 checkpoint still does not cover delivery receipts or the full invalidation event ledger. This slice does not qualify complete delivery-ledger recovery. Prior binaries do not enforce the new object/assertion tombstones: forward and fence-preserving rollback artifacts must be rebuilt and qualified before activation. The existing Engine C5 privacy receipt blocker stays in force.

## Local verification

`tests/knowledge/test_source_cleanup.py` exercises real disposable PostgreSQL: source and derived row removal, complete revision-chain removal, unrelated knowledge, terminal identifier reuse, rollback after injected failure, concurrent cleanup/erasure, scope and quarantine refusal, stale and clean pg_dump/psql restore, pagination/tampering/change refusal, after-erasure export and authenticated delivery replay. The broader knowledge suite and Engine order-source bridge are separate regression gates. Evidence is recorded in the coordinating workspace under `evaluations/wakaru-source-cleanup-20261030/`.
