# C15: explicit subject/source lineage and scoped legacy erasure

Status: offline implementation, draft review only. No application mounts,
worker registration, provider access, live ingestion, deployment or email sends.

## Reconciled baseline and C14 evidence

C14 PRs Engine #253, Inkwell #395, Wakaru #92 and Web #115 were merged by
subsequent work on 1 November. C15 starts at main, not the obsolete C14 branches.
The duplicate Inkwell migration 58 failure was fixed by main's 62-65 numbering.
Reconciliation PRs #258/#398/#99 have green application checks; Web #119 has
green Node 20/22 and contracts checks, but external Claude review rejects its
credential. That external check is unresolved and is not suppressed here.

## Boundary and reuse

C5/C14 receipts continue to cover their existing owned tables only. C15 adds
`integration_lineage_c15_v1`, a distinct registered-record accounting contract.
It does not widen C5 completion or certify whole-customer/application erasure.
C14 inventory and retained-ledger restore remain the baseline; a bound C15
addendum classifies new ledger relations without rewriting historical evidence.
The effective inventory also binds the exact prior and regenerated Inkwell
sqlc model hashes; external-copy ownership and blockers stay unchanged.
Native qualification takes a separately reviewed exact migration catalog.
Frozen harnesses and the historical rollback packet remain unchanged.
Existing authenticated C11/C12 receivers and C13 schedulers remain untouched.

The operator library is deliberately under scripts, outside application startup.
Identical copies and contract parity are required by the cross-service fixture;
no service acquires a runtime dependency on another checkout.
Engine and Inkwell use versioned PostgreSQL migrations and the same library.
Engine keeps its ledger in public; Inkwell uses the existing private
integration_privacy schema so its restricted runner needs no public CREATE.
Only the guard lookup table permits trusted owner RLS bypass; the other five
PostgreSQL ledger relations force RLS. No ordinary application grants are added.
Wakaru installs the same logical ledger in each explicitly supplied legacy
SQLite file. Web validates and summarizes receipts without mounting an action.
This is an offline adoption/cleanup seam, not a production erasure endpoint.

## Explicit attribution

An operator must opt in, provide a dedicated connection, a canonical merchant
UUID/store ID, and an exact reviewed merchant/store allowlist. No environment
credentials, merchant discovery, network clients or scheduler are created.

Aliases are typed, hashed references: Shopify customer, Gorgias customer,
anonymous ID or cart ID. Email is not an alias type. Each maps to one canonical
subject hash with an immutable evidence hash. Conflicting mappings fail.
A proof hash records provenance; it is not authorization or proof of identity.
Human-reviewed scope/source attribution remains required for legacy records
whose original schema lacks a store or canonical customer reference.

Adoption resolves an alias, checks actual merchant ownership, locks the legacy
row and freezes its payload hash, source keys and provenance. One raw object
cannot be reassigned across stores or subjects. Changes require a different
source revision/object, never mutation of adopted membership. Raw output
ownership also checks its parent attempt. No content is copied into the ledger.
The frozen record also retains its typed alias edge; receipts hash alias,
subject, source, proof and original payload-hash membership.

## State machine and concurrency

`bind -> adopt -> start -> drain -> receipt` is the explicit operator sequence.
Subject erase and exact source withdrawal are separate targets. Start commits
a durable stop and request identity before resolving the frozen member census.
The pending census cannot produce completion; attribution failure keeps the
stop committed and an exact retry can resume after metadata is restored. An unresolved/empty
census stays blocked, including erase before first arrival. New adoption under
a stopped subject/source fails. A different source revision remains eligible
unless its subject is also stopped.

PostgreSQL operations take one transaction advisory lock per merchant before
locking legacy rows. SQLite uses BEGIN IMMEDIATE. This orders adoption, stop
census and cleanup across stores. Row triggers never acquire the scope lock,
avoiding row-lock/scope-lock inversions. Triggers reject INSERT or UPDATE of an
adopted raw object, including moving the tenant/key or replay after deletion.
DELETE remains possible for existing retention jobs; unexplained absence is
blocked rather than treated as successful erasure.

Drain processes 1-100 members per transaction. A failed page rolls back only
that page; the previously committed stop remains. Pending work is prioritized
before blocked retries. Repeating the same request is idempotent; a changed
request with the same ID fails. Another committed C15 deletion of the exact
object can satisfy overlapping requests. Missing records alone cannot.

## Initial adapters and protected history

Engine/Inkwell: delete only `intelligence.raw_outputs` content for an exact
registered attempt owned by the merchant. Preserve attempts, results, selected
judgments, budget reservations, spending, uncertain provider claims and all
immutable decisions/assignments/delivery evidence.

Wakaru: delete registered `raw_outputs` content or `recovery_plans` payloads
from an explicitly supplied legacy SQLite file. Preserve the attempt/results/
selected/budget journal. The plan replay guard remains after content deletion.
Adapters require the appropriate replay guards to be installed and enabled.
A table created after SQLite installation must be explicitly enrolled before
adoption, and both PostgreSQL and SQLite raw outputs check parent ownership.
Existing writers are not automatically enrolled. Unregistered objects and
other persistence families remain uncertified.

Inkwell email documents, slots, sent/provider material, Engine profiles/events,
semantic stores, object storage, downloaded exports and external systems are
not erased by these adapters. Their C14 blockers remain. No generic cascade,
DeleteTenant call, email merge, provider cancellation or spend release exists.

## Receipt and restore

Receipts bind version, service, merchant/store, request/task hash and frozen
census hash. Counts and blockers describe registered rows only. A receipt is a
fresh operator observation, not an authenticated cross-service authorization.
Web requires all three services and exact expected bindings supplied by a
trusted caller. All activation booleans and whole-application completion must
be false. The trusted caller must collect fresh observations for each restore. This
unmounted JSON projection does not authenticate a receipt or establish its
freshness. A saved packet is not evidence of a fresh restore.

Every receipt rechecks completed members against physical payload storage.
A stale restored payload resets that member to pending.
Every adopted object freezes its full alias/source attribution fingerprint,
and each request retains its original census hash. Missing source edges, alias
proofs, stop fences or members fail closed rather than narrowing completion. C15 reads deny stopped
content even before cleanup. Restore the independently retained latest lineage
ledger after historical payload and before granting runtime access. Preserve
aliases, source edges, tasks, stop fences and member accounting across restore
and rollback. Inkwell Down deliberately refuses to drop the ledger.
A local restore rehearsal does not qualify managed backup retention, IAM,
external copies, live provider behavior or whole-authority rollback.

## Rollout gates

Before any runtime enrollment: review attribution evidence and operator role
ownership; authenticate adapters/receipts; wire existing subject/source intake;
qualify ordinary-reader holds and every producer admission path; enumerate
remaining immutable/external copies; validate retained-ledger archive ordering
and target-owner grants; complete independent review and approve deployment.
All PRs stay draft. No rollout or live action is part of C15.
