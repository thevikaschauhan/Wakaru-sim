# C16 authenticated offline lineage

C16 connects native TypeSafe output persistence in Engine, Inkwell and Wakaru to the C15 ledger. Wakaru also uses the service for recovery-plan selection and cache reads. An offline host explicitly supplies the client, receiver, service grants, dedicated connections and reviewed bindings. Application startup does not mount the receiver. Web continues to consume its existing bounded receipt projection.

## Authority and requests

The service uses the C11 HMAC protocol. A key binds one service audience, merchant, store and list of exact POST operations. Signatures cover the path and exact body bytes and expire after 30 seconds. Clients accept only an explicitly supplied IPv4 loopback origin. Each operation requires a valid grant and independent server-side attribution. A request cannot specify a store, alias, subject, source list, proof, address or database.

The server indexes a reviewed binding by merchant, store, object kind and native request key or plan revision. It checks the prebound C15 alias, canonical subject, alias proof and current stops. Each binding supplies an exact sorted set of source revisions and the provenance proof for adoption. The operator must enroll new revisions explicitly. Admission returns a digest of this binding. A native producer carries that digest through the attempt. Plan selection carries the digest from its cache lookup through capture. Reconfiguration during either operation causes the receiver to reject the old digest.

| Private operation under /internal/integrations/lineage | Key | Behavior |
| --- | --- | --- |
| admit | Native judgment request key | Check the guard, binding and stops before reservation. Returning a selected judgment also requires its winning attempt ID, registered payload and intact attribution. |
| capture | Existing attempt ID or reviewed plan revision | Lock the merchant, verify the attempt owner and request key for raw output, write a new payload and register its C15 lineage in one transaction. |
| read | Same object key | Reauthorize, recheck the frozen membership, body hash, retention window and current stops on every read. |

Capture retries with the same body and provenance succeed. Conflicts, unregistered historical content, expired or missing registered content and unresolved attribution fail closed. A read may return an empty plan-cache result only for an authorized revision with neither a payload nor a registration. Storage failures return an opaque error. Responses use no-store caching. C11 bounds each signed request to 200,000 bytes. Larger native raw outputs are rejected on enrolled stores.

## Stops, retries and history

Admission runs on every native attempt. Capture repeats the checks when a response arrives. A stop between these operations prevents payload persistence and selection. The native attempt, result and reservation accounting commit independently of capture. A failed capture never falls back to direct SQL or filesystem writes. Signed reads expose only current registered payloads in a reviewed scope. A cache hit must pass the same frozen attribution, integrity and retention checks for the winning attempt. First capture trusts the authenticated native producer for the submitted bytes and the originating attempt. A compromised producer that holds admission and capture grants is outside this boundary. C15 remains the authority for operator adoption, stops, draining and restore. Before freezing a census or returning a receipt, C15 verifies the registered attributions in the store against their fingerprints, then selects the matching subject or source. Damaged attribution leaves the privacy stop active. A historical receipt whose members omit a matching registration fails validation.

The PostgreSQL rehearsal uses separate native, capture and operator roles. The native role can maintain the journal but cannot read or insert raw body content. The capture role can register resources and sources but cannot change aliases, stops, privacy requests or spending. Row-lock privileges on the attempt and raw output are scoped to columns. Only disposable databases are granted these roles.

For Wakaru, an operator calls C15 guard installation and then explicitly enrolls each legacy SQLite table in a dedicated file. Enrollment applies to the whole table in that file. Triggers require the authenticated capture connection for new inserts and updates. An unenrolled native process rejects the guarded file, including before a first registered plan exists. SQLite file ownership and independently retained ledger custody remain deployment prerequisites. Callers with native administrative or filesystem access are outside the authenticated service boundary.

## Qualification

The combined cross-service rehearsal runs the real Go Store.Judge and authenticated raw-output reader against PostgreSQL, and the real Wakaru Store.judge and RecoveryWorkflow.run against SQLite. Model transports are synthetic. It covers withdrawal during inference, cache reads after withdrawal, grant and body substitution, exact and conflicting retries, a failed ledger write, restart and the existing C15 retained-ledger restore. The inventory records exact predecessor and successor source hashes and retains its previous classifications. Requalify the combined service heads if any changes.

Runtime identity provisioning, managed role grants and restore custody, business ownership of attribution bindings, external copies and whole-application erasure remain open. Live ingestion, deployment and email delivery stay disabled.


The acceptance entry point is Engine's `scripts/rehearse-integration-lineage.py`. Supply the parent directory of four sibling checkouts named `engine`, `inkwell`, `wakaru` and `web`, a new output directory and a fresh loopback PostgreSQL 16 cluster containing `c15_admin_test`. The Engine workflow supplies the pinned companion revisions and dependencies. Wakaru's standalone SQLite acceptance runs with `python scripts/test_integration_lineage_service.py -v`.
