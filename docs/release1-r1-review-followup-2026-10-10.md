# Release 1 hardening review follow-up

10 October 2026. Follow-up to PR #84 at parent `a16059e66f0c9db0d0161c744c51472e30fa2624`.

## Reproduction and change

The real adapter-to-SQLite judgment path, using mocked HTTP responses, accepted a present string, boolean, negative or null `usage.cost` as an evaluated result with unknown cost. Expanded regression cases also reproduced acceptance of NaN/infinity and an uncaught overflow for a 401-digit integer. Before the runtime fix, seven added cases failed and the other 45 focused cases passed.

`validate` now rejects a present cost unless it is numeric, non-boolean, finite, non-negative and representable by the existing accounting path. An oversized integer becomes `invalid_usage` instead of escaping the adapter. The existing error path durably records `schema_failure`, preserves the attempt reservation and leaves no selected judgment or lease. Missing cost remains unreported. Positive coverage verifies missing usage, missing cost, zero cost, an in-reservation amount and a valid over-reservation amount, including all three budget records and the over-reservation hold.

The idle-retention regression now checks the database row count before `PlanJournal.load`, which independently purges expired rows. A controlled no-op-purge substitution passed the original test but fails the corrected assertion (`2 != 1`). The authenticated synchronous and queued intake tests now assert the exact rejection code, so an unrelated HTTP 400 cannot satisfy them.

## Validation

- Focused provider/retention/intake suite: **57 passed**.
- Full backend, intelligence and learning suites with signed completion enabled: **437 passed**, no skips. This includes queued-job rehydration and rollback.
- Deliberate no-op retention substitution: **one expected failure**, proving the corrected test detects missing cleanup.
- Contract hashes and pinned Ruff bug-class checks passed.
- Temporary local Redis 8.6.2, Python 3.12 and redis-py 6.4.0; the temporary environment reuses the existing dependency overlay. Hosted CI installs declared requirements and uses Redis 8.2.9, so hosted results remain a separate gate.
- GitNexus impact identified `Client.evaluate` as the direct production caller, low risk. Test functions have no resolved upstream callers. A forced refresh completed, but the MCP still reported one-commit staleness and the index reports truncated flows; these results supplement the reviewed diff and executed suites rather than proving complete impact coverage.

No provider request, deployment, policy activation, publication or shopper send was performed. The adapter endpoint/model and shared contract did not change. Use a new reviewed deployment profile for future activation, as required by the existing rollout process; this patch does not rewrite historical judgments or plans.

## Remaining gates

Push this follow-up on the existing PR and verify hosted CI at its exact head. Resolve the outstanding changes-requested review through the normal review process before merge. Production and staging still need their separately authorized rollout and acceptance; local test success does not close identity, B3, semantic-quality or delivery gates.
