# AF06 recommendation service

This module is a dark, internal recommendation endpoint. It never sends email.
The [technical handoff](../../../engine/docs/adaptive-followup-af06.md) documents
contracts, source requirements, HMAC framing, judgment thresholds, cache and
lease semantics, budgets, runtime configuration, architecture and release gates.

The pinned AF01 schemas remain in `contracts/recovery/v2`; the existing
`cart_recovery.intelligence.typesafe.Client` remains the OpenRouter transport.
No existing v1 SQLite table or behavior is replaced.

## Local fixture verification

Use a temporary virtual environment with the backend requirements and pytest /
fakeredis installed. Point `AF06_TEST_DATABASE_URL` only at an isolated local
PostgreSQL database whose name includes `af06_`. Tests install the additive
journal schema if absent; they use mock provider transports, randomly scoped
fixture tenants and registry entries. The real HTTP test binds loopback only.

```sh
python -m pytest cart_recovery/adaptive/tests cart_recovery/intelligence/test_typesafe.py
python contracts/recovery/v2/verify.py
python -m cart_recovery.adaptive.evaluation ../engine/docs/evidence/adaptive-followup-af06/synthetic-selection-rows.jsonl
```

The saved synthetic selection report demonstrates control flow and reporting.
It has mock answers, no independent human labels and no paid-model usage.
Its reducer-only latency is not a service benchmark. Its blocked gates must not
be interpreted as measured product quality, calibration or conversion uplift.

## Migration and release boundary

Apply `migrations/001_journal.sql` explicitly and atomically, for example with
`psql --single-transaction -v ON_ERROR_STOP=1 -f ...` against the chosen semantic
database. The migration enables no deployment and contains no credentials.
Register immutable policy JSON/hash, the environment's shared limits and exact
workflow/model/rubric binding through an operator-controlled configuration step.
Application access is backend-only; RLS/PUBLIC revocations do not grant an
application role automatically. Production database roles, encryption, retention
and erasure need operational review.

The combined candidate also contains draft checking, measurement, erasure and
fresh Engine release-authority checks. See the current
[combined runtime qualification](../../docs/combined-runtime.md). Engine's caller
must assemble complete resolved inputs, authenticate the call, reject stale
bindings under its current lease and persist composition work atomically. Neither
this endpoint nor a passing semantic bundle substitutes for AF08 dispatch checks.
Production qualification and TF08 remain disabled. Apply `002_erasure.sql` after
the journal migration when qualifying the combined adaptive database; runtime
images do not execute migrations automatically.
