# TypeSafe pre-launch operations

These profiles support TS-15/16 in isolated development or staging processes. They do not deploy services, publish a contract, enable execution policies, configure a recipient, or promote a conversion model. Keep production settings unchanged.

## Start an explicitly scoped process

1. Apply existing migrations through Engine 056 and Inkwell 0049. Use passive consumers before producers. Back up and inspect existing fact-review inventory before any staging migration. These changes add no migration and activate no facts.
2. Copy `development.json` to a private path. Replace the merchant placeholder with actual **development-store merchant UUIDs**, keep OpenRouter and the approved revision, and choose each workflow's mode. For staging, set `environment` to `staging`. Do not put credentials in the profile. Disabled workflows use `off` and an empty merchant list. Begin only the intended component in `observe`; advance passing categories to `enforce` using a new deployment ID. Independent semantic-quality evaluation remains required for wider autonomous coverage.
3. Install the reviewed profile locally with Engine's `scripts/typesafe-operations.py activate --input PROFILE --directory PRIVATE_RUNTIME_DIRECTORY --deployment-id UNIQUE_ID`. It creates an immutable deployment receipt and atomically replaces `active.json`. This is a filesystem operation, not a platform deployment. Use the same reviewed file on each participating API/worker host. A multi-host rollout requires coordinating those copies; this tool does not promise atomic cross-host activation.
4. In **each actual API and worker process**, set `TYPESAFE_PRELAUNCH=1`, `TYPESAFE_ENVIRONMENT=development` (or staging), and `TYPESAFE_DEPLOYMENT_FILE` to that absolute `active.json` path. Set `TYPESAFE_PROVIDER=openrouter`, `TYPESAFE_MODEL=typesafe/jev-1.13`, and `TYPESAFE_APPROVED_MODELS=typesafe/jev-1.13-20260917`. Supply `OPENROUTER_API_KEY` through existing server-side secrets. The adapter permits only `https://openrouter.ai/api/alpha/decisions`, refuses redirects, and caps provider work at three seconds with at most two attempts.
5. Set corresponding `TYPESAFE_POLICY_MODE`, `TYPESAFE_RECOVERY_MODE`, `TYPESAFE_CLAIMS_MODE` and `_MERCHANTS` variables to the reviewed profile modes and explicit IDs. A disagreement, unknown workflow, missing file, disallowed merchant or model binding holds inference. Engine needs policy/recovery settings, Wakaru recovery, Inkwell claims. Run the synthetic provider smoke inside every deployed worker image before enabling a component. API-only credentials do not prove worker wiring.
6. Wakaru requires an absolute `TYPESAFE_JOURNAL_PATH` on a persistent **single-host** volume. SQLite is not a distributed-worker journal; do not expand workers onto independent hosts with separate journals. Ensure backup and tenant-deletion handling includes this file and learning registry. Do not enable conversion models unless a separate real-outcome promotion report qualifies them.

The Wakaru worker purges its local journal on startup and hourly, including when
the queue is idle and inference is disabled. Every judgment also runs the same
global purge; loading a recovery plan expires old plans for all merchants.
Raw provider output and plans expire after seven days; attempts, results,
selected judgments, audit records and budget history expire after ninety days.
Current leases and budgets are preserved. Retention emits content-free completion
or failure logs and retries failures on the next interval. Keep the worker running
while the volume contains journal data. During a worker shutdown, operators can
run `python -c 'import os; from cart_recovery.intelligence.maintenance import purge_journal; purge_journal(os.environ["TYPESAFE_JOURNAL_PATH"])'`
on that same mounted volume. Backups and exported artifacts need their own deletion
schedule; a service restart or model rollback does not reset retention.

All pre-launch Inkwell recovery send authorization and worker dispatch are blocked even for previously approved documents. Engine's Inkwell-facing send endpoint is independently blocked. Inkwell contract publish and save-and-publish endpoints are blocked. Review, correction, preview and exact-revision approval remain usable. External CI/deployment permissions and Shopify credentials must retain the existing no-publish scope; these application fences are not a substitute for platform permissions.

## Rollback rehearsal

Use `off` in the affected workflow's copied profile and install it with a **new** deployment ID. Keep `TYPESAFE_PRELAUNCH=1`; never remove the safety fence as a rollback technique. The next judgment checks controls before cached selection; recovery checks before saved plans and after inference. An in-flight result from another deployment is not applied. Already recorded attempts and historical judgments remain auditable. Changing the deployment ID changes judgment and plan cache binding even when reverting to an earlier provider/rubric.

Do not restore old `active.json` bytes or reuse a deployment ID. Do not clear review requirements, resurrect revoked facts, unsuppress purchases, or retry an unknown provider send. Existing checker-disabled logic preserves unresolved claim holds. To restart a workflow, install a newly reviewed profile and configure the matching process modes. A queued job pinned to an incompatible mode is held. New evidence or fact revisions require normal regeneration and revalidation.

