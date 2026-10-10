# Release 1 R1-A: hardening verification and CI coverage

10 October 2026. Continues PR #84 from `5d1f2c2ee3a680241af6e3c909342bf4afc9a397`. The existing branch supplies the malformed-intake, provider-accounting and idle-retention fixes. This follow-up changes CI and adds a synthetic fixture; it does not change runtime behavior.

## Reproduced gap

The backend workflow runs backend tests. The contracts workflow separately runs `cart_recovery/intelligence`. Their combined collection contains 417 tests and no `cart_recovery/learning` tests. The signed completion test skips unless a request file is supplied. Successful previous CI therefore did not exercise the eight learning regressions or that opt-in completion trace.

## Changes

- The contracts workflow now includes the learning package, covering data leakage, maturity, contamination, currency accounting, serving states and synthetic-promotion refusal.
- The backend workflow runs the signed completion test separately from the repository root with a committed synthetic request and runner-temporary output.
- The fixture uses reserved `.invalid` contact data, invented product data, fixed tenant/episode IDs and two synthetic evidence events. It contains no live customer, recovery URL, credential or copied browser identity.
- The existing completion test uses a mocked provider transport and verifies persisted RQ/SQLite replay, no additional inference on restart, unchanged buyer state and a held result after rollback. No live provider credentials are required.

## Local verification

- Python 3.12 environment with the repository-pinned `redis==6.4.0`: **424 passed, one completion-harness skip** across backend, intelligence and learning.
- The skipped completion test then ran with the committed synthetic fixture: **one passed**. CI now supplies that fixture automatically.
- Exact expanded contracts command: **46 passed**, including all eight learning tests.
- `intelligence_v1` manifest verification passed.
- The Redis reconnect regression used an isolated local Redis 8.6.2 Unix socket. Hosted CI continues to use its existing Redis 8.2.9 service; this local run does not establish server-version parity.
- Other Python dependencies came from the existing test environment via a temporary environment overlay. The old environment was unchanged. A clean hosted dependency install remains CI's responsibility.
- Known Flask-Limiter test-storage and RQ deprecation warnings remain; no warning is represented as a production readiness check.

The graph index reported dropped entry points/callees and one trace-budget limit. No runtime symbol is modified by this follow-up. Review the complete YAML/fixture diff in addition to the pre-commit graph check.

## Reproduce

From the repository root with backend test dependencies installed:

```sh
python -m pytest -q -ra backend/tests cart_recovery/intelligence cart_recovery/learning
TYPESAFE_COMPLETION_REQUEST=backend/tests/fixtures/typesafe-completion-request.json \
TYPESAFE_SYNTHETIC_INSIGHT=/tmp/typesafe-completion-insight.json \
python -m pytest -q -ra backend/tests/test_completion_slice.py
python3 contracts/intelligence/v1/verify.py
```

Set `WAKARU_TEST_REDIS_URL` to an isolated test Redis instance to include the real reconnect test. Do not point it at production. The completion test's output is synthetic and must not be promoted to a real training dataset.

## Remaining gates

Merge and deployment are separate from local verification. Recheck PR checks on the new head. The next revised-plan package is R1-B: read-only deployed revision, schema, worker-configuration, provider-binding and no-send-control inventory. Genuine Klaviyo identity acceptance, independent semantic quality, outcomes, delivery and merchant experiments remain open.
