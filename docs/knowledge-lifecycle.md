# Durable knowledge lifecycle: local implementation

This follows the reviewed domain contract commit `78c0bd0`. PostgreSQL now stores
bounded shopper/merchant knowledge through `KnowledgeStore`. No API route,
worker, startup migration, retrieval provider or production database is connected.
The qualified runtime allowlist is unchanged. This is the local persistence
foundation of slice 3; cross-repository activation remains gated.

## Ownership and interface

Engine must resolve the authenticated merchant/store/subject identity and current
identity/privacy generations. A trusted adapter supplies approved source receipts
and catalog bindings. `capture_source` verifies the receipt's content hash against
the supplied bytes and keeps the exact approved claims and source revision; it
does not approve merchant policy or infer that supplied content supports a claim.
The internal adapter, not an HTTP body or model, must establish that authority.
The raw source content remains with its existing owner.

All methods are internal privileged operations. No tenant may access the database
role directly. RLS/least-privilege roles and Engine authentication wiring are
required activation work, not covered by these local fixtures.

| Operation | Behavior |
| --- | --- |
| `provision` | Binds an Engine-issued identity; refuses erased merchants/subjects and stale generations |
| `capture_source`, `capture_entity` | Scoped source/catalog registration; immutable source revisions; withdrawn revisions cannot be reintroduced |
| `save` | Validates serialized v1 knowledge using stored receipts; atomic aggregate-version CAS and idempotent command receipt |
| `current` | Returns only current, effective, unexpired records with usable source/input lineage; retains contradiction edges |
| `export_subject`, `export_merchant` | Internal scoped export of stored history, sources, catalog and command metadata |
| `withdraw_source` | Retained stop excludes source-derived records from current reads and future assertions; unrelated future writes remain possible |
| `revise_identity` | Requires increasing identity and privacy generations; clears old payloads and refuses delayed old-generation writes |
| `erase_subject`, `erase_merchant` | Atomically retain stops and remove payload/source/catalog/command rows; includes previously unseen subjects/merchants |

Readers return a version, not execution permission. A command replay is a write
acknowledgment, not a current context snapshot. Context compilation and downstream
execution must independently recheck current scope, sources, expiry and Engine
authorization. No inference quality or conversion improvement is claimed.

## Transaction and history model

One subject aggregate inherits the domain contract's 64-object/128-edge limits.
Existing object content and lineage are immutable; status may become superseded
or revoked, and terminal status cannot be undone. Updates retain correction
history. Concurrent corrections with the same expected aggregate version produce
one winner and a stale-version refusal. A partial transaction cannot publish
payload, idempotency receipt or invalidation event independently.

The lock order is control, merchant, subject, then data. Subject operations
serialize through the retained fence; merchant exports/erasure take the merchant
lock exclusively. The durable payload-free event ledger records writes,
withdrawals, identity changes and erasure for later invalidation wiring. There is
no external event dispatcher in this slice.

This bounded aggregate is deliberately fail-closed at capacity. It is not yet an
unbounded lifelong event store. Before broader use, implement bounded historical
lineage receipts/compaction without losing evidence or privacy controls. Source
and catalog registries are capped at 256 entries per aggregate. Physical expiry,
backup retention, export disposal and retained-ledger retention policy remain
activation gates. Expired records are filtered from current reads but stay in
authorized history until erasure or a separately qualified retention operation.

## Privacy and restore

`knowledge_data` contains erasable aggregate payloads, source receipts, catalog
receipts and idempotency receipts. `knowledge_retained` contains HMAC-scoped
merchant/subject stops, generations, current data versions, source withdrawals,
payload-free invalidation events and a quarantine control. It contains no raw
shopper ID, policy text or preference values. These pseudonymous fences still
require restricted access and a retention policy.

The fence HMAC key is mandatory external configuration and must be stable across
restart/restore. Its fingerprint prevents silently opening the store with a
different key. Key loss or rotation requires a qualified migration; there is no
fallback to an empty key or generated replacement.

The SQL migration is explicit and starts **quarantined**. `bootstrap_empty` only
opens a genuinely empty new store. A data restore cannot use this bootstrap.
Restore data while quarantined, overlay an independently current retained
checkpoint, then call `release_quarantine`. That gate purges restored erased
payloads and refuses missing/stale active rows, wrong identity/merchant bindings
and unfenced data. An outdated checkpoint cannot regress existing retained stops.

The checkpoint digest is a binding check, not proof of freshness or authenticity
by itself. A trusted restore control plane must obtain it from the current
authoritative ledger, independently of the data backup. Copying a digest from an
old backup does not establish that guarantee. Full-database restores also require
external quarantine before any application connects. These operator/control-plane
requirements are unresolved activation gates, not hidden automatic guarantees.

## Qualification and remaining work

Tests use a newly initialized PostgreSQL cluster on a private Unix socket and
fixture-only databases. They exercise application and database restart, concurrent
corrections, transaction rollback, scope/source binding, expiry, revocation,
source withdrawal, identity revision, subject/merchant export and erasure,
erase/write races, actual `pg_dump`/`psql` restoration and fresh-database quarantine.
The cluster and fixture databases are stopped/removed by test teardown.

```sh
python -m pytest tests/knowledge tests/test_knowledge_contract.py
python ops/runtime/verify_privacy.py
```

The local C14 addendum registers the new writer and migration while retaining all
18 existing blockers. `wakaru.knowledge_lifecycle` is an additional blocked family
until Engine source/privacy adapters, cross-repository erasure receipts and
restore control-plane wiring are qualified. The shared C14 base inventory stays
unchanged. The inventory verifier now also refuses unreviewed knowledge migrations.

Next: wire the trusted Engine identity/source and privacy control adapters,
qualify cross-repository export/erase/restore, then package and test a new artifact.
Context compilation and memory-quality evaluation remain later gates. Do not
enable this store in startup or deploy it based only on this local result.
