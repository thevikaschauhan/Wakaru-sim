# M1c authenticated cleanup and retained export transport

This slice adds an explicit `create_privacy_app` factory and Engine `PrivacyClient`. Both remain unmounted at startup. It does not add a production worker, Engine privacy-job acknowledgment, scheduling, a new database migration, or whole-application export/completion.

## Authority and requests

Use a separate `wakaru-knowledge-privacy` audience with tenant/store and exact-operation grants. Existing knowledge and delivery credentials grant no privacy transport permission. Only `purge_withdrawn` and `export_retained_page` are exposed under `/internal/integrations/knowledge-privacy/`. Bootstrap, quarantine release, merchant-wide operations and source capture remain unavailable here. Engine callers must independently resolve and authorize the exact subject identity/privacy scope. A client method is not proof that a privacy job is authorized or complete.

Request JSON uses the strict `knowledge_privacy_request_v1` schema. Unknown/duplicate fields, operation/path mismatches and invalid scope bindings fail. Request bodies are bounded to 16 KB. Responses bind the scope, operation and exact request SHA-256, with `Cache-Control: no-store`; errors contain no SQL, source or identity details. HTTPS is mandatory in the Engine client except explicitly selected literal loopback fixtures. Redirects and automatic retries are disabled. The client embeds the same request/response schemas as Wakaru and refuses missing fields, malformed results, excessive responses or a completion claim.

Cleanup requires `expected_version`, checked inside the locked cleanup transaction before any deletion. A successful response reports the transaction's resulting version and deletion counts. A competing cleanup using the old version is refused. A no-op at the current version leaves that version unchanged. Timeout, lost acknowledgment, transport refusal or malformed response yields unknown outcome in Engine, without retry. A deliberate later request requires independent current scope/version resolution. There is no new durable cleanup receipt or automatic ambiguity reconciliation.

## Bounded export orchestration

`PrivacyClient.ExportRetained` assembles only Wakaru's retained metadata for one authorized subject. It reads withdrawals/tombstones, delivery receipts and invalidation events under one snapshot. Each page is limited to 100 rows; callers set a total budget between 1 and 10,000 rows. The client validates field layouts, row shapes/order, cursor progress, counts, scope, subject/tenant erasure state and snapshot continuity. Any refusal, mismatch, truncation or budget overflow returns no partial export. Snapshot changes require a new explicitly initiated export.

Exports work after subject/merchant erasure using the exact retained scope. They remain pseudonymous personal metadata requiring protected storage and retention controls. No file is persisted by the reusable client. Current payload/source/catalog/save-command export, Engine retained-command inventory, cross-service export artifact assembly and delivery to the requester remain separate obligations. A snapshot reflects a point-in-time export, not an ongoing privacy permission.

## Qualification and remaining gates

`scripts/qualify-knowledge-privacy.py` in Engine builds a race-enabled Go fixture and runs it against the real Python WSGI app and disposable PostgreSQL. Fixtures test purge, stale version refusal, multi-page export after erasure, lost acknowledgment with exactly one attempt, wrong scope and quarantine. Go response-adversary tests and the complete Wakaru knowledge suite run in the same qualification. No environment database URLs, remote providers or network dependency installation are used.

The older order-source bridge remains a separate regression gate. The Engine C5 blocker stays in force. Next: Engine retained inventory export, privacy-job authorization/orchestration, aggregate obligation accounting/acknowledgment and complete retained-ledger restore qualification. Retention policy, least privilege, key custody, forward/rollback artifacts, hosted CI and native AMD64 qualification remain pending. Existing rollback artifacts do not qualify this new transport or tombstone-aware runtime.
