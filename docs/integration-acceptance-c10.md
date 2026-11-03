# C10 cross-service acceptance

C10 qualifies the dormant C1-C9 integration foundation with synthetic data. A
passing report means the selected local or hosted fixture matrix passed at the
recorded source hashes. It does not enable provider ingestion, production routes,
deployment, merchant approval, or email delivery.

## Connected behavior

`TestC10CrossServiceAcceptance` creates an isolated Engine merchant and Shopify
projection in PostgreSQL using restricted runtime roles. It installs the fixture
Gorgias account, freezes a C6 context and checks an explicit C7 support observation.
Engine forwards the frozen request to Wakaru's actual private C8 HTTP blueprint,
using a synthetic bearer token bound to that merchant. Wakaru uses its real SQLite
privacy owner and bounded interpreter. No model or provider is contacted.

The resulting handoff is adopted by an Inkwell test binary into PostgreSQL C5/C7
artifacts. Inkwell's actual private composition handler accepts the current
revision and rejects missing authentication. Repeated composition is idempotent.
The fixture supplies an empty approved fact set, so copy remains empty and
review-only. C8's separate authority tests exercise positive fact selection,
current-product bounds, private text exclusion and hostile-plan rejection.

A real Next page reads Engine overview, Inkwell review status and current copy
through HTTP, without intercepting those responses. The test controller reopens
the support case and explicitly delivers its revision invalidation to Inkwell.
Engine refuses further analysis/composition. The browser reload shows the hold
and context wait, disables copy inspection, and the previous copy endpoint
returns 409. Desktop/mobile captures and keyboard expansion are recorded. The
browser makes zero API mutations. Other dashboard shell/onboarding calls are
stubbed, and external browser requests are aborted.

These adapters are hosted only by test code. The synthetic bearer mapping is not
production authentication. Adoption and invalidation delivery are explicit test
controls, not a deployed ingestion worker or outbox transport. Provider-authorized
per-shopper coverage and real account-to-store mapping still require sandbox proof.

## Reproducible qualification

Use sibling isolated checkouts named `engine`, `inkwell`, `wakaru`, and `web`.
Install Go from `inkwell/go.mod`, Web's lockfile with `npm ci`, Chromium with
`npx playwright install chromium`, and Python dependencies:

```sh
python -m pip install 'jsonschema[format-nongpl]==4.25.1' rfc8785==0.1.4 flask==3.1.3 pytest==8.4.2
```

Create two disposable loopback databases named `c10_engine_test` and
`c10_inkwell_test`. Use `scripts/setup-integration-db.py` to migrate Engine and
`go run ./cmd/migrate up` in Inkwell. The test database owner needs permission to
create restricted fixture roles; never use a production database. Set
`INTEGRATIONS_FULL_SCHEMA_DATABASE_URL` and
`INTEGRATION_PRIVACY_TEST_DATABASE_URL` to their explicit `127.0.0.1` PostgreSQL
URLs with `sslmode=disable`. The runner rejects other hosts, names and options.

```sh
python scripts/test_qualify_integrations.py
python scripts/qualify-integrations.py --workspace .. --output /tmp/c10-evidence
```

The runner strips service/provider environment variables from child processes,
builds the companion fixtures, checks contract parity, runs race-enabled Go tests,
Python replay/privacy tests, Web's strict wire/component readers, and the connected
browser path. Actual C9 Go wire exports are supplied to Web tests. Every selected
test must pass; skips, empty reports and duplicate outcomes fail qualification.
The fourteen rows in `contracts/integrations/c10/matrix.json` require named tests.
The report records full commit IDs, dirty status, source content hashes, test names,
log/XML hashes, browser results and blocked release gates. It fails if source files
change during execution. Generated browser evidence is ignored by Git.

The Engine CI workflow pins all three companions by full SHA and uploads the report,
logs and screenshots even on failure. Local passing evidence and hosted CI remain
separate. Standard repository CI supplies the broader regression and build checks.
All PRs stay draft and stacked on the preceding slice. The Web C10 branch is
explicitly disabled in Vercel configuration before its first push. Inkwell C10 CI
cannot receive the optional staging Engine credentials.

## Coverage limits and release gates

The matrix exercises lifecycle/replay, conservative support, exact context and
fact bindings, dispatch ordering, tenant isolation, C5 erasure, mixed-version
receipts, and the merchant read path. It combines a connected four-service journey
with independent adversarial tests. It is not fourteen complete live-provider
journeys. C7's direct and coordinated dispatch tests use fake providers; zero
provider calls are required when a hold wins ordering. Positive baseline tests
may call those fakes. No real delivery provider is configured.

The C5 cross-service privacy proof covers `integration_c5_v1`, including queued
artifacts and replay fences. It does not certify legacy email documents, orders,
provider copies, backups, or every historical derivative. Real Gorgias merges,
guest identities, multi-store account mapping and provider pagination must also
be checked against an authorized sandbox. Missing/ambiguous fixture identities
remain conservative, not guessed matches.

The following gates remain blocked regardless of a green report:

1. Provider-authorized Shopify/Gorgias sandbox and verified account/store/subject
   coverage, including refresh, merge, guest, deletion and notification behavior.
2. Complete privacy inventory and erasure evidence for legacy and external stores.
3. Reviewed production service/merchant authentication, fact resolution, route
   mounting, transport, rate limits and least-privilege grants.
4. Authorized shadow ingestion with measured reconciliation, freshness and holds.
5. Separately authorized pilot deployment and rollback acceptance.
6. Separately authorized controlled email delivery and provider-side verification.

Rollback must disable personalization without clearing deterministic holds.
Disabling ingestion produces unavailable/stale coverage, never a clear receipt.
Keep privacy fences, revision history and audit schemas; do not roll back by
removing tables or restoring old approvals.
