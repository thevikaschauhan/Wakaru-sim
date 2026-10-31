# Authenticated knowledge boundary: local, opt-in slice

This slice adds `create_knowledge_service_app` and the companion Engine
`internal/knowledgeclient` package. Neither is registered in application startup,
workers or the runtime allowlist. No database migration is applied automatically.

The factory is empty by default. Explicit enablement requires a previously
initialized `KnowledgeStore` and separately provisioned C11 service grants with
 audience `wakaru-knowledge`, a canonical merchant UUID, a store ID and exact
`POST /internal/integrations/knowledge/<operation>` permissions. The existing
30-second, exact-body HMAC authentication protocol is reused unchanged. Merchant
JWTs, model output and source text cannot create a service principal. Query
strings, duplicate fields, unknown fields and oversized requests are refused.
The schema is `contracts/knowledge/service-v1/request.schema.json`.

## Operations and authority

- `provision`: register an Engine-resolved subject and current generations.
- `source`: register an Engine-approved receipt and matching base64 source bytes.
  The store retains the receipt, not the raw content. Signature/hash validation
  does not prove that a human approved a claim or that the claim is true.
- `entity`: register a scoped authoritative catalog identity.
- `save`: admit a knowledge batch using stored receipts, version CAS and durable
  command idempotency. Unknown source claims do not become approved by being sent.
- `current` and `export_subject`: return current evidence or a scoped stored export.
- `withdraw_source`, `revise_identity`, `erase_subject`: enforce the existing
  retained source/privacy fences, including delayed and repeated commands.

Tenant/store authority comes from the grant, not the body. Subject identity and
its revisions must still be resolved by the privileged Engine caller. This is a
transport boundary for trusted Engine commands, not a merchant-facing proxy and
not a completed source-capture or approval workflow. Existing Engine Shopify,
profile, preference, source-approval and privacy-job producers are not wired.
Credentials must be restricted to the operations and tenant they actually own.

Bootstrap, quarantine release, restore checkpoints, merchant-wide export and
merchant-wide erase are deliberately not remote operations in this slice. A
quarantined store cannot be opened through this API. Broader privacy fulfillment,
retained-ledger freshness, RLS and least-privilege database roles remain gates.

## Transport, replay and privacy

Engine signs the exact body and pins the endpoint. Production configuration
requires HTTPS; an explicit fixture flag allows literal loopback HTTP only.
Redirects are never followed. Requests are bounded to 200,000 bytes, source base64
to 160,000 characters, and responses to 2,000,000 bytes. Oversized exports fail
rather than returning partial completion. Export paging/disposal remains future
work. The service emits `Cache-Control: no-store` and never echoes exception
messages or SQL details. The host must keep access logs free of authorization
headers and request bodies; no observability pipeline is enabled here.

Successful receipts bind the operation, exact request hash and complete scope.
A receipt acknowledges that scoped operation, not whole-application erasure,
authorization to send, source approval beyond the supplied trusted receipt, or
permission to serve a cached snapshot. Consumers must recheck freshness and
Engine policy before use. The client rejects mismatched/duplicate/trailing or
oversized responses and does not retry automatically. A timeout, invalid response
or HTTP failure is not proof that no mutation occurred. Save reconciliation uses
the same command ID and payload; changing the command ID after uncertainty is
not a retry policy. A repeated identity transition can refuse the old generation;
its owner must reconcile the current identity instead of replaying blindly.

## Qualification

`tests/knowledge/test_service.py` exercises the service against disposable
PostgreSQL: authentication and tampering, scope binding, restricted grants,
source admission, CAS, restart, export, erasure, withdrawals, identity changes,
quarantine and redacted dependency failure. Existing lifecycle tests remain in
place. Engine client tests include real HTTP signing, response binding, redirects,
malformed responses and cancellation with race detection.

Cross-language proof is explicit and never skips missing prerequisites:

```sh
# In the companion Engine worktree:
go build -tags knowledgefixtures -o /tmp/knowledge-service-fixture ./cmd/knowledge-service-fixture
# In Wakaru, with local PostgreSQL tools and test dependencies installed:
KNOWLEDGE_ENGINE_FIXTURE=/tmp/knowledge-service-fixture python -m pytest ops/knowledge/test_engine_bridge.py
python -m pytest tests/knowledge tests/test_knowledge_contract.py
python contracts/knowledge/service-v1/verify.py
python ops/runtime/verify_privacy.py
```

The bridge runs the real Go client as separate processes against a loopback Python
HTTP service and disposable PostgreSQL, including source registration, persistent
memory, export/erase, withdrawal, identity changes and quarantine denial. The
fixture driver reads no environment credentials and rejects non-loopback URLs.

No production provider, live merchant, deployment or existing simulation artifact
is touched. All 85 packaged runtime files stay unchanged. No public push or hosted
CI run is part of this local slice. Next: resolve the production Engine identity
and approved-source adapters and authenticate privacy-job completion/invalidation
receipts before cross-repository activation or packaging.
