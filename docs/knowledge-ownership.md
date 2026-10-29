# Knowledge ownership and migration boundary

Prepared locally on 2026-10-29 from the combined, native-AMD64-qualified baseline.
This is slice 2 of `wakaru-refactoring-review-2026-10-28.md`. Source mapping below
was checked against Engine `wakaru-integration-retirement` and its qualified
Wakaru companion. No database or provider migration is included.

## Authority stays with its existing owner

| Area | Owner | Domain contract relationship |
| --- | --- | --- |
| Merchant/store/shopper identities and revisions | Engine | Required trusted scope; Wakaru cannot infer identity links |
| Consent, suppression, eligibility, purchase state, cadence and execution | Engine | Rechecked outside knowledge; preferences cannot authorize actions |
| Commercial events, deterministic profile counters and source revisions | Engine | Referenced observations/outcomes, not recomputed by a model |
| Approved merchant facts | Application-owned approved source records, admitted through Engine authority | Exact claims and revisions; candidates/scraped text are not approval |
| Derived shopper/merchant knowledge | Wakaru | Scoped assertions and hypotheses, evidence lineage, expiry and conflicts |
| Judgment replay/accounting | Existing Wakaru intelligence/adaptive machinery | Separate immutable judgment records; no duplicate judgment store here |
| Composition and downstream delivery | Inkwell under Engine authorization | Future compiled context is evidence input, never permission |
| Optional semantic retrieval | Zep projection adapter | Rebuildable, non-authoritative projection; resolve results to current source records |

## Existing records and adapters

| Current source | Observed behavior | Required disposition |
| --- | --- | --- |
| Engine `models/shopper.go:ShopperProfile`, `services/shopper_profile_service.go:EnrichProfile` | Merchant/anonymous-ID keyed visits, journey/checkout state, purchases and message counters; `ShouldEnrollInZep` triggers on cart additions | Keep deterministic ownership in Engine. Do not turn counts into stated preferences. Resolve store and identity revision before creating scoped source receipts. No automatic backfill. |
| Engine `models/mirofish.go:ShopperProfileContext` | Recovery-time profile projection | Preserve existing reader compatibility. New knowledge is a separate contract, not an in-place reinterpretation. |
| Engine `services/zep_service.go:zepUserID`, `IngestEvents`, `GetContext` | Zep user ID combines anonymous ID and merchant ID; sends event summaries; returns up to ten graph facts as text or fallback strings | Existing key lacks explicit store/identity/privacy revision. Text has no admitted per-claim source receipt. Do not import it as authoritative knowledge. A later adapter must resolve scope and current source lineage; unavailable and empty results must remain distinct. |
| Wakaru `backend/app/services/zep_graph_memory_updater.py` and `oasis_profile_generator.py` | Simulation activity memory and generated agent profiles | Legacy simulation ownership only. No mapping to real shopper traits/preferences. Retain/dispose through the retirement inventory, not domain admission. |
| Wakaru `backend/app/services/graph_lifecycle.py`, `zep_graph_sweeper.py` | Scratch graph ownership/lifecycle accounting | Preserve retirement fences and retained artifact evidence. Not the future application knowledge store. |
| Wakaru `cart_recovery/intelligence/integration_contracts.py:read_contract` | Existing scoped recovery/context contracts, hashes, generations and feedback binding | Unchanged. Future context compiler must integrate new records through current invalidation and authorization rules. |
| `contracts/intelligence/v1`, integration bundle and recovery v2 contracts | Existing immutable buyer-state/intelligence/recovery/judgment interfaces | Byte-for-byte preserved in this slice. New domain objects do not replace their authority/replay requirements. |

Legacy memory text, enrollment flags and generated graph facts lack enough
provenance for automatic conversion. Missing mappings become explicit unknown
states or quarantine candidates. No cross-merchant global shopper profile is
introduced. Store bindings must be resolved, not filled with a guessed default.

## Next slice: durable lifecycle

Implement an application-owned persistence boundary before runtime activation:

1. Authenticated scope/source/catalog resolution; immutable source revisions and
   record IDs; deterministic idempotency keys; atomic per-assertion revision CAS.
2. Transactional corrections and revocations with source lineage. Concurrent
   updates must retain a conflict or reject a stale predecessor, never lose it.
3. Current identity/privacy-generation fences before reads and writes, including
   delayed jobs, expired sources and withdrawn approvals.
4. Subject/merchant export and erasure coverage, downstream projection/cache
   invalidation, retained fence ledger, and quarantined restore ordering.
5. Restart, retry, concurrent correction, source withdrawal, identity revision,
   late-write, erase/export and restore tests against disposable PostgreSQL.
6. Update the C14 privacy inventory and exact runtime allowlist only when those
   new writers and consumers exist; qualify the resulting artifact separately.

The context compiler and quality evaluation follow persistence. They must bound
retrieval, preserve unknown/conflicting evidence, freeze source-linked snapshots
and recheck authorization before use. Optional Zep integration remains a separate
evaluated adapter change. The existing deployment and production-data gates stay
closed.