Run Engine's `scripts/test-completion-slice.sh` with disposable local `TEST_DATABASE_URL`, `INKWELL_TEST_DSN` and `COMPLETION_PYTHON`. It refuses remote databases, clears live-provider credentials, exercises the captured-store replay, signed persisted worker job, duplicate restart, rendered approval revision, failure load and rollback. Run `scripts/test-learning-slice.sh` for the point-in-time outcome, refund, identity-gap and contamination matrix. The receipts are private temporary artifacts. The replay adds an explicit synthetic error to sanitized historical captured events; it is not a fresh deployed-store run.

## Dashboard and alarms

Ship `typesafe_operation` JSON log lines from Engine, Inkwell and Wakaru to the existing private log collector. They contain only bounded service/workflow labels, status/reason, timing, attempt counts, cache hits, reported cost and conservative reservations. They contain no tenant IDs, shopper text, prompts, keys or provider bodies. Keep raw provider outputs in their existing seven-day storage and audit metadata within the existing ninety-day cap or shorter tenant policy.

Export a bounded log window and run `scripts/typesafe-operations.py report LOG... --output NEW_REPORT.json`. Each source line must appear once; do not concatenate overlapping exports or failure output that reprints captured logs. Open Web `/typesafe-operations` and choose that report. It is parsed locally and never uploaded. The dashboard is a saved log window, not a live health endpoint. Refresh the export for a new window.

The report shows per-workflow p50/p95 judgment latency, provider attempts, cache reuse, unavailable results, reported costs, reservations and unknown-cost counts. Cache replay never re-counts historical attempts. Unknown provider charges are not reported as complete zero-cost totals. Schema validity is a response-shape metric, not semantic accuracy. Alerts flag unknown returned model revisions, budget/backpressure/cost overruns, store p95 above 3.5 seconds, schema validity below 99% after 100 responses, and missing service telemetry. An unknown revision is quarantined automatically; obtain fresh conformance and evaluation evidence before adding it to an approved list. No notification is sent by this report tool.

On missing telemetry or unexplained cost/latency, hold the affected workflow with a new deployment record and inspect the bounded request/attempt ledger under tenant-scoped operator access. Compare API and worker environment/mounts, current revision and collected window before re-enabling. Draft semantic work retains the existing USD 0.005 per workflow/episode reservation cap; profile daily limits further restrict merchant and service-environment spend. These are per-service ledgers, not a pooled cross-service spend guarantee. The deployment-file setting itself also enables the process safety fence; setting the pre-launch flag to zero while retaining that path does not disable it. Leave headroom for all three services and provider-side limits.

## Acceptance and evidence limits

The local fixture tests orchestration and deterministic safety. A separate live synthetic OpenRouter response proves wiring for that process. Neither proves live storefront tracking, actual worker deployment, independent 500-case semantic quality, deliverability, customer intent, conversion calibration, revenue or lift. A fresh hosted development/staging trace requires separately authorized deployment; do not call the local replay deployed acceptance. Preserve no-publish/no-shopper-send boundaries throughout that future rehearsal.

## Browser rehearsal

After the completion script returns its evidence directory, start the Web development server with `NEXT_PUBLIC_INKWELL_URL=http://localhost:4182/v1` on `127.0.0.1:3183`. Use a disposable Auth.js secret and the local Engine URL in this test process; never use production browser credentials.

In a second terminal, start Inkwell's opt-in API fixture with the same local test DSN:

```sh
INKWELL_SEMANTIC_BROWSER_FIXTURE=/private/tmp/typesafe-browser.json \
TYPESAFE_BROWSER_MERCHANT=<merchantId from document.json> \
TYPESAFE_BROWSER_DOCUMENT=<documentId from document.json> \
TYPESAFE_BROWSER_ORIGIN=http://127.0.0.1:3183 \
TYPESAFE_PRELAUNCH=1 \
go test ./internal/api -run TestSemanticReviewBrowserFixture -count=1 -v
```

The fixture writes a private local test token and runs for at most fifteen minutes. It has no sender or external Engine client. From Web, run:

```sh
INKWELL_TEST_DSN="$LOCAL_INKWELL_TEST_DSN" \
python3 scripts/test-typesafe-review-browser.py \
  --fixture /private/tmp/typesafe-browser.json \
  --report "$COMPLETION_OUTPUT/operations.json" \
  --output "$PRIVATE_BROWSER_EVIDENCE"
```

Install the Playwright CLI/browser first or set `PLAYWRIGHT_CLI` to its launcher. The script restores only synthetic local browser state, corrects the held plan, requests regeneration through HTTP, executes the real local composer against that document, approves the new preview, and checks the dashboard. The composition provider and semantic responses are fixtures. Touch `/private/tmp/typesafe-browser.json.done` to stop the API fixture, and stop the temporary Web server. Keep token/storage files outside committed evidence. A new run needs a fresh completion-script document because this test intentionally approves its final revision.
