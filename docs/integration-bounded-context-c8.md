# C8 bounded interpretation and review copy

## Scope and activation

C8 builds on Engine C7 `1d04ed664db44324bac8ea82ae11e0a973da325d`, Inkwell C7 plus its CI fix `dc49653bc0c5fda041ae0f7ad540d585cc75af75`, and Wakaru C5 `90b61cc1340a89b283042234750ee440e918bb8a`. Engine owns frozen forwarding and support admission, Wakaru selects relevant approved assistance, and Inkwell validates and stores new review copy. Web presentation remains C9. The C1 bundle and public v1 APIs are unchanged.

Nothing here activates live ingestion, provider refresh, deployment, models or email sends. The new Engine forwarding function is an explicit in-process seam. The Wakaru blueprint and Inkwell HTTP handler are optional private adapters and are **not mounted by production bootstrap**. They require injected authentication and privacy stores. No migration seed, runtime grant, scheduler, queue consumer or allowlist is installed. Existing v1 recovery continues through its existing path. A caller must never downgrade a rejected C8 request into that path.

The C8 path returns `review_required` copy, never an approved/sendable email document. It does not create a new abandonment episode, change ownership or holdout, or resume an old draft. C7's dispatch fence remains authoritative. C8 success is not proof of current support clearance or consent.

## Reused foundation and remaining provider limitation

Reuse C1 strict readers and canonical hashes, C5 privacy-owned artifact stores, C6 frozen context reservations, and C7 support bindings, revision CAS and permanent old-draft supersession. No integration evidence enters legacy Wakaru journals, TypeSafe stores, Zep graphs, Redis results, Inkwell email documents, immutable decision payloads or external rendition blobs in this path.

C4/C6 still cannot supply customer-scoped, complete Gorgias ticket coverage or authenticated sanitized message spans in production. The positive statement fixture is synthetic and does not change that limitation. A future producer must establish customer/store identity, author/visibility, sanitized spans, completeness, exact source-revision privacy keys and retention before it can supply this input. C8 rejects text that is not pinned by a reference inside the C6 context. Missing statement content produces no personalized claims.

## Wire and evidence authority

`bounded_context_c8_v1` wraps the exact `recovery_context_v2`, buyer snapshot hash, current cart product IDs, privacy source keys, and at most 30 minimized statements. A statement contains its complete existing evidence reference, text of at most 2,000 UTF-8 bytes, `author: customer`, `visibility: public`, and expiry. The text SHA-256 must equal the frozen reference's `span_hash`. Only `customer_statement` references with `copy_eligible` permission are admitted. Internal notes, agent text, attachments, unknown fields, duplicate keys and mismatched tenant/store/subject/episode bindings reject.

References must be exact members of the frozen context. Source updates cannot be later than its decision time; statement retention must cover the context lifetime. Selection additionally requires resolved identity, complete customer-scoped source coverage, and no known hold constraint. Historical order statements and product statements outside the current cart are excluded. They are never relabelled as a current-cart motive. Engine checks supplied product IDs against the frozen cart reservation.

The request ID is SHA-256 of the full JCS request, including statement bytes, product scope and privacy keys. It binds every input affecting selection. C1's combined context hash remains unchanged. Hashes supplement authenticated service identity; they do not authenticate a caller.

Wakaru uses a conservative deterministic relevance selector for shipping or product assistance. Unknown topics stay empty. A future semantic selector may rank only the supplied approved fact IDs and source IDs through the same validated interface. No arbitrary generated claims or motive prose is accepted. Selection produces `recovery_plan_v2` with `action: review`, `status: needs_review`, `mode: assist`, at most three approved facts, exact fact revisions/text, source references, the buyer snapshot hash, context hash, context expiry, and a canonical plan revision. No incentives, reasons presented as observed motives, model scores or authorization are produced.

Engine validates that plan before forwarding and repeats the current C7 check after analysis. A hold or source-generation change during analysis stops composition. Requests are reconstructed only from the existing C6 reservation, with its original cart fingerprint, timestamps, source keys, privacy generation and connection pins. Retries do not recapture evidence or fetch providers. The explicit callbacks run outside database locks.

Inkwell independently validates the plan, recomputes its revision, checks all references and exact approved claim text, and requires a current approved-facts resolver supplied by the authenticated host. Revoked or changed fact revisions reject. Review paragraphs contain only approved fact text and fact provenance; support statements are never quoted or interpolated into recipient copy. The output is structured review copy, not rendered HTML or a sendable document. Future UI rendering must escape text.

## C7 revisions and privacy ownership

`ComposeBoundedReview` requires an existing C7 work item in `review_required` at the caller's expected revision. It locks privacy scope before decision/document and work, verifies the original episode and current C6 input, and requires identical subject, privacy generation, source keys and expiry. A stale revision, awaiting/held state, completed/ambiguous decision, missing context or privacy stop fails closed.

The complete bounded request, plan, copy and decision/revision link are stored together as one C5 `document` artifact. Exact retries return the same artifact; changed content conflicts. No approval is inherited. C5 export, subject/source/store erasure, generation fences and exact-source retention cover these new bytes without weakening legacy immutable audit triggers. Old artifact exports are historical evidence, not current approval; C9 must check the current C7 revision before presenting an actionable review.

Wakaru sync and async admission share `BoundedService`. Submission writes a C5 `pending_job` under the request hash. A queue may carry only merchant/store plus this opaque ID, never statement text or the result. Explicit worker execution rereads C5 state, returns an existing frozen plan on retry, and fences the plan write after selection. Polling rereads C5 rather than returning a cached result. Erasure before work, during selection, or after completion prevents late writes and subsequent reads. No worker is installed by C8.

This closes the new C8 artifact path. It does not certify cleanup of pre-existing legacy documents, exports, provider copies or external browser state. Those remain separate activation inventory. Do not route C8 bytes into a legacy writer to bypass these stores.

## Private adapter contract

The optional Wakaru blueprint exposes `POST /analyze`, `POST /jobs`, and `GET /jobs/{store}/{id}` when an authorized host mounts it. Its authenticator must verify the existing service signature and resolve the merchant; body merchant IDs cannot override it. `BoundedService.run` is the explicit execution entry point. The optional Inkwell handler accepts a POST with store ID, original decision ID, expected C7 revision, bounded input and plan. Its injected authenticator must enforce the existing service permission; its fact resolver must use current approved, cart-scoped facts. Responses use `Cache-Control: no-store` and safe error codes. There is no browser or unauthenticated route.

Public Inkwell OpenAPI/Go and Web generated types are unchanged because these adapters are not registered public endpoints. Mounting them or adopting their output in public document/review endpoints requires a separate authenticated transport and public type-generation change before activation.

## Acceptance and next gates

Tests cover Python-to-Go canonical plans, input tampering and unsupported claims, private/agent text, order/product scope, prompt instructions in statements, tenant/store binding, sync/async equivalence, frozen replay, erasure during selection and late workers, C7 revision CAS, current fact withdrawal, privacy export/erasure, frozen Engine retries, and a support hold arriving during analysis. Cross-repository CI compiles the actual Inkwell fixture, runs Wakaru's actual interpreter and sends a C6/C7-backed Engine package through both. No provider is contacted.

Before live use: prove scoped provider ingestion and sanitized message production; mount authenticated transports with explicit permissions; install a privacy-owned queue worker and delivery acknowledgments; connect the authoritative current-fact resolver; review database grants; complete legacy privacy inventory; implement C9 merchant review and replacement-document behavior; and run separately authorized sandbox acceptance. Deployment and sends still require explicit authorization. All PRs stay draft.
